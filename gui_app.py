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

# In-memory job store: {job_id: job_dict}
# job_dict keys: id, label, vm_name, status, started, dest, compress,
#                no_verify_ssl, sftp_host, sftp_user, sftp_password,
#                log, schedule_type, schedule_time, schedule_id
jobs: dict = {}

# APScheduler instance
scheduler = None
if HAS_SCHEDULER:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.start()

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
    if key in _bg_refresh_running:
        return
    _bg_refresh_running.add(key)

    def _worker():
        try:
            _fetch_and_cache(host, user, password, no_verify_ssl)
        finally:
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
                        free  = st.f_available * st.f_frsize
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
    return {
        'id':            jid,
        'label':         info.get('label', ''),
        'vm_name':       info.get('vm_name', '—'),
        'status':        info.get('status', 'unknown'),
        'started_fmt':   fmt_time(info.get('started')),
        'dest':          info.get('dest', ''),
        'compress':      info.get('compress', False),
        'sftp_host':     info.get('sftp_host', ''),
        'schedule_type': info.get('schedule_type', 'now'),
        'schedule_time': info.get('schedule_time', ''),
        'schedule_id':   info.get('schedule_id'),
        'disk_filter':   disk_filter,
        'disks_count':   len(disk_filter) if disk_filter is not None else None,
    }


def run_job_thread(jid):
    """Worker executed in a thread (and by APScheduler)."""
    info = jobs.get(jid)
    if not info:
        return
    info['status']   = 'running'
    info['started']  = time.time()
    info['progress'] = {'pct': 0, 'phase': 'starting', 'detail': 'Initializing…'}
    log_path = str(JOBS_DIR / jid / 'backup.log')

    def progress_cb(prog):
        info['progress'] = prog

    try:
        run_backup(
            host=info['host'],
            user=info['user'],
            password=info['password'],
            vm_name=info['vm_name'],
            dest=info['dest'],
            compress=info.get('compress', False),
            no_verify_ssl=info.get('no_verify_ssl', False),
            sftp_host=info.get('sftp_host') or None,
            sftp_user=info.get('sftp_user') or None,
            sftp_password=info.get('sftp_password') or None,
            sftp_key=None,
            log_path=log_path,
            progress_cb=progress_cb,
            disk_filter=info.get('disk_filter'),  # None = all disks
        )
        info['status']   = 'finished'
        info['progress'] = {'pct': 100, 'phase': 'done', 'detail': 'Backup completed successfully'}
    except Exception as e:
        info['status'] = f'failed ({e})'


