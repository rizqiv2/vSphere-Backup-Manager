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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                job_label TEXT,
                vm_name TEXT,
                started REAL,
                ended REAL,
                duration REAL,
                status TEXT,
                size_bytes INTEGER,
                notification_sent INTEGER DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
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
        
        start_month = info.get('schedule_start_month')
        if not start_month:
            start_month = datetime.now().month
            info['schedule_start_month'] = start_month
            try:
                save_jobs_db()
            except Exception:
                pass
        
        months = sorted([(start_month + i * 3 - 1) % 12 + 1 for i in range(4)])
        trigger = CronTrigger(
            month=",".join(map(str, months)),
            day=day_val,
            hour=int(hour), minute=int(minute)
        )
    elif schedule_type == '6_monthly':
        hour, minute = (schedule_time.split(':') + ['00'])[:2]
        day_val = monthly_day
        if str(day_val).isdigit():
            day_val = max(1, min(28, int(day_val)))
            
        start_month = info.get('schedule_start_month')
        if not start_month:
            start_month = datetime.now().month
            info['schedule_start_month'] = start_month
            try:
                save_jobs_db()
            except Exception:
                pass
                
        months = sorted([(start_month + i * 6 - 1) % 12 + 1 for i in range(2)])
        trigger = CronTrigger(
            month=",".join(map(str, months)),
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


def get_dir_size(path):
    total = 0
    if not path:
        return total
    try:
        if os.path.exists(path):
            if os.path.isfile(path):
                return os.path.getsize(path)
            for dirpath, dirnames, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if not os.path.islink(fp):
                        total += os.path.getsize(fp)
    except Exception:
        pass
    return total


def fmt_duration(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    else:
        return f"{s}s"


def get_setting(key, default=None):
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row is not None:
            return row[0]
    except Exception:
        pass
    finally:
        conn.close()
    return default


def set_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
    except Exception as e:
        print(f"Error setting {key}: {e}", file=sys.stderr)
    finally:
        conn.close()


def send_webhook_notification(url, payload_type, run_data, raise_on_error=False, telegram_bot_token=None, telegram_chat_id=None):
    import requests
    
    if payload_type == 'telegram':
        token = (telegram_bot_token or get_setting('telegram_bot_token', '')).strip()
        chat_id = (telegram_chat_id or get_setting('telegram_chat_id', '')).strip()
        if not (token and chat_id):
            msg = "Telegram Bot Token and Chat ID are required for Telegram notifications."
            print(f"Telegram error: {msg}", file=sys.stderr)
            if raise_on_error:
                raise RuntimeError(msg)
            return
            
        size_gb = run_data['size_bytes'] / (1024 * 1024 * 1024)
        duration_str = fmt_duration(run_data['duration'])
        started_str = datetime.fromtimestamp(run_data['started']).strftime('%Y-%m-%d %H:%M:%S')
        status_text = run_data['status'].upper()
        
        status_emoji = "✅"
        if "failed" in run_data['status'].lower():
            status_emoji = "❌"
        elif "error" in run_data['status'].lower():
            status_emoji = "⚠️"
            
        title = f"<b>Backup Job {status_text}</b>"
        text = (
            f"{status_emoji} {title}\n\n"
            f"<b>Job:</b> {run_data['job_label'] or run_data['job_id'][:8]}\n"
            f"<b>VM(s):</b> {run_data['vm_name']}\n"
            f"<b>Size:</b> {size_gb:.2f} GB\n"
            f"<b>Duration:</b> {duration_str}\n"
            f"<b>Started:</b> {started_str}\n"
            f"<b>Status:</b> {run_data['status']}\n\n"
            f"<i>Job ID: {run_data['job_id']}</i>"
        )
        
        target_url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        
        try:
            r = requests.post(target_url, json=payload, timeout=15)
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else "Unknown"
            resp_text = e.response.text if e.response is not None else ""
            try:
                err_data = e.response.json()
                if 'description' in err_data:
                    msg = f"Telegram API error ({status_code}): {err_data['description']}"
                else:
                    msg = f"Telegram API error ({status_code}): {resp_text[:100]}"
            except Exception:
                msg = f"Telegram API error ({status_code}): {resp_text[:100] or e}"
            print(f"Telegram error: {msg}", file=sys.stderr)
            if raise_on_error:
                raise RuntimeError(msg) from e
        except Exception as e:
            msg = f"Failed to send Telegram message: {e}"
            print(f"Telegram error: {msg}", file=sys.stderr)
            if raise_on_error:
                raise RuntimeError(msg) from e
        return

    if not url:
        return
        
    size_gb = run_data['size_bytes'] / (1024 * 1024 * 1024)
    duration_str = fmt_duration(run_data['duration'])
    started_str = datetime.fromtimestamp(run_data['started']).strftime('%Y-%m-%d %H:%M:%S')
    
    status_text = run_data['status'].upper()
    color = "#10b981"
    if "failed" in run_data['status'].lower():
        color = "#ef4444"
    elif "error" in run_data['status'].lower():
        color = "#f59e0b"
        
    title = f"Backup Job {status_text}: {run_data['job_label'] or run_data['job_id'][:8]}"
    
    if payload_type == 'slack_discord':
        if "discord.com" in url:
            discord_color = 1096065
            if "failed" in run_data['status'].lower():
                discord_color = 15680580
            elif "error" in run_data['status'].lower():
                discord_color = 16096779
                
            payload = {
                "embeds": [{
                    "title": title,
                    "color": discord_color,
                    "fields": [
                        {"name": "VM(s)", "value": run_data['vm_name'], "inline": True},
                        {"name": "Duration", "value": duration_str, "inline": True},
                        {"name": "Backup Size", "value": f"{size_gb:.2f} GB", "inline": True},
                        {"name": "Status", "value": run_data['status'], "inline": True},
                        {"name": "Start Time", "value": started_str, "inline": True}
                    ],
                    "footer": {
                        "text": f"Job ID: {run_data['job_id']}"
                    }
                }]
            }
        else:
            payload = {
                "text": f"*{title}*",
                "attachments": [{
                    "color": color,
                    "fields": [
                        {"title": "VM(s)", "value": run_data['vm_name'], "short": True},
                        {"title": "Duration", "value": duration_str, "short": True},
                        {"title": "Backup Size", "value": f"{size_gb:.2f} GB", "short": True},
                        {"title": "Status", "value": run_data['status'], "short": True},
                        {"title": "Start Time", "value": started_str, "short": True},
                        {"title": "Job ID", "value": run_data['job_id'], "short": True}
                    ]
                }]
            }
    else:
        payload = run_data
        
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"Error sending webhook notification: {e}", file=sys.stderr)
        if raise_on_error:
            raise


def send_email_notification(smtp, run_data, raise_on_error=False):
    host = smtp.get('host')
    port = int(smtp.get('port') or 587)
    user = smtp.get('user')
    password = smtp.get('password')
    sender = smtp.get('sender')
    recipient = smtp.get('recipient')
    
    if not (host and sender and recipient):
        return
        
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    
    size_gb = run_data['size_bytes'] / (1024 * 1024 * 1024)
    duration_str = fmt_duration(run_data['duration'])
    started_str = datetime.fromtimestamp(run_data['started']).strftime('%Y-%m-%d %H:%M:%S')
    ended_str = datetime.fromtimestamp(run_data['ended']).strftime('%Y-%m-%d %H:%M:%S')
    
    status_text = run_data['status'].upper()
    
    theme_color = "#10b981"
    bg_banner = "linear-gradient(135deg, #10b981 0%, #059669 100%)"
    if "failed" in run_data['status'].lower():
        theme_color = "#ef4444"
        bg_banner = "linear-gradient(135deg, #ef4444 0%, #dc2626 100%)"
    elif "error" in run_data['status'].lower():
        theme_color = "#f59e0b"
        bg_banner = "linear-gradient(135deg, #f59e0b 0%, #d97706 100%)"
        
    subject = f"Backup Job {status_text}: {run_data['job_label'] or run_data['job_id'][:8]}"
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{
          font-family: 'Inter', system-ui, -apple-system, sans-serif;
          background-color: #080a10;
          color: #f8fafc;
          margin: 0; padding: 20px;
        }}
        .card {{
          background-color: #0e111a;
          border: 1px solid rgba(255, 255, 255, 0.05);
          border-radius: 12px;
          overflow: hidden;
          max-width: 600px;
          margin: 20px auto;
          box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
        }}
        .banner {{
          background: {bg_banner};
          color: #ffffff;
          padding: 24px;
          text-align: center;
        }}
        .banner h2 {{
          margin: 0; font-size: 20px; font-weight: 700;
        }}
        .content {{
          padding: 28px;
        }}
        .grid {{
          display: table;
          width: 100%;
          margin-bottom: 20px;
        }}
        .row {{
          display: table-row;
        }}
        .cell-lbl {{
          display: table-cell;
          padding: 8px 10px;
          font-weight: 600;
          color: #94a3b8;
          width: 30%;
          font-size: 13px;
        }}
        .cell-val {{
          display: table-cell;
          padding: 8px 10px;
          color: #f8fafc;
          font-size: 13.5px;
        }}
        .footer {{
          padding: 16px 28px;
          background-color: rgba(8, 10, 16, 0.4);
          border-top: 1px solid rgba(255, 255, 255, 0.05);
          font-size: 11px;
          color: #64748b;
          text-align: center;
        }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="banner">
          <h2>Backup Job: {status_text}</h2>
        </div>
        <div class="content">
          <div class="grid">
            <div class="row">
              <div class="cell-lbl">Job Label</div>
              <div class="cell-val">{run_data['job_label'] or '—'}</div>
            </div>
            <div class="row">
              <div class="cell-lbl">VM Name(s)</div>
              <div class="cell-val"><strong>{run_data['vm_name']}</strong></div>
            </div>
            <div class="row">
              <div class="cell-lbl">Size</div>
              <div class="cell-val">{size_gb:.2f} GB</div>
            </div>
            <div class="row">
              <div class="cell-lbl">Duration</div>
              <div class="cell-val">{duration_str}</div>
            </div>
            <div class="row">
              <div class="cell-lbl">Start Time</div>
              <div class="cell-val">{started_str}</div>
            </div>
            <div class="row">
              <div class="cell-lbl">End Time</div>
              <div class="cell-val">{ended_str}</div>
            </div>
            <div class="row">
              <div class="cell-lbl">Status</div>
              <div class="cell-val" style="color: {theme_color}; font-weight: 700;">{run_data['status']}</div>
            </div>
          </div>
        </div>
        <div class="footer">
          vSphere Backup Manager &middot; Job ID: {run_data['job_id']}
        </div>
      </div>
    </body>
    </html>
    """
    
    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(html, 'html'))
    
    encryption = smtp.get('encryption', 'starttls')
    try:
        import ssl

        # Build a permissive SSL context that accepts TLS 1.2+ and skips cert verification.
        # This covers old/self-signed mail servers while still using encrypted transport.
        # NOTE: TLSv1 and TLSv1_1 are disabled by default in modern OpenSSL builds, so
        # setting minimum_version to TLSv1_2 is the correct approach to avoid
        # [SSL: UNSUPPORTED_PROTOCOL] errors while remaining compatible with all
        # major SMTP providers (Gmail, Office365, Postfix, Exim, etc.)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2

        if encryption == 'ssl':
            # Direct SSL/TLS handshake (port 465)
            server = smtplib.SMTP_SSL(host, port, context=context, timeout=15)
        elif encryption == 'starttls':
            # Plain connection upgraded to TLS via STARTTLS (port 587)
            server = smtplib.SMTP(host, port, timeout=15)
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        else:
            # No encryption – plain SMTP relay (port 25 / internal)
            server = smtplib.SMTP(host, port, timeout=15)

        if user and password:
            server.login(user, password)

        server.sendmail(sender, recipient, msg.as_string())
        server.quit()
    except smtplib.SMTPAuthenticationError as e:
        msg = f"Authentication failed (code {e.smtp_code}): wrong username or password."
        print(f"Email error: {msg}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(msg) from e
    except smtplib.SMTPSenderRefused as e:
        msg = f"Sender address rejected by server (code {e.smtp_code}): {e.smtp_error.decode(errors='replace')}. Tip: Use port 587 + STARTTLS with your login credentials."
        print(f"Email error: {msg}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(msg) from e
    except smtplib.SMTPRecipientsRefused as e:
        # e.recipients is a dict: {addr: (code, msg_bytes)}
        details = "; ".join(
            f"{addr}: {err[1].decode(errors='replace')} (code {err[0]})"
            for addr, err in e.recipients.items()
        )
        msg = f"Server rejected recipient(s): {details}"
        print(f"Email error: {msg}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(msg) from e
    except smtplib.SMTPConnectError as e:
        msg = f"Could not connect to {host}:{port} — {e.smtp_error.decode(errors='replace') if isinstance(e.smtp_error, bytes) else e}"
        print(f"Email error: {msg}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(msg) from e
    except smtplib.SMTPException as e:
        msg = f"SMTP error: {e}"
        print(f"Email error: {msg}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(msg) from e
    except OSError as e:
        msg = f"Connection failed to {host}:{port} — {e}. Check that the host/port are reachable."
        print(f"Email error: {msg}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(msg) from e
    except Exception as e:
        print(f"Error sending email notification: {e}", file=sys.stderr)
        if raise_on_error:
            raise


def send_email_via_sendmail(cfg, run_data, raise_on_error=False):
    """Send email using the local sendmail binary (bypasses SMTP auth entirely).
    Mimics how PHP mail() or Nagios work on servers that have postfix/sendmail locally.
    """
    import subprocess
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    sendmail_path = cfg.get('sendmail_path', '/usr/sbin/sendmail').strip() or '/usr/sbin/sendmail'
    sender = cfg.get('sender', '').strip()
    recipient = cfg.get('recipient', '').strip()

    if not (sender and recipient):
        if raise_on_error:
            raise RuntimeError("Sender and Recipient email addresses are required.")
        return

    size_gb = run_data['size_bytes'] / (1024 * 1024 * 1024)
    duration_str = fmt_duration(run_data['duration'])
    started_str = datetime.fromtimestamp(run_data['started']).strftime('%Y-%m-%d %H:%M:%S')
    ended_str = datetime.fromtimestamp(run_data['ended']).strftime('%Y-%m-%d %H:%M:%S')
    status_text = run_data['status'].upper()

    theme_color = "#10b981"
    bg_banner = "linear-gradient(135deg, #10b981 0%, #059669 100%)"
    if "failed" in run_data['status'].lower():
        theme_color = "#ef4444"
        bg_banner = "linear-gradient(135deg, #ef4444 0%, #dc2626 100%)"
    elif "error" in run_data['status'].lower():
        theme_color = "#f59e0b"
        bg_banner = "linear-gradient(135deg, #f59e0b 0%, #d97706 100%)"

    subject = f"Backup Job {status_text}: {run_data['job_label'] or run_data['job_id'][:8]}"

    # Re-use same HTML body as SMTP version
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body{{font-family:system-ui,sans-serif;background:#080a10;color:#f8fafc;margin:0;padding:20px}}
.card{{background:#0e111a;border:1px solid rgba(255,255,255,0.05);border-radius:12px;overflow:hidden;max-width:600px;margin:20px auto}}
.banner{{background:{bg_banner};color:#fff;padding:24px;text-align:center}}
.banner h2{{margin:0;font-size:20px;font-weight:700}}
.content{{padding:28px}}
table{{width:100%;border-collapse:collapse}}
td{{padding:8px 10px;font-size:13px}}
td:first-child{{color:#94a3b8;font-weight:600;width:30%}}
td:last-child{{color:#f8fafc}}
.footer{{padding:16px 28px;background:rgba(8,10,16,0.4);border-top:1px solid rgba(255,255,255,0.05);font-size:11px;color:#64748b;text-align:center}}
</style></head>
<body><div class="card">
<div class="banner"><h2>Backup Job: {status_text}</h2></div>
<div class="content"><table>
<tr><td>Job Label</td><td>{run_data['job_label'] or '—'}</td></tr>
<tr><td>VM Name(s)</td><td><strong>{run_data['vm_name']}</strong></td></tr>
<tr><td>Size</td><td>{size_gb:.2f} GB</td></tr>
<tr><td>Duration</td><td>{duration_str}</td></tr>
<tr><td>Start Time</td><td>{started_str}</td></tr>
<tr><td>End Time</td><td>{ended_str}</td></tr>
<tr><td>Status</td><td style="color:{theme_color};font-weight:700">{run_data['status']}</td></tr>
</table></div>
<div class="footer">vSphere Backup Manager &middot; Job ID: {run_data['job_id']}</div>
</div></body></html>"""

    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(html, 'html'))

    try:
        # sendmail -t reads recipients from headers, -oi ignores lone dots in body
        proc = subprocess.run(
            [sendmail_path, '-t', '-oi'],
            input=msg.as_bytes(),
            capture_output=True,
            timeout=30
        )
        if proc.returncode != 0:
            stderr_out = proc.stderr.decode(errors='replace').strip()
            err = f"sendmail exited with code {proc.returncode}: {stderr_out}"
            print(f"Email sendmail error: {err}", file=sys.stderr)
            if raise_on_error:
                raise RuntimeError(err)
    except FileNotFoundError:
        err = f"sendmail binary not found at '{sendmail_path}'. Install postfix/sendmail or check the path."
        print(f"Email sendmail error: {err}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(err)
    except subprocess.TimeoutExpired:
        err = f"sendmail timed out after 30 seconds."
        print(f"Email sendmail error: {err}", file=sys.stderr)
        if raise_on_error:
            raise RuntimeError(err)
    except Exception as e:
        print(f"Error sending email via sendmail: {e}", file=sys.stderr)
        if raise_on_error:
            raise


def log_and_notify_run(jid, info, start_time, end_time, status, run_dest):
    size_bytes = get_dir_size(run_dest) if run_dest else 0
    duration = end_time - start_time
    
    vm_names = info.get('vm_names')
    if vm_names:
        vm_display = ", ".join(vm_names)
    else:
        vm_display = info.get('vm_name', '—')
        
    conn = sqlite3.connect(DB_PATH)
    run_id = None
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO job_runs (job_id, job_label, vm_name, started, ended, duration, status, size_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (jid, info.get('label', ''), vm_display, start_time, end_time, duration, status, size_bytes))
        conn.commit()
        run_id = cursor.lastrowid
    except Exception as e:
        print(f"Error writing to job_runs database: {e}", file=sys.stderr)
    finally:
        conn.close()
        
    # Log Retention Policy Cleanups
    retention_days = get_setting('log_retention_days', 'never')
    if retention_days != 'never' and str(retention_days).isdigit():
        days = int(retention_days)
        if days > 0:
            cutoff_time = time.time() - (days * 86400)
            conn = sqlite3.connect(DB_PATH)
            try:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM job_runs WHERE started < ?", (cutoff_time,))
                conn.commit()
            except Exception as e:
                print(f"Error cleaning up old job runs: {e}", file=sys.stderr)
            finally:
                conn.close()
        
    alert_level = get_setting('alert_level', 'all')
    is_failed = 'failed' in status.lower() or 'error' in status.lower()
    
    should_alert = False
    if alert_level == 'all':
        should_alert = True
    elif alert_level == 'failed' and is_failed:
        should_alert = True
        
    if not should_alert:
        return
        
    run_data = {
        'job_id': jid,
        'job_label': info.get('label', ''),
        'vm_name': vm_display,
        'started': start_time,
        'ended': end_time,
        'duration': duration,
        'status': status,
        'size_bytes': size_bytes
    }
    
    notification_sent = 0
    
    webhook_enabled = get_setting('webhook_enabled') == 'true'
    webhook_url = get_setting('webhook_url')
    webhook_type = get_setting('webhook_type', 'slack_discord')
    if webhook_enabled and (webhook_url or webhook_type == 'telegram'):
        try:
            send_webhook_notification(webhook_url, webhook_type, run_data)
            notification_sent = 1
        except Exception:
            pass
            
    smtp_enabled = get_setting('smtp_enabled') == 'true'
    if smtp_enabled:
        mail_service = get_setting('smtp_mail_service', 'smtp')
        if mail_service == 'sendmail':
            sendmail_cfg = {
                'sendmail_path': get_setting('sendmail_path', '/usr/sbin/sendmail'),
                'sender': get_setting('smtp_sender'),
                'recipient': get_setting('smtp_recipient'),
            }
            try:
                t = threading.Thread(target=send_email_via_sendmail, args=(sendmail_cfg, run_data), daemon=True)
                t.start()
                notification_sent = 1
            except Exception:
                pass
        else:
            smtp_settings = {
                'host': get_setting('smtp_host'),
                'port': get_setting('smtp_port'),
                'user': get_setting('smtp_user'),
                'password': get_setting('smtp_password'),
                'sender': get_setting('smtp_sender'),
                'recipient': get_setting('smtp_recipient'),
                'encryption': get_setting('smtp_encryption', 'starttls')
            }
            try:
                t = threading.Thread(target=send_email_notification, args=(smtp_settings, run_data), daemon=True)
                t.start()
                notification_sent = 1
            except Exception:
                pass
            
    if notification_sent and run_id:
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE job_runs SET notification_sent = 1 WHERE id = ?", (run_id,))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()


def job_to_display(jid, info):
    """Convert internal job dict to template-friendly dict."""
    disk_filter = info.get('disk_filter')
    vm_names = info.get('vm_names')
    if vm_names:
        vm_display = f"{len(vm_names)} VMs ({', '.join(vm_names[:3])}{'...' if len(vm_names) > 3 else ''})"
    else:
        vm_display = info.get('vm_name', '—')

    next_run = None
    if HAS_SCHEDULER and scheduler and info.get('schedule_id'):
        try:
            sched_job = scheduler.get_job(f'backup-{jid}')
            if sched_job and sched_job.next_run_time:
                next_run = sched_job.next_run_time.strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            pass

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
        'next_run':        next_run,
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
            final_status = info['status']
            run_dest = info.get('run_dest')
            
        log_and_notify_run(jid, info, info['started'], time.time(), final_status, run_dest)
        
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
            
            with jobs_db_lock:
                final_status = info.get('status', 'failed')
            
            log_and_notify_run(jid, info, info['started'], time.time(), final_status, run_dest)


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


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings_page():
    if request.method == 'POST':
        set_setting('smtp_enabled', 'true' if 'smtp_enabled' in request.form else 'false')
        set_setting('smtp_mail_service', request.form.get('smtp_mail_service', 'smtp'))
        set_setting('smtp_host', request.form.get('smtp_host', '').strip())
        set_setting('smtp_port', request.form.get('smtp_port', '587').strip())
        set_setting('smtp_encryption', request.form.get('smtp_encryption', 'starttls'))
        set_setting('smtp_user', request.form.get('smtp_user', '').strip())
        set_setting('smtp_password', request.form.get('smtp_password', '').strip())
        set_setting('smtp_sender', request.form.get('smtp_sender', '').strip())
        set_setting('smtp_recipient', request.form.get('smtp_recipient', '').strip())
        set_setting('sendmail_path', request.form.get('sendmail_path', '/usr/sbin/sendmail').strip())
        
        set_setting('webhook_enabled', 'true' if 'webhook_enabled' in request.form else 'false')
        set_setting('webhook_url', request.form.get('webhook_url', '').strip())
        set_setting('webhook_type', request.form.get('webhook_type', 'slack_discord'))
        set_setting('telegram_bot_token', request.form.get('telegram_bot_token', '').strip())
        set_setting('telegram_chat_id', request.form.get('telegram_chat_id', '').strip())
        
        set_setting('alert_level', request.form.get('alert_level', 'all'))
        set_setting('log_retention_days', request.form.get('log_retention_days', 'never'))
        
        flash('Settings saved successfully.', 'success')
        return redirect(url_for('settings_page'))
        
    opts = {
        'smtp_enabled': get_setting('smtp_enabled', 'false') == 'true',
        'smtp_mail_service': get_setting('smtp_mail_service', 'smtp'),
        'smtp_host': get_setting('smtp_host', ''),
        'smtp_port': get_setting('smtp_port', '587'),
        'smtp_encryption': get_setting('smtp_encryption', 'starttls'),
        'smtp_user': get_setting('smtp_user', ''),
        'smtp_password': get_setting('smtp_password', ''),
        'smtp_sender': get_setting('smtp_sender', ''),
        'smtp_recipient': get_setting('smtp_recipient', ''),
        'sendmail_path': get_setting('sendmail_path', '/usr/sbin/sendmail'),
        
        'webhook_enabled': get_setting('webhook_enabled', 'false') == 'true',
        'webhook_url': get_setting('webhook_url', ''),
        'webhook_type': get_setting('webhook_type', 'slack_discord'),
        'telegram_bot_token': get_setting('telegram_bot_token', ''),
        'telegram_chat_id': get_setting('telegram_chat_id', ''),
        
        'alert_level': get_setting('alert_level', 'all'),
        'log_retention_days': get_setting('log_retention_days', 'never')
    }
    return render_template('settings.html', settings=opts)


@app.route('/settings/test-notification', methods=['POST'])
@login_required
def settings_test_notification():
    webhook_enabled = 'webhook_enabled' in request.form
    webhook_url = request.form.get('webhook_url', '').strip()
    webhook_type = request.form.get('webhook_type', 'slack_discord')
    
    smtp_enabled = 'smtp_enabled' in request.form
    mail_service = request.form.get('smtp_mail_service', 'smtp')
    smtp_settings = {
        'host': request.form.get('smtp_host', '').strip(),
        'port': request.form.get('smtp_port', '587').strip(),
        'user': request.form.get('smtp_user', '').strip(),
        'password': request.form.get('smtp_password', '').strip(),
        'sender': request.form.get('smtp_sender', '').strip(),
        'recipient': request.form.get('smtp_recipient', '').strip(),
        'encryption': request.form.get('smtp_encryption', 'starttls')
    }
    sendmail_cfg = {
        'sendmail_path': request.form.get('sendmail_path', '/usr/sbin/sendmail').strip() or '/usr/sbin/sendmail',
        'sender': request.form.get('smtp_sender', '').strip(),
        'recipient': request.form.get('smtp_recipient', '').strip(),
    }

    test_run_data = {
        'job_id': 'test-run-id-12345',
        'job_label': 'Diagnostic Test Alert',
        'vm_name': 'mock-vm-1, mock-vm-2',
        'started': time.time() - 45,
        'ended': time.time(),
        'duration': 45.0,
        'status': 'finished (Diagnostic Test Success)',
        'size_bytes': 1532984025
    }

    webhook_error = None
    email_error = None

    if webhook_enabled and (webhook_url or webhook_type == 'telegram'):
        try:
            telegram_bot_token = request.form.get('telegram_bot_token', '').strip()
            telegram_chat_id = request.form.get('telegram_chat_id', '').strip()
            send_webhook_notification(
                webhook_url,
                webhook_type,
                test_run_data,
                raise_on_error=True,
                telegram_bot_token=telegram_bot_token,
                telegram_chat_id=telegram_chat_id
            )
        except Exception as e:
            webhook_error = str(e)

    if smtp_enabled:
        try:
            if mail_service == 'sendmail':
                send_email_via_sendmail(sendmail_cfg, test_run_data, raise_on_error=True)
            else:
                send_email_notification(smtp_settings, test_run_data, raise_on_error=True)
        except Exception as e:
            email_error = str(e)
            
    if webhook_error or email_error:
        err_msg = ""
        if webhook_error:
            err_msg += f"Webhook Failed: {webhook_error}. "
        if email_error:
            err_msg += f"Email Failed: {email_error}."
        flash(err_msg, 'danger')
    else:
        flash('Diagnostic test notifications sent successfully. Please check your Inbox and Webhook channel!', 'success')
        
    return redirect(url_for('settings_page'))


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

    # Calculate set of scheduled VMs
    active_scheduled_vms = set()
    with jobs_db_lock:
        for job in jobs.values():
            if job.get('schedule_type') and job.get('schedule_type') != 'now' and job.get('schedule_id'):
                vm_names = job.get('vm_names')
                if vm_names:
                    for vm in vm_names:
                        active_scheduled_vms.add(vm)
                else:
                    vm_name = job.get('vm_name')
                    if vm_name:
                        active_scheduled_vms.add(vm_name)

    return render_template(
        'vms.html',
        vms=vm_list,
        error=error,
        cache_age=cache_age,
        scheduled_vms=list(active_scheduled_vms)
    )


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


@app.route('/reports')
@login_required
def reports_dashboard():
    conn = sqlite3.connect(DB_PATH)
    runs = []
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, job_id, job_label, vm_name, started, ended, duration, status, size_bytes, notification_sent 
            FROM job_runs 
            ORDER BY started DESC
        ''')
        rows = cursor.fetchall()
        for r in rows:
            runs.append({
                'id': r[0],
                'job_id': r[1],
                'job_label': r[2],
                'vm_name': r[3],
                'started': r[4],
                'started_fmt': datetime.fromtimestamp(r[4]).strftime('%Y-%m-%d %H:%M:%S') if r[4] else '—',
                'ended': r[5],
                'duration': r[6],
                'duration_fmt': fmt_duration(r[6]) if r[6] else '—',
                'status': r[7],
                'size_bytes': r[8],
                'size_gb': round(r[8] / (1024 * 1024 * 1024), 2) if r[8] else 0.0,
                'notification_sent': bool(r[9])
            })
    except Exception as e:
        print(f"Error fetching job_runs: {e}", file=sys.stderr)
    finally:
        conn.close()
        
    total_runs = len(runs)
    success_runs = sum(1 for r in runs if r['status'].lower() in ('finished', 'success'))
    failed_runs = total_runs - success_runs
    success_rate = round((success_runs / total_runs) * 100, 1) if total_runs > 0 else 100.0
    total_size_bytes = sum(r['size_bytes'] for r in runs)
    total_size_gb = round(total_size_bytes / (1024 * 1024 * 1024), 2)
    
    avg_duration = round(sum(r['duration'] for r in runs) / total_runs, 1) if total_runs > 0 else 0.0
    avg_duration_fmt = fmt_duration(avg_duration)
    
    stats = {
        'total': total_runs,
        'success': success_runs,
        'failed': failed_runs,
        'success_rate': success_rate,
        'total_size_gb': total_size_gb,
        'avg_duration_fmt': avg_duration_fmt
    }
    
    return render_template('reports.html', runs=runs, stats=stats)


@app.route('/api/reports-data')
@login_required
def reports_data_api():
    conn = sqlite3.connect(DB_PATH)
    chart_data = []
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT started, size_bytes, duration, status 
            FROM job_runs 
            ORDER BY started DESC 
            LIMIT 15
        ''')
        rows = cursor.fetchall()
        for r in reversed(rows):
            chart_data.append({
                'date': datetime.fromtimestamp(r[0]).strftime('%m-%d %H:%M'),
                'size_gb': round(r[1] / (1024 * 1024 * 1024), 2) if r[1] else 0.0,
                'duration_sec': round(r[2], 1) if r[2] else 0.0,
                'status': r[3]
            })
    except Exception as e:
        print(f"Error fetching reports API: {e}", file=sys.stderr)
    finally:
        conn.close()
    return jsonify({'runs': chart_data})


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
            info['schedule_start_month'] = None

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
