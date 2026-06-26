"""
gui_app.py — vSphere Backup Manager
Flask web UI: login → VM browser → create jobs → schedule backups
"""
import os
import sys
import uuid
import threading
import time
import platform
import subprocess
import json
import sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask, request, redirect, url_for, session,
    flash, jsonify, abort, render_template
)

from backup_core import run_backup, list_vms

IS_LINUX = platform.system() == 'Linux'

# ── APScheduler (optional graceful degradation) ──────────────────────────────
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False
    print("WARNING: APScheduler not installed — recurring schedules disabled. "
          "Install with: pip install APScheduler", file=sys.stderr)

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'vsphere-backup-dev-key-change-me')

BASE_DIR  = Path(__file__).resolve().parent
JOBS_DIR  = BASE_DIR / 'jobs'
JOBS_DIR.mkdir(exist_ok=True)

JOBS_DB_PATH = BASE_DIR / 'jobs.json'
DB_PATH = BASE_DIR / 'jobs.db'
jobs_db_lock = threading.RLock()

# In-memory job store: {job_id: job_dict}
jobs: dict = {}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                started REAL,
                status TEXT,
                data TEXT
            )
        ''')
        conn.commit()
    finally:
        conn.close()

def migrate_old_json_db():
    if JOBS_DB_PATH.exists():
        try:
            with open(JOBS_DB_PATH, 'r', encoding='utf-8') as f:
                old_jobs = json.load(f)
            if old_jobs and isinstance(old_jobs, dict):
                init_db()
                conn = sqlite3.connect(DB_PATH)
                try:
                    cursor = conn.cursor()
                    for jid, info in old_jobs.items():
                        cursor.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,))
                        if not cursor.fetchone():
                            cursor.execute(
                                "INSERT INTO jobs (id, started, status, data) VALUES (?, ?, ?, ?)",
                                (jid, info.get('started', 0), info.get('status', ''), json.dumps(info, ensure_ascii=False))
                            )
                    conn.commit()
                    print(f"MIGRATION: Successfully migrated jobs from jobs.json to SQLite database.")
                finally:
                    conn.close()
            try:
                bak_path = BASE_DIR / 'jobs.json.bak'
                if bak_path.exists():
                    bak_path.unlink()
                JOBS_DB_PATH.rename(bak_path)
            except Exception:
                pass
        except Exception as e:
            print(f"WARNING: Migration of jobs.json failed: {e}", file=sys.stderr)

def load_jobs_db():
    global jobs
    init_db()
    migrate_old_json_db()
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, data FROM jobs")
        rows = cursor.fetchall()
        
        with jobs_db_lock:
            jobs.clear()
            for jid, data_str in rows:
                try:
                    jobs[jid] = json.loads(data_str)
                except Exception as e:
                    print(f"ERROR: Failed to parse job data for {jid}: {e}", file=sys.stderr)
                    
        # Clean up any jobs left in running/queued state across restart
        updated_jobs = []
        with jobs_db_lock:
            for jid, info in jobs.items():
                if info.get('status') in ('running', 'queued'):
                    info['status'] = 'failed (Interrupted by restart)'
                    info['progress'] = {
                        'pct': 100,
                        'phase': 'failed',
                        'detail': 'Job was interrupted by server restart.'
                    }
                    updated_jobs.append((jid, info))
        
        if updated_jobs:
            try:
                cursor = conn.cursor()
                for jid, info in updated_jobs:
                    cursor.execute(
                        "INSERT OR REPLACE INTO jobs (id, started, status, data) VALUES (?, ?, ?, ?)",
                        (jid, info.get('started', 0), info.get('status', ''), json.dumps(info, ensure_ascii=False))
                    )
                conn.commit()
            except Exception as e:
                print(f"ERROR: Failed to update interrupted jobs in SQLite: {e}", file=sys.stderr)
        conn.close()
    except Exception as e:
        print(f"ERROR: Failed to load SQLite database: {e}", file=sys.stderr)

def save_jobs_db():
    with jobs_db_lock:
        try:
            conn = sqlite3.connect(DB_PATH)
            try:
                cursor = conn.cursor()
                # Remove deleted jobs from SQLite
                if jobs:
                    placeholders = ','.join('?' for _ in jobs)
                    cursor.execute(f"DELETE FROM jobs WHERE id NOT IN ({placeholders})", list(jobs.keys()))
                else:
                    cursor.execute("DELETE FROM jobs")
                
                # Insert or replace active jobs
                for jid, info in jobs.items():
                    cursor.execute(
                        "INSERT OR REPLACE INTO jobs (id, started, status, data) VALUES (?, ?, ?, ?)",
                        (jid, info.get('started', 0), info.get('status', ''), json.dumps(info, ensure_ascii=False))
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"ERROR: Failed to save jobs database to SQLite: {e}", file=sys.stderr)

# APScheduler instance
scheduler = None
if HAS_SCHEDULER:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.start()

def register_scheduler_job(info):
    if not HAS_SCHEDULER or not scheduler:
        return None

    jid = info['id']
    schedule_type = info.get('schedule_type')
    schedule_time = info.get('schedule_time', '')
    weekly_day = info.get('weekly_day', '0')
    monthly_day = info.get('monthly_day', '1')
    interval_hours = info.get('interval_hours', '24')
    vm_name = info.get('vm_name')
    vm_names = info.get('vm_names')
    label = info.get('label', '')

    trigger = None
    if schedule_type == 'daily':
        hour, minute = (schedule_time.split(':') + ['00'])[:2]
        trigger = CronTrigger(hour=int(hour), minute=int(minute))
    elif schedule_type == 'weekly':
        hour, minute = (schedule_time.split(':') + ['00'])[:2]
        trigger = CronTrigger(
            day_of_week=int(weekly_day),
            hour=int(hour), minute=int(minute)
        )
    elif schedule_type == 'monthly':
        hour, minute = (schedule_time.split(':') + ['00'])[:2]
        day_val = monthly_day
        if str(day_val).isdigit():
            day_val = max(1, min(28, int(day_val)))
        trigger = CronTrigger(
            day=day_val,
            hour=int(hour), minute=int(minute)
        )
    elif schedule_type == '3_monthly':
        hour, minute = (schedule_time.split(':') + ['00'])[:2]
        day_val = monthly_day
        if str(day_val).isdigit():
            day_val = max(1, min(28, int(day_val)))
        trigger = CronTrigger(
            month='*/3',
            day=day_val,
            hour=int(hour), minute=int(minute)
        )
    elif schedule_type == '6_monthly':
        hour, minute = (schedule_time.split(':') + ['00'])[:2]
        day_val = monthly_day
        if str(day_val).isdigit():
            day_val = max(1, min(28, int(day_val)))
        trigger = CronTrigger(
            month='*/6',
            day=day_val,
            hour=int(hour), minute=int(minute)
        )
    elif schedule_type == 'yearly':
        hour, minute = (schedule_time.split(':') + ['00'])[:2]
        day_val = monthly_day
        if str(day_val).isdigit():
            day_val = max(1, min(28, int(day_val)))
        yearly_month = info.get('yearly_month', '1')
        trigger = CronTrigger(
            month=int(yearly_month) if str(yearly_month).isdigit() else 1,
            day=day_val,
            hour=int(hour), minute=int(minute)
        )
    elif schedule_type == 'interval':
        trigger = IntervalTrigger(hours=max(1, int(interval_hours or 24)))

    if trigger:
        def make_runner(j):
            def _runner():
                run_job_thread(j)
            return _runner

        if vm_names:
            sched_name = f"Backup {len(vm_names)} VMs ({label or jid[:8]})"
        else:
            sched_name = f"Backup {vm_name} ({label or jid[:8]})"

        sched_id = f'backup-{jid}'
        try:
            scheduler.remove_job(sched_id)
        except Exception:
            pass

        sched_job = scheduler.add_job(
            make_runner(jid),
            trigger=trigger,
            id=sched_id,
            name=sched_name,
            misfire_grace_time=3600,
            max_instances=1,
        )
        return sched_job.id
    return None

def reschedule_active_jobs():
    if not HAS_SCHEDULER or not scheduler:
        return
    rescheduled_count = 0
    for jid, info in list(jobs.items()):
        if info.get('schedule_type') and info.get('schedule_type') != 'now' and info.get('schedule_id'):
            try:
                sched_id = register_scheduler_job(info)
                if sched_id:
                    rescheduled_count += 1
            except Exception as e:
                print(f"ERROR: Failed to reschedule job {jid}: {e}", file=sys.stderr)
    print(f"Loaded {len(jobs)} jobs and re-scheduled {rescheduled_count} jobs.")

# Load database and reschedule active tasks on startup
load_jobs_db()
reschedule_active_jobs()

# ── VM list cache ─────────────────────────────────────────────────────────────
# Keyed by (host, user) so different users get separate caches.
_vm_cache: dict = {}          # key -> {'vms': [...], 'ts': float, 'error': str|None}
_vm_cache_lock = threading.Lock()
VM_CACHE_TTL = 60             # seconds before background refresh


def _cache_key(host, user):
    return f'{host}::{user}'


def get_cached_vms(host, user, password, no_verify_ssl=False, force=False):
    """
    Return VM list from cache. If cache is missing or expired, fetch synchronously.
    A background thread keeps the cache warm after the first fetch.
    """
    key = _cache_key(host, user)
    with _vm_cache_lock:
        entry = _vm_cache.get(key)

    now = time.time()
    if not force and entry and (now - entry['ts']) < VM_CACHE_TTL:
        # Fresh cache — return immediately
        return entry['vms'], entry['error'], entry['ts']

    if not force and entry:
        # Stale but exists — return stale data and kick off background refresh
        _start_bg_refresh(host, user, password, no_verify_ssl)
        return entry['vms'], entry['error'], entry['ts']

    # No cache at all — must fetch synchronously (first load or forced refresh)
    return _fetch_and_cache(host, user, password, no_verify_ssl)


def _fetch_and_cache(host, user, password, no_verify_ssl):
    """Fetch VM list from vSphere and store in cache. Returns (vms, error, ts)."""
    key = _cache_key(host, user)
    try:
        vms = list_vms(host, user, password, no_verify_ssl=no_verify_ssl)
        order = {'poweredOn': 0, 'suspended': 1, 'poweredOff': 2}
        vms.sort(key=lambda v: (order.get(v['power_state'], 3), v['name'].lower()))
        entry = {'vms': vms, 'ts': time.time(), 'error': None}
    except Exception as e:
        # Keep old VM list on error, just update error message
        with _vm_cache_lock:
            old = _vm_cache.get(key, {})
        entry = {'vms': old.get('vms', []), 'ts': time.time(), 'error': str(e)}
    with _vm_cache_lock:
        _vm_cache[key] = entry
    return entry['vms'], entry['error'], entry['ts']


_bg_refresh_running: set = set()


def _start_bg_refresh(host, user, password, no_verify_ssl):
    """Kick off a background thread to refresh the cache if not already running."""
    key = _cache_key(host, user)
    with _vm_cache_lock:
        if key in _bg_refresh_running:
            return
        _bg_refresh_running.add(key)

    def _worker():
        try:
            _fetch_and_cache(host, user, password, no_verify_ssl)
        finally:
            with _vm_cache_lock:
                _bg_refresh_running.discard(key)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


# ── NFS management (Linux only) ─────────────────────────────────────────────────

def list_nfs_mounts():
    """Return list of currently mounted NFS/CIFS shares from /proc/mounts."""
    mounts = []
    if not IS_LINUX:
        return mounts
    try:
        with open('/proc/mounts', 'r') as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 4 and parts[2] in ('nfs', 'nfs4', 'cifs'):
                    info = {'device': parts[0], 'mountpoint': parts[1],
                            'fstype': parts[2], 'options': parts[3]}
                    # Add disk space info
                    try:
                        st = os.statvfs(parts[1])
                        total = st.f_blocks * st.f_frsize
                        free  = st.f_bavail * st.f_frsize
                        used  = total - free
                        info['total_gb']  = round(total / (1024**3), 1)
                        info['used_gb']   = round(used  / (1024**3), 1)
                        info['free_gb']   = round(free  / (1024**3), 1)
                        info['pct_used']  = int(used / total * 100) if total > 0 else 0
                    except Exception as e:
                        print(f"ERROR: failed to get disk space for {parts[1]}: {e}", file=sys.stderr)
                        info.update({'total_gb': 0, 'used_gb': 0, 'free_gb': 0, 'pct_used': 0})
                    mounts.append(info)
    except (FileNotFoundError, PermissionError):
        pass
    return mounts


def mount_nfs(server, export, mountpoint, nfs_version='4', extra_opts=''):
    """Mount an NFS share (Linux only)."""
    if not IS_LINUX:
        raise RuntimeError('NFS mounting is only supported on Linux')
    os.makedirs(mountpoint, exist_ok=True)
    opts = []
    if nfs_version:
        opts.append(f'vers={nfs_version}')
    if extra_opts:
        opts.append(extra_opts.strip())
    cmd = ['mount', '-t', 'nfs']
    if opts:
        cmd += ['-o', ','.join(opts)]
    cmd += [f'{server}:{export}', mountpoint]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or 'mount failed').strip())


def umount_nfs(mountpoint):
    """Unmount an NFS share (Linux only)."""
    if not IS_LINUX:
        raise RuntimeError('NFS unmounting is only supported on Linux')
    result = subprocess.run(['umount', mountpoint], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or 'umount failed').strip())


# ── Helpers ───────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('host'):
            flash('Please log in first.', 'info')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts else '—'


def job_to_display(jid, info):
    """Convert internal job dict to template-friendly dict."""
    disk_filter = info.get('disk_filter')
    vm_names = info.get('vm_names')
    if vm_names:
        vm_display = f"{len(vm_names)} VMs ({', '.join(vm_names[:3])}{'...' if len(vm_names) > 3 else ''})"
    else:
        vm_display = info.get('vm_name', '—')
    return {
        'id':            jid,
        'label':         info.get('label', ''),
        'vm_name':       vm_display,
        'status':        info.get('status', 'unknown'),
        'started_fmt':   fmt_time(info.get('started')),
        'dest':          info.get('dest', ''),
        'run_dest':      info.get('run_dest', ''),
        'replication_dest': info.get('replication_dest', ''),
        'compress':      info.get('compress', False),
        'sftp_host':     info.get('sftp_host', ''),
        'schedule_type': info.get('schedule_type', 'now'),
        'schedule_time': info.get('schedule_time', ''),
        'schedule_id':   info.get('schedule_id'),
        'disk_filter':   disk_filter,
        'disks_count':   len(disk_filter) if disk_filter is not None else None,
        'retention_type':  info.get('retention_type', 'keep_all'),
        'retention_value': info.get('retention_value', 5),
        'monthly_day':     info.get('monthly_day'),
        'yearly_month':    info.get('yearly_month'),
        'weekly_day':      info.get('weekly_day'),
        'vm_names':        vm_names,
        'use_cbt':         info.get('use_cbt', False),
    }


def enforce_retention_policy(info, log_path=None):
    def log_msg(msg):
        print(msg)
        if log_path:
            try:
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(f"[Retention] {msg}\n")
            except Exception:
                pass

    retention_type = info.get('retention_type', 'keep_all')
    retention_val = info.get('retention_value', 5)

    vm_name = info.get('vm_name')
    parent_dest = info.get('dest')
    if not vm_name or not parent_dest:
        return

    vm_dir = os.path.join(parent_dest, vm_name)
    if not os.path.exists(vm_dir):
        return

    try:
        subdirs = []
        failed_dirs = []
        for name in os.listdir(vm_dir):
            path = os.path.join(vm_dir, name)
            if os.path.isdir(path) and name.startswith('backup-'):
                # Check for manifest.json to verify successful completion
                manifest_file = os.path.join(path, 'manifest.json')
                if os.path.exists(manifest_file):
                    subdirs.append((name, path))
                else:
                    failed_dirs.append((name, path))

        # Always clean up failed/incomplete backup directories immediately
        if failed_dirs:
            log_msg(f"Found {len(failed_dirs)} failed/incomplete backup directory(ies). Cleaning up...")
            for name, path in failed_dirs:
                try:
                    import shutil
                    shutil.rmtree(path)
                    log_msg(f"Cleaned up failed backup directory: {name}")
                except Exception as e:
                    log_msg(f"ERROR cleaning up failed backup {name}: {e}")

        # If retention is keep_all, we don't prune successful backups, but we did clean up failed ones
        if retention_type == 'keep_all':
            return

        # Sort successful backups chronologically by folder name (backup-YYYYMMDDHHMMSS)
        subdirs.sort(key=lambda x: x[0])

        if retention_type == 'keep_count':
            if len(subdirs) > retention_val:
                to_delete = subdirs[:-retention_val]
                log_msg(f"Enforcing count retention (keep {retention_val}). Deleting {len(to_delete)} old successful backup(s)...")
                for name, path in to_delete:
                    try:
                        import shutil
                        shutil.rmtree(path)
                        log_msg(f"Deleted old backup directory: {name}")
                    except Exception as e:
                        log_msg(f"ERROR deleting {name}: {e}")

        elif retention_type == 'keep_days':
            import shutil
            cutoff_time = time.time() - (retention_val * 86400)
            deleted_count = 0
            for name, path in subdirs:
                try:
                    ts_str = name[7:]
                    dt = datetime.strptime(ts_str[:14], '%Y%m%d%H%M%S')
                    folder_time = dt.timestamp()
                except Exception:
                    folder_time = os.path.getmtime(path)

                if folder_time < cutoff_time:
                    try:
                        shutil.rmtree(path)
                        log_msg(f"Deleted backup older than {retention_val} days: {name}")
                        deleted_count += 1
                    except Exception as e:
                        log_msg(f"ERROR deleting {name}: {e}")
            if deleted_count > 0:
                log_msg(f"Enforced age retention. Deleted {deleted_count} backups.")

    except Exception as e:
        log_msg(f"ERROR during retention cleanup: {e}")


def replicate_backup_folder(src_dir, dest_dir, log_path=None):
    """
    Copy all files from primary backup folder to replication target folder,
    then verify checksums.
    """
    def log_msg(msg):
        print(msg)
        if log_path:
            try:
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(f"[Replication] {msg}\n")
            except Exception:
                pass

    log_msg(f"Starting replication from '{src_dir}' to '{dest_dir}'...")
    if not os.path.exists(src_dir):
        log_msg(f"ERROR: Source directory '{src_dir}' does not exist.")
        return False

    try:
        os.makedirs(dest_dir, exist_ok=True)
        import shutil
        for item in os.listdir(src_dir):
            s = os.path.join(src_dir, item)
            d = os.path.join(dest_dir, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        log_msg(f"Replication file copy completed successfully.")
        
        # Verify checksums on replication target folder
        log_msg("Verifying checksums on replication target...")
        from backup_core import verify_backup_checksums
        if verify_backup_checksums(dest_dir):
            log_msg("Replication verification OK: all SHA-256 checksums match.")
            return True
        else:
            log_msg("WARNING: Replication verification FAILED on target. Checksums do not match.")
            return False
    except Exception as e:
        log_msg(f"ERROR during replication: {e}")
        return False


def run_job_thread(jid):
    """Worker executed in a thread (and by APScheduler)."""
    with jobs_db_lock:
        info = jobs.get(jid)
        if not info:
            return
        info['status']   = 'running'
        info['started']  = time.time()
        info['progress'] = {'pct': 0, 'phase': 'starting', 'detail': 'Initializing…'}
        save_jobs_db()
    
    def is_cancelled():
        with jobs_db_lock:
            return jobs.get(jid, {}).get('status') == 'cancelling'
    
    vm_names = info.get('vm_names')
    log_path = str(JOBS_DIR / jid / 'backup.log')
    
    if vm_names:
        # Grouped/Batch VM backup run
        total_vms = len(vm_names)
        with jobs_db_lock:
            info['run_dest'] = os.path.join(info['dest'], f"batch-{datetime.fromtimestamp(info['started']).strftime('%Y%m%d%H%M%S')}")
            save_jobs_db()
        
        success_vms = []
        failed_vms = []
        
        for idx, vm in enumerate(vm_names):
            if is_cancelled():
                with jobs_db_lock:
                    failed_vms.append((vm, "Cancelled by user"))
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(f"\nSkipping VM {idx+1}/{total_vms} ({vm}): Backup cancelled by user\n")
                continue
                
            vm_pct_start = int((idx / total_vms) * 100)
            vm_pct_end = int(((idx + 1) / total_vms) * 100)
            
            def make_vm_progress_cb(vm_n, start_p, end_p, vm_idx, total):
                def _cb(prog):
                    prog_pct = prog.get('pct', 0)
                    overall_pct = start_p + int((prog_pct / 100) * (end_p - start_p))
                    with jobs_db_lock:
                        info['progress'] = {
                            'pct': overall_pct,
                            'phase': f'vm {vm_idx+1}/{total} ({vm_n})',
                            'detail': f"[{vm_n}] {prog.get('phase', '')}: {prog.get('detail', '')}"
                        }
                return _cb
            
            try:
                # Log separator in log file
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(f"\n{'='*50}\n")
                    f.write(f"Starting Backup for VM {idx+1}/{total_vms}: {vm}\n")
                    f.write(f"{'='*50}\n\n")
                
                # Create run-specific destination folder for this VM under the batch folder
                run_timestamp = datetime.fromtimestamp(info['started']).strftime('%Y%m%d%H%M%S')
                vm_dest = os.path.join(info['dest'], vm, f"backup-{run_timestamp}")
                
                # Resolve disk filter for this specific VM from disk_filter_map
                disk_filter = info.get('disk_filter_map', {}).get(vm)
                
                run_backup(
                    host=info['host'],
                    user=info['user'],
                    password=info['password'],
                    vm_name=vm,
                    dest=vm_dest,
                    compress=info.get('compress', False),
                    no_verify_ssl=info.get('no_verify_ssl', False),
                    sftp_host=info.get('sftp_host') or None,
                    sftp_user=info.get('sftp_user') or None,
                    sftp_password=info.get('sftp_password') or None,
                    sftp_key=None,
                    log_path=log_path,
                    progress_cb=make_vm_progress_cb(vm, vm_pct_start, vm_pct_end, idx, total_vms),
                    disk_filter=disk_filter,
                    job_id=jid,
                    is_cancelled_cb=is_cancelled,
                    use_cbt=info.get('use_cbt', False),
                )
                with jobs_db_lock:
                    success_vms.append(vm)
                
                # Replicate successful backup if replication_dest is configured
                rep_dest = info.get('replication_dest')
                if rep_dest:
                    rep_vm_dest = os.path.join(rep_dest, vm, f"backup-{run_timestamp}")
                    replicate_backup_folder(vm_dest, rep_vm_dest, log_path=log_path)
            except Exception as e:
                is_cancel_err = "cancelled by user" in str(e).lower()
                if is_cancel_err:
                    with jobs_db_lock:
                        failed_vms.append((vm, "Cancelled by user"))
                        info['status'] = 'failed (Cancelled)'
                        info['progress'] = {'pct': 100, 'phase': 'failed', 'detail': 'Backup cancelled by user'}
                        save_jobs_db()
                    break
                else:
                    with jobs_db_lock:
                        failed_vms.append((vm, str(e)))
                    with open(log_path, 'a', encoding='utf-8') as f:
                        f.write(f"\nERROR backing up VM {vm}: {e}\n\n")
            finally:
                # Always enforce retention policy (which cleans up failed folders immediately)
                vm_info = {
                    'vm_name': vm,
                    'dest': info['dest'],
                    'retention_type': info.get('retention_type', 'keep_all'),
                    'retention_value': info.get('retention_value', 5)
                }
                enforce_retention_policy(vm_info, log_path=log_path)
                
                # Enforce retention policy on replication target if configured
                if info.get('replication_dest'):
                    rep_vm_info = {
                        'vm_name': vm,
                        'dest': info['replication_dest'],
                        'retention_type': info.get('retention_type', 'keep_all'),
                        'retention_value': info.get('retention_value', 5)
                    }
                    enforce_retention_policy(rep_vm_info, log_path=log_path)
        
        with jobs_db_lock:
            if failed_vms:
                if success_vms:
                    info['status'] = f"finished with errors (Failed: {', '.join([f[0] for f in failed_vms])})"
                else:
                    info['status'] = f"failed (All backups failed)"
            else:
                info['status'] = 'finished'
                
            info['progress'] = {
                'pct': 100,
                'phase': 'done',
                'detail': f"Batch completed. Success: {len(success_vms)}, Failed: {len(failed_vms)}"
            }
            save_jobs_db()
        
    else:
        # Single VM backup run (original behavior)
        run_timestamp = datetime.fromtimestamp(info['started']).strftime('%Y%m%d%H%M%S')
        run_dest = os.path.join(info['dest'], info['vm_name'], f"backup-{run_timestamp}")
        with jobs_db_lock:
            info['run_dest'] = run_dest
            save_jobs_db()

        def progress_cb(prog):
            with jobs_db_lock:
                info['progress'] = prog

        try:
            run_backup(
                host=info['host'],
                user=info['user'],
                password=info['password'],
                vm_name=info['vm_name'],
                dest=run_dest,
                compress=info.get('compress', False),
                no_verify_ssl=info.get('no_verify_ssl', False),
                sftp_host=info.get('sftp_host') or None,
                sftp_user=info.get('sftp_user') or None,
                sftp_password=info.get('sftp_password') or None,
                sftp_key=None,
                log_path=log_path,
                progress_cb=progress_cb,
                disk_filter=info.get('disk_filter'),  # None = all disks
                job_id=jid,
                is_cancelled_cb=is_cancelled,
                use_cbt=info.get('use_cbt', False),
            )
            with jobs_db_lock:
                info['status']   = 'finished'
                info['progress'] = {'pct': 100, 'phase': 'done', 'detail': 'Backup completed successfully'}
                save_jobs_db()
            
            # Replicate successful backup if replication_dest is configured
            rep_dest = info.get('replication_dest')
            if rep_dest:
                rep_run_dest = os.path.join(rep_dest, info['vm_name'], f"backup-{run_timestamp}")
                replicate_backup_folder(run_dest, rep_run_dest, log_path=log_path)
        except Exception as e:
            with jobs_db_lock:
                if "cancelled by user" in str(e).lower():
                    info['status'] = 'failed (Cancelled)'
                    info['progress'] = {'pct': 100, 'phase': 'failed', 'detail': 'Backup cancelled by user'}
                else:
                    info['status'] = f'failed ({e})'
                save_jobs_db()
        finally:
            # Always enforce retention policy (which cleans up failed folders immediately)
            enforce_retention_policy(info, log_path=log_path)
            
            # Enforce retention policy on replication target if configured
            if info.get('replication_dest'):
                rep_info = {
                    'vm_name': info['vm_name'],
                    'dest': info['replication_dest'],
                    'retention_type': info.get('retention_type', 'keep_all'),
                    'retention_value': info.get('retention_value', 5)
                }
                enforce_retention_policy(rep_info, log_path=log_path)


def create_and_start_job(
    vm_name, dest, compress, no_verify_ssl,
    sftp_host, sftp_user, sftp_password,
    schedule_type, schedule_time, weekly_day, interval_hours,
    label='', disk_filter=None, monthly_day=1, yearly_month=1,
    retention_type='keep_all', retention_value=5,
    vm_names=None, disk_filter_map=None, use_cbt=False,
    replication_dest=None
):
    """Create a job entry and either run immediately or register schedule.
    disk_filter: list of VMDK path strings to include, or None for all.
    monthly_day: day of month (1-28) for monthly schedule.
    use_cbt: if True enable Changed Block Tracking incremental backup.
    """
    jid = datetime.now().strftime('%Y%m%d%H%M%S') + '-' + uuid.uuid4().hex[:6]
    job_dir = JOBS_DIR / jid
    job_dir.mkdir(parents=True, exist_ok=True)

    info = {
        'id':            jid,
        'label':         label,
        'host':          session['host'],
        'user':          session['user'],
        'password':      session['password'],
        'vm_name':       vm_name,
        'vm_names':      vm_names,
        'disk_filter_map': disk_filter_map,
        'dest':          dest,
        'replication_dest': replication_dest,
        'compress':      compress,
        'no_verify_ssl': no_verify_ssl,
        'sftp_host':     sftp_host,
        'sftp_user':     sftp_user,
        'sftp_password': sftp_password,
        'started':       time.time(),
        'status':        'queued',
        'schedule_type': schedule_type,
        'schedule_time': schedule_time,
        'schedule_id':   None,
        'disk_filter':   disk_filter,  # None = back up all disks
        'weekly_day':    weekly_day,
        'monthly_day':   monthly_day,
        'yearly_month':  yearly_month,
        'interval_hours': interval_hours,
        'retention_type':  retention_type,
        'retention_value': retention_value,
        'use_cbt':         use_cbt,
    }
    with jobs_db_lock:
        jobs[jid] = info

        if schedule_type == 'now' or not HAS_SCHEDULER:
            t = threading.Thread(target=run_job_thread, args=(jid,), daemon=True)
            t.start()
        else:
            sched_id = register_scheduler_job(info)
            if sched_id:
                info['schedule_id'] = sched_id
                info['status'] = 'scheduled'
            else:
                # Fallback: run now
                t = threading.Thread(target=run_job_thread, args=(jid,), daemon=True)
                t.start()

        save_jobs_db()
    return jid


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if session.get('host'):
        return redirect(url_for('vms'))
    return redirect(url_for('login'))


# ── Login / Logout ────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        host          = request.form.get('host', '').strip()
        user          = request.form.get('user', '').strip()
        password      = request.form.get('password', '')
        no_verify_ssl = 'no_verify_ssl' in request.form

        if not (host and user and password):
            flash('Host, username and password are required.', 'danger')
            return render_template('login.html')

        # Verify credentials — also warms the VM cache for instant /vms load
        vm_list, error, _ = _fetch_and_cache(host, user, password, no_verify_ssl)
        if error and not vm_list:
            flash(f'Connection failed: {error}', 'danger')
            return render_template('login.html')

        session['host']          = host
        session['user']          = user
        session['password']      = password
        session['no_verify_ssl'] = no_verify_ssl
        flash(f'Connected to {host} — {len(vm_list)} VMs found.', 'success')
        return redirect(url_for('vms'))

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))


# ── VM Browser ────────────────────────────────────────────────────────────────

@app.route('/vms')
@login_required
def vms():
    force = request.args.get('refresh') == '1'
    vm_list, error, cache_ts = get_cached_vms(
        session['host'], session['user'], session['password'],
        no_verify_ssl=session.get('no_verify_ssl', False),
        force=force,
    )
    cache_age = int(time.time() - cache_ts) if cache_ts else None
    return render_template('vms.html', vms=vm_list, error=error, cache_age=cache_age)


@app.route('/api/vms')
@login_required
def api_vms():
    force = request.args.get('refresh') == '1'
    vm_list, error, cache_ts = get_cached_vms(
        session['host'], session['user'], session['password'],
        no_verify_ssl=session.get('no_verify_ssl', False),
        force=force,
    )
    if error and not vm_list:
        return jsonify({'error': error}), 500
    return jsonify({'vms': vm_list, 'cache_age': int(time.time() - cache_ts) if cache_ts else None})


@app.route('/api/vm/<vm_name>/disks')
@login_required
def api_vm_disks(vm_name):
    """Return disk list for a specific VM (from cache)."""
    vm_list, error, _ = get_cached_vms(
        session['host'], session['user'], session['password'],
        no_verify_ssl=session.get('no_verify_ssl', False)
    )
    for vm in vm_list:
        if vm['name'] == vm_name:
            return jsonify(vm.get('disks', []))
    return jsonify({'error': f'VM "{vm_name}" not found'}), 404


# ── Create Job ────────────────────────────────────────────────────────────────

@app.route('/jobs/create', methods=['GET', 'POST'])
@login_required
def create_job():
    if request.method == 'POST':
        vm_name          = request.form.get('vm_name', '').strip()
        dest             = request.form.get('dest', './backups').strip()
        replication_dest = request.form.get('replication_dest', '').strip() or None
        compress         = 'compress' in request.form
        no_verify_ssl    = 'no_verify_ssl' in request.form
        sftp_host        = request.form.get('sftp_host', '').strip() or None
        sftp_user        = request.form.get('sftp_user', '').strip() or None
        sftp_password    = request.form.get('sftp_password', '') or None
        schedule_type    = request.form.get('schedule_type', 'now')
        daily_time       = request.form.get('daily_time', '02:00')
        weekly_day       = request.form.get('weekly_day', '0')
        weekly_time      = request.form.get('weekly_time', '02:00')
        
        monthly_basis = request.form.get('monthly_basis', 'day_num')
        if monthly_basis == 'weekday':
            monthly_week_num = request.form.get('monthly_week_num', '1st')
            monthly_day_of_week = request.form.get('monthly_day_of_week', 'sun')
            monthly_day = f"{monthly_week_num} {monthly_day_of_week}"
            monthly_time = request.form.get('monthly_time_2', '02:00')
        else:
            monthly_day = request.form.get('monthly_day', '1')
            monthly_time = request.form.get('monthly_time_1', '02:00')
            
        yearly_month  = request.form.get('yearly_month', '1')
        interval_hrs  = request.form.get('interval_hours', '24')
        label         = request.form.get('job_label', '').strip()

        if not vm_name:
            flash('Please select a virtual machine.', 'danger')
            return redirect(url_for('create_job'))

        # Determine schedule_time string for display
        if schedule_type == 'daily':
            sched_time = daily_time
        elif schedule_type == 'weekly':
            sched_time = weekly_time
        elif schedule_type in ('monthly', '3_monthly', '6_monthly', 'yearly'):
            sched_time = monthly_time
        else:
            sched_time = ''

        # disk_filter: None = all disks; list = selected disks only
        disk_selection_shown = 'disk_selection_shown' in request.form
        if disk_selection_shown:
            raw_filter = request.form.getlist('disk_filter')
            disk_filter = raw_filter if raw_filter else None
        else:
            disk_filter = None  # disks not shown yet = backup all

        retention_type = request.form.get('retention_type', 'keep_all')
        try:
            retention_value = int(request.form.get('retention_value', '5'))
        except ValueError:
            retention_value = 5

        use_cbt = request.form.get('use_cbt') == '1'

        jid = create_and_start_job(
            vm_name=vm_name,
            dest=dest,
            compress=compress,
            no_verify_ssl=no_verify_ssl,
            sftp_host=sftp_host,
            sftp_user=sftp_user,
            sftp_password=sftp_password,
            schedule_type=schedule_type,
            schedule_time=sched_time,
            weekly_day=weekly_day,
            interval_hours=interval_hrs,
            label=label,
            disk_filter=disk_filter,
            monthly_day=monthly_day,
            yearly_month=yearly_month,
            retention_type=retention_type,
            retention_value=retention_value,
            use_cbt=use_cbt,
            replication_dest=replication_dest
        )
        n_disks = len(disk_filter) if disk_filter is not None else 'all'
        flash(f'Job created — {n_disks} disk(s) selected.', 'success')
        return redirect(url_for('job_detail', jobid=jid))


    # GET: load VM list for the dropdown
    selected_vm   = request.args.get('vm', '')
    show_schedule = bool(request.args.get('schedule', ''))
    vm_list, error, _ = get_cached_vms(
        session['host'], session['user'], session['password'],
        no_verify_ssl=session.get('no_verify_ssl', False)
    )
    if error and not vm_list:
        flash(f'Could not load VM list: {error}', 'danger')
    # Sort alphabetically for the dropdown
    vm_list = sorted(vm_list, key=lambda v: v['name'].lower())

    from datetime import datetime
    return render_template(
        'create_job.html',
        vms=vm_list,
        selected_vm=selected_vm,
        show_schedule=show_schedule,
        current_month=datetime.now().month,
    )


# ── Batch Jobs ────────────────────────────────────────────────────────────────

@app.route('/jobs/batch', methods=['GET', 'POST'])
@login_required
def batch_jobs():
    vm_list, _, _ = get_cached_vms(
        session['host'], session['user'], session['password'],
        no_verify_ssl=session.get('no_verify_ssl', False)
    )
    vms_by_name = {v['name']: v for v in vm_list}

    if request.method == 'POST':
        vm_names         = request.form.getlist('vms')
        dest             = request.form.get('dest', './backups').strip()
        replication_dest = request.form.get('replication_dest', '').strip() or None
        compress         = 'compress' in request.form
        no_verify_ssl    = 'no_verify_ssl' in request.form
        disk_strategy    = request.form.get('disk_strategy', 'all')
        schedule_type    = request.form.get('schedule_type', 'now')
        daily_time       = request.form.get('daily_time', '02:00')
        weekly_day       = request.form.get('weekly_day', '0')
        weekly_time      = request.form.get('weekly_time', '02:00')
        
        monthly_basis = request.form.get('monthly_basis', 'day_num')
        if monthly_basis == 'weekday':
            monthly_week_num = request.form.get('monthly_week_num', '1st')
            monthly_day_of_week = request.form.get('monthly_day_of_week', 'sun')
            monthly_day = f"{monthly_week_num} {monthly_day_of_week}"
            monthly_time = request.form.get('monthly_time_2', '02:00')
        else:
            monthly_day = request.form.get('monthly_day', '1')
            monthly_time = request.form.get('monthly_time_1', '02:00')
            
        yearly_month  = request.form.get('yearly_month', '1')
        interval_hrs  = request.form.get('interval_hours', '24')
        label_prefix  = request.form.get('job_label', '').strip()

        if schedule_type == 'daily':
            sched_time = daily_time
        elif schedule_type == 'weekly':
            sched_time = weekly_time
        elif schedule_type in ('monthly', '3_monthly', '6_monthly', 'yearly'):
            sched_time = monthly_time
        else:
            sched_time = ''

        if not vm_names:
            flash('No VMs selected.', 'danger')
            return redirect(url_for('vms'))

        disk_filter_map = {}
        for vm_name in vm_names:
            if disk_strategy == 'os':
                vm_info = vms_by_name.get(vm_name, {})
                disks = sorted(vm_info.get('disks', []), key=lambda d: d.get('size_gb', 0))
                disk_filter = [disks[0]['path']] if disks else None
            elif disk_strategy == 'vmx':
                disk_filter = []
            else:
                disk_filter = None
            disk_filter_map[vm_name] = disk_filter

        retention_type = request.form.get('retention_type', 'keep_all')
        try:
            retention_value = int(request.form.get('retention_value', '5'))
        except ValueError:
            retention_value = 5

        use_cbt = request.form.get('use_cbt') == '1'

        label = label_prefix if label_prefix else f"Batch Backup — {len(vm_names)} VMs"

        jid = create_and_start_job(
            vm_name=None,
            dest=dest,
            compress=compress,
            no_verify_ssl=no_verify_ssl,
            sftp_host=None,
            sftp_user=None,
            sftp_password=None,
            schedule_type=schedule_type,
            schedule_time=sched_time,
            weekly_day=weekly_day,
            interval_hours=interval_hrs,
            label=label,
            disk_filter=None,
            monthly_day=monthly_day,
            yearly_month=yearly_month,
            retention_type=retention_type,
            retention_value=retention_value,
            vm_names=vm_names,
            disk_filter_map=disk_filter_map,
            use_cbt=use_cbt,
            replication_dest=replication_dest
        )

        strat_label = {'all': 'all disks', 'os': 'OS disk only', 'vmx': 'VMX config only'}.get(disk_strategy, disk_strategy)
        flash(f'Batch backup job created for {len(vm_names)} VMs ({strat_label}).', 'success')
        return redirect(url_for('list_jobs'))


    # GET: show batch config form
    vm_names = request.args.getlist('vms')
    if not vm_names:
        flash('No VMs specified for batch backup.', 'danger')
        return redirect(url_for('vms'))

    from datetime import datetime
    return render_template(
        'batch_job.html',
        vm_names=vm_names,
        vms_by_name=vms_by_name,
        current_month=datetime.now().month,
    )


# ── Jobs Dashboard ────────────────────────────────────────────────────────────

@app.route('/jobs')
@login_required
def list_jobs():
    with jobs_db_lock:
        sorted_items = sorted(jobs.items(), key=lambda x: x[1].get('started', 0), reverse=True)
        job_list = [
            job_to_display(jid, info)
            for jid, info in sorted_items
        ]
    scheduled_count = sum(1 for j in job_list if j['schedule_id'])
    return render_template('jobs.html', jobs=job_list, scheduled_count=scheduled_count)


# ── Job Detail ────────────────────────────────────────────────────────────────

@app.route('/job/<jobid>')
@login_required
def job_detail(jobid):
    with jobs_db_lock:
        info = jobs.get(jobid)
        if not info:
            abort(404)
        job_disp = job_to_display(jobid, info)
    return render_template('job_detail.html', job=job_disp)


@app.route('/job/<jobid>/log')
@login_required
def job_log(jobid):
    with jobs_db_lock:
        info = jobs.get(jobid)
    if not info:
        abort(404)
    log_path = JOBS_DIR / jobid / 'backup.log'
    if not log_path.exists():
        return '(No log output yet)', 200
    with open(log_path, 'rb') as f:
        lines = f.read().splitlines()[-300:]
    return '\n'.join(line.decode('utf-8', errors='replace') for line in lines)


@app.route('/api/job/<jobid>/status')
@login_required
def api_job_status(jobid):
    with jobs_db_lock:
        info = jobs.get(jobid)
        if not info:
            return jsonify({'error': 'not found'}), 404
        status = info.get('status', 'unknown')
        progress = info.get('progress', {'pct': 0, 'phase': '', 'detail': ''})
    return jsonify({
        'status':   status,
        'id':       jobid,
        'progress': progress,
    })


@app.route('/job/<jobid>/cancel-schedule', methods=['POST'])
@login_required
def cancel_schedule(jobid):
    with jobs_db_lock:
        info = jobs.get(jobid)
        if not info:
            abort(404)
        sched_id = info.get('schedule_id')
        if sched_id and scheduler:
            try:
                scheduler.remove_job(sched_id)
            except Exception:
                pass
        info['schedule_id'] = None
        info['status'] = info.get('status', 'finished') if info.get('status') not in ('queued', 'running') else info['status']
        save_jobs_db()
    flash('Recurring schedule cancelled.', 'success')
    return redirect(url_for('job_detail', jobid=jobid))


@app.route('/job/<jobid>/reactivate-schedule', methods=['POST'])
@login_required
def reactivate_schedule(jobid):
    with jobs_db_lock:
        info = jobs.get(jobid)
        if not info:
            abort(404)
        if not info.get('schedule_type') or info.get('schedule_type') == 'now':
            flash('This job does not have a recurring schedule configured.', 'danger')
            return redirect(url_for('job_detail', jobid=jobid))
        if info.get('schedule_id'):
            flash('Schedule is already active.', 'warning')
            return redirect(url_for('job_detail', jobid=jobid))
        sched_id = register_scheduler_job(info)
        if sched_id:
            info['schedule_id'] = sched_id
            if info.get('status') not in ('running', 'queued'):
                info['status'] = 'scheduled'
            save_jobs_db()
            flash('Recurring schedule reactivated successfully.', 'success')
        else:
            flash('Failed to reactivate schedule.', 'danger')
    return redirect(url_for('job_detail', jobid=jobid))


@app.route('/job/<jobid>/run', methods=['POST'])
@login_required
def run_job_now(jobid):
    with jobs_db_lock:
        info = jobs.get(jobid)
        if not info:
            abort(404)
        if info.get('status') in ('running', 'queued'):
            flash('Backup is already running or queued.', 'warning')
            return redirect(url_for('job_detail', jobid=jobid))
        
        # Mark status as queued atomically to prevent double run race condition
        info['status'] = 'queued'
        save_jobs_db()
    
    # Start backup execution in a background thread
    t = threading.Thread(target=run_job_thread, args=(jobid,), daemon=True)
    t.start()
    flash('Backup triggered successfully and is running in the background.', 'success')
    return redirect(url_for('job_detail', jobid=jobid))


@app.route('/job/<jobid>/stop', methods=['POST'])
@login_required
def stop_job(jobid):
    with jobs_db_lock:
        info = jobs.get(jobid)
        if not info:
            abort(404)
        if info.get('status') in ('running', 'queued'):
            info['status'] = 'cancelling'
            info['progress'] = {'pct': info.get('progress', {}).get('pct', 0), 'phase': 'cancelling', 'detail': 'Stopping backup execution…'}
            save_jobs_db()
            flash('Request to stop backup sent.', 'info')
        else:
            flash('Job is not running or queued.', 'warning')
    return redirect(url_for('job_detail', jobid=jobid))


@app.route('/job/<jobid>/delete', methods=['POST'])
@login_required
def delete_job(jobid):
    with jobs_db_lock:
        info = jobs.get(jobid)
        if not info:
            abort(404)
        
        # Cancel schedule first if it exists
        sched_id = info.get('schedule_id')
        if sched_id and scheduler:
            try:
                scheduler.remove_job(sched_id)
            except Exception:
                pass
                
        # Remove from jobs dict
        jobs.pop(jobid, None)
        save_jobs_db()
    
    # Remove the job directory containing the log file
    import shutil
    job_dir = JOBS_DIR / jobid
    if job_dir.exists():
        try:
            shutil.rmtree(job_dir)
        except Exception as e:
            print(f"ERROR: Failed to delete job directory {job_dir}: {e}", file=sys.stderr)
            
    flash('Job and its schedule deleted successfully.', 'success')
    return redirect(url_for('list_jobs'))


@app.route('/job/<jobid>/edit', methods=['GET', 'POST'])
@login_required
def edit_job(jobid):
    with jobs_db_lock:
        info = jobs.get(jobid)
        if not info:
            abort(404)
        if info.get('status') in ('running', 'queued'):
            flash('Cannot edit a running or queued job.', 'danger')
            return redirect(url_for('job_detail', jobid=jobid))

        if request.method == 'POST':
            dest             = request.form.get('dest', './backups').strip()
            replication_dest = request.form.get('replication_dest', '').strip() or None
            compress         = 'compress' in request.form
            no_verify_ssl    = 'no_verify_ssl' in request.form
            schedule_type    = request.form.get('schedule_type', 'now')
            daily_time       = request.form.get('daily_time', '02:00')
            weekly_day       = request.form.get('weekly_day', '0')
            weekly_time      = request.form.get('weekly_time', '02:00')

            monthly_basis = request.form.get('monthly_basis', 'day_num')
            if monthly_basis == 'weekday':
                monthly_week_num = request.form.get('monthly_week_num', '1st')
                monthly_day_of_week = request.form.get('monthly_day_of_week', 'sun')
                monthly_day = f"{monthly_week_num} {monthly_day_of_week}"
                monthly_time = request.form.get('monthly_time_2', '02:00')
            else:
                monthly_day = request.form.get('monthly_day', '1')
                monthly_time = request.form.get('monthly_time_1', '02:00')

            yearly_month  = request.form.get('yearly_month', '1')
            interval_hrs  = request.form.get('interval_hours', '24')
            label         = request.form.get('job_label', '').strip()

            if schedule_type == 'daily':
                sched_time = daily_time
            elif schedule_type == 'weekly':
                sched_time = weekly_time
            elif schedule_type in ('monthly', '3_monthly', '6_monthly', 'yearly'):
                sched_time = monthly_time
            else:
                sched_time = ''

            try:
                retention_value = int(request.form.get('retention_value', '5'))
            except ValueError:
                retention_value = 5
            retention_type = request.form.get('retention_type', 'keep_all')
            use_cbt = request.form.get('use_cbt') == '1'

            # Cancel old schedule if exists
            old_sched_id = info.get('schedule_id')
            if old_sched_id and scheduler:
                try:
                    scheduler.remove_job(old_sched_id)
                except Exception:
                    pass
            info['schedule_id'] = None

            # Update job config
            info['label'] = label
            info['dest'] = dest
            info['replication_dest'] = replication_dest
            info['compress'] = compress
            info['no_verify_ssl'] = no_verify_ssl
            info['use_cbt'] = use_cbt
            info['retention_type'] = retention_type
            info['retention_value'] = retention_value
            info['schedule_type'] = schedule_type
            info['schedule_time'] = sched_time
            info['weekly_day'] = weekly_day
            info['monthly_day'] = monthly_day
            info['yearly_month'] = yearly_month
            info['interval_hours'] = interval_hrs

            # Register new schedule if applicable
            if schedule_type != 'now' and HAS_SCHEDULER:
                new_sched_id = register_scheduler_job(info)
                if new_sched_id:
                    info['schedule_id'] = new_sched_id
                    info['status'] = 'scheduled'
                else:
                    info['status'] = 'finished'
            else:
                info['status'] = 'finished' if info.get('status') == 'scheduled' else info.get('status', 'finished')

            save_jobs_db()
            flash('Job updated successfully.', 'success')
            return redirect(url_for('job_detail', jobid=jobid))

        # GET: Display edit form
        job_disp = job_to_display(jobid, info)
        from datetime import datetime
        return render_template('edit_job.html', job=job_disp, raw_job=info, current_month=datetime.now().month)


# ── Template filter ───────────────────────────────────────────────────────────
@app.template_filter('startswith')
def startswith_filter(value, prefix):
    return str(value).startswith(prefix)


@app.after_request
def add_header(r):
    """Prevent caching of dynamic pages by the browser."""
    if not request.path.startswith('/static/'):
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        r.headers["Pragma"] = "no-cache"
        r.headers["Expires"] = "0"
    return r


# ── NFS Management Routes ─────────────────────────────────────────────────────────

@app.route('/nfs')
@login_required
def nfs_manager():
    mounts = list_nfs_mounts()
    return render_template('nfs.html', mounts=mounts, is_linux=IS_LINUX)


@app.route('/nfs/mount', methods=['POST'])
@login_required
def nfs_mount():
    server     = request.form.get('server', '').strip()
    export     = request.form.get('export', '').strip()
    mountpoint = request.form.get('mountpoint', '').strip()
    nfs_ver    = request.form.get('nfs_version', '4')
    extra_opts = request.form.get('extra_opts', '').strip()

    if not (server and export and mountpoint):
        flash('Server, export path, and mount point are required.', 'danger')
        return redirect(url_for('nfs_manager'))
    try:
        mount_nfs(server, export, mountpoint, nfs_version=nfs_ver, extra_opts=extra_opts)
        flash(f'Mounted {server}:{export} → {mountpoint} successfully.', 'success')
    except Exception as e:
        flash(f'Mount failed: {e}', 'danger')
    return redirect(url_for('nfs_manager'))


@app.route('/nfs/umount', methods=['POST'])
@login_required
def nfs_umount():
    mountpoint = request.form.get('mountpoint', '').strip()
    if not mountpoint:
        flash('Mount point is required.', 'danger')
        return redirect(url_for('nfs_manager'))
    try:
        umount_nfs(mountpoint)
        flash(f'Unmounted {mountpoint} successfully.', 'success')
    except Exception as e:
        flash(f'Unmount failed: {e}', 'danger')
    return redirect(url_for('nfs_manager'))


@app.route('/api/nfs')
@login_required
def api_nfs():
    return jsonify(list_nfs_mounts())


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