def create_and_start_job(
    vm_name, dest, compress, no_verify_ssl,
    sftp_host, sftp_user, sftp_password,
    schedule_type, schedule_time, weekly_day, interval_hours,
    label='', disk_filter=None, monthly_day=1
):
    """Create a job entry and either run immediately or register schedule.
    disk_filter: list of VMDK path strings to include, or None for all.
    monthly_day: day of month (1-28) for monthly schedule.
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
        'dest':          dest,
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
    }
    jobs[jid] = info

    if schedule_type == 'now' or not HAS_SCHEDULER:
        t = threading.Thread(target=run_job_thread, args=(jid,), daemon=True)
        t.start()
    else:
        # Build APScheduler trigger
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
            trigger = CronTrigger(
                day=max(1, min(28, int(monthly_day or 1))),
                hour=int(hour), minute=int(minute)
            )
        elif schedule_type == 'interval':
            trigger = IntervalTrigger(hours=max(1, int(interval_hours or 24)))

        if trigger:
            # Capture jid in closure
            def make_runner(j):
                def _runner():
                    run_job_thread(j)
                return _runner

            sched_job = scheduler.add_job(
                make_runner(jid),
                trigger=trigger,
                id=f'backup-{jid}',
                name=f'Backup {vm_name} ({label or jid[:8]})',
                misfire_grace_time=3600,
                max_instances=1,
            )
            info['schedule_id'] = sched_job.id
            info['status'] = 'scheduled'
        else:
            # Fallback: run now
            t = threading.Thread(target=run_job_thread, args=(jid,), daemon=True)
            t.start()

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
        vm_name       = request.form.get('vm_name', '').strip()
        dest          = request.form.get('dest', './backups').strip()
        compress      = 'compress' in request.form
        no_verify_ssl = 'no_verify_ssl' in request.form
        sftp_host     = request.form.get('sftp_host', '').strip() or None
        sftp_user     = request.form.get('sftp_user', '').strip() or None
        sftp_password = request.form.get('sftp_password', '') or None
        schedule_type = request.form.get('schedule_type', 'now')
        daily_time    = request.form.get('daily_time', '02:00')
        weekly_day    = request.form.get('weekly_day', '0')
        weekly_time   = request.form.get('weekly_time', '02:00')
        monthly_day   = request.form.get('monthly_day', '1')
        monthly_time  = request.form.get('monthly_time', '02:00')
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
        elif schedule_type == 'monthly':
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

    return render_template(
        'create_job.html',
        vms=vm_list,
        selected_vm=selected_vm,
        show_schedule=show_schedule,
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
        vm_names      = request.form.getlist('vms')
        dest          = request.form.get('dest', './backups').strip()
        compress      = 'compress' in request.form
        no_verify_ssl = session.get('no_verify_ssl', False)
        disk_strategy = request.form.get('disk_strategy', 'all')
        schedule_type = request.form.get('schedule_type', 'now')
        daily_time    = request.form.get('daily_time', '02:00')
        weekly_day    = request.form.get('weekly_day', '0')
        weekly_time   = request.form.get('weekly_time', '02:00')
        monthly_day   = request.form.get('monthly_day', '1')
        monthly_time  = request.form.get('monthly_time', '02:00')
        interval_hrs  = request.form.get('interval_hours', '24')
        label_prefix  = request.form.get('job_label', '').strip()

        if schedule_type == 'daily':
            sched_time = daily_time
        elif schedule_type == 'weekly':
            sched_time = weekly_time
        elif schedule_type == 'monthly':
            sched_time = monthly_time
        else:
            sched_time = ''

        if not vm_names:
            flash('No VMs selected.', 'danger')
            return redirect(url_for('vms'))

        created = []
        for vm_name in vm_names:
            # Resolve disk_filter from strategy
            if disk_strategy == 'os':
                vm_info = vms_by_name.get(vm_name, {})
                disks = sorted(vm_info.get('disks', []), key=lambda d: d.get('size_gb', 0))
                disk_filter = [disks[0]['path']] if disks else None
            elif disk_strategy == 'vmx':
                disk_filter = []
            else:
                disk_filter = None

            label = f'{label_prefix} — {vm_name}' if label_prefix else vm_name

            jid = create_and_start_job(
                vm_name=vm_name,
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
                disk_filter=disk_filter,
                monthly_day=monthly_day,
            )
            created.append(jid)

        strat_label = {'all': 'all disks', 'os': 'OS disk only', 'vmx': 'VMX config only'}.get(disk_strategy, disk_strategy)
        flash(f'{len(created)} backup job{"s" if len(created)!=1 else ""} created ({strat_label}).', 'success')
        return redirect(url_for('jobs'))


    # GET: show batch config form
    vm_names = request.args.getlist('vms')
    if not vm_names:
        flash('No VMs specified for batch backup.', 'danger')
        return redirect(url_for('vms'))

    return render_template(
        'batch_job.html',
        vm_names=vm_names,
        vms_by_name=vms_by_name,
    )


# ── Jobs Dashboard ────────────────────────────────────────────────────────────

@app.route('/jobs')
@login_required
def list_jobs():
    job_list = [
        job_to_display(jid, info)
        for jid, info in sorted(jobs.items(), key=lambda x: x[1].get('started', 0), reverse=True)
    ]
    scheduled_count = sum(1 for j in job_list if j['schedule_id'])
    return render_template('jobs.html', jobs=job_list, scheduled_count=scheduled_count)


# ── Job Detail ────────────────────────────────────────────────────────────────

@app.route('/job/<jobid>')
@login_required
def job_detail(jobid):
    info = jobs.get(jobid)
    if not info:
        abort(404)
    return render_template('job_detail.html', job=job_to_display(jobid, info))


@app.route('/job/<jobid>/log')
@login_required
def job_log(jobid):
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
    info = jobs.get(jobid)
    if not info:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'status':   info.get('status', 'unknown'),
        'id':       jobid,
        'progress': info.get('progress', {'pct': 0, 'phase': '', 'detail': ''}),
    })


@app.route('/job/<jobid>/cancel-schedule', methods=['POST'])
@login_required
def cancel_schedule(jobid):
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
    flash('Recurring schedule cancelled.', 'success')
    return redirect(url_for('job_detail', jobid=jobid))


# ── Template filter ───────────────────────────────────────────────────────────
@app.template_filter('startswith')
def startswith_filter(value, prefix):
    return str(value).startswith(prefix)


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
