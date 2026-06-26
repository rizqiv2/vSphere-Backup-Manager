import atexit
import getpass
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
import threading
import builtins

# Thread-local storage for log file path to ensure print() statements are thread-safe and write to the correct log file
thread_local_log = threading.local()

def print(*args, **kwargs):
    # Check if this thread has a thread-local log path
    log_path = getattr(thread_local_log, 'path', None)
    if log_path:
        try:
            sep = kwargs.get('sep', ' ')
            end = kwargs.get('end', '\n')
            msg = sep.join(str(arg) for arg in args) + end
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(msg)
        except Exception as e:
            builtins.print(f"Fallback log error: {e}", file=sys.stderr)
            builtins.print(*args, **kwargs)
    else:
        builtins.print(*args, **kwargs)

import requests
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim, vmodl

try:
    import paramiko
except Exception:
    paramiko = None


def get_si(host, user, pwd, no_verify_ssl=False):
    context = None
    if no_verify_ssl:
        context = ssl._create_unverified_context()
    si = SmartConnect(host=host, user=user, pwd=pwd, sslContext=context)
    # Caller is responsible for disconnect via Disconnect(si)
    return si


def list_vms(host, user, password, no_verify_ssl=False):
    """Connect to vCenter/ESXi and return a list of VM info dicts."""
    si = None
    try:
        si = get_si(host, user, password, no_verify_ssl=no_verify_ssl)
        content = si.RetrieveContent()
        obj_view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        vms = []
        for vm in obj_view.view:
            try:
                summary = vm.summary
                config = summary.config
                runtime = summary.runtime
                guest = summary.guest
                storage = summary.storage

                # Power state
                power_map = {
                    vim.VirtualMachinePowerState.poweredOn: 'poweredOn',
                    vim.VirtualMachinePowerState.poweredOff: 'poweredOff',
                    vim.VirtualMachinePowerState.suspended: 'suspended',
                }
                power_state = power_map.get(runtime.powerState, str(runtime.powerState))

                # Datastore names
                ds_names = []
                try:
                    for ds in vm.datastore:
                        ds_names.append(ds.info.name)
                except Exception:
                    pass

                # Disk info (label, path, size)
                disks = []
                try:
                    for dev in vm.config.hardware.device:
                        if isinstance(dev, vim.vm.device.VirtualDisk):
                            fn = getattr(dev.backing, 'fileName', None)
                            if not fn:
                                continue
                            label = ''
                            if dev.deviceInfo:
                                label = dev.deviceInfo.label or ''
                            size_kb = getattr(dev, 'capacityInKB', 0) or 0
                            disks.append({
                                'label':   label or f'Hard disk {dev.unitNumber}',
                                'path':    fn,
                                'size_gb': round(size_kb / (1024 * 1024), 1),
                                'unit':    dev.unitNumber,
                            })
                except Exception:
                    pass

                vms.append({
                    'name':        config.name,
                    'power_state': power_state,
                    'num_cpu':     config.numCpu,
                    'memory_mb':   config.memorySizeMB,
                    'guest_os':    config.guestFullName or config.guestId or 'Unknown',
                    'ip_address':  (guest.ipAddress or '') if guest else '',
                    'datastores':  ds_names,
                    'committed_gb': round((storage.committed or 0) / (1024 ** 3), 2),
                    'tools_status': (guest.toolsStatus or 'unknown') if guest else 'unknown',
                    'disks':       disks,
                })
            except Exception as e:
                vms.append({'name': getattr(vm, 'name', '?'), 'error': str(e),
                            'power_state': 'unknown', 'num_cpu': 0,
                            'memory_mb': 0, 'guest_os': '', 'ip_address': '',
                            'datastores': [], 'committed_gb': 0,
                            'tools_status': 'unknown', 'disks': []})
        obj_view.Destroy()
        return vms
    finally:
        if si:
            try:
                Disconnect(si)
            except Exception:
                pass


def wait_for_task(task, action_name='job'):
    while True:
        info = getattr(task, 'info', None)
        if info and info.state in (vim.TaskInfo.State.success, vim.TaskInfo.State.error):
            break
        time.sleep(1)
    info = task.info
    if info.state == vim.TaskInfo.State.success:
        return info.result
    else:
        err = info.error
        fault_name = err.__class__.__name__ if err else "UnknownFault"
        err_msg = getattr(err, 'msg', None) or str(err)
        raise Exception(f"{action_name} did not complete successfully: {fault_name}: {err_msg}")


def create_snapshot(vm, snap_name, desc="backup snapshot", memory=False, quiesce=False):
    print(f"Creating snapshot '{snap_name}'")
    task = vm.CreateSnapshot_Task(name=snap_name, description=desc, memory=memory, quiesce=quiesce)
    wait_for_task(task, 'CreateSnapshot')
    print("Snapshot created")


def find_datacenter_for_datastore(content, datastore_name):
    for dc in content.rootFolder.childEntity:
        if isinstance(dc, vim.Datacenter):
            for ds in dc.datastore:
                if ds.info.name == datastore_name:
                    return dc
    return None


def download_datastore_file(host, dc_name, datastore_name, ds_path, local_path,
                            session_cookie, verify_ssl=True, progress_cb=None):
    """Download a file from a vSphere datastore and return its SHA-256 checksum. progress_cb(bytes_done, bytes_total) is optional."""
    # Keep slashes unencoded (safe='/') — vCenter's /folder/ API requires them in the URL path.
    encoded_path = urllib.parse.quote(ds_path, safe='/')
    url = (f"https://{host}/folder/{encoded_path}"
           f"?dcPath={urllib.parse.quote(dc_name)}&dsName={urllib.parse.quote(datastore_name)}")
    headers = {"Cookie": f"vmware_soap_session={session_cookie}"}
    print(f"Downloading {ds_path} from datastore {datastore_name} to {local_path}")
    print(f"  URL: {url}")
    sha256 = hashlib.sha256()
    with requests.get(url, headers=headers, stream=True, verify=verify_ssl, proxies={"http": None, "https": None}, timeout=30) as r:
        r.raise_for_status()
        total_bytes = int(r.headers.get('Content-Length', 0))
        print(f"  HTTP {r.status_code}, Content-Length: {total_bytes} bytes")
        done_bytes = 0
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    sha256.update(chunk)
                    done_bytes += len(chunk)
                    if progress_cb:
                        progress_cb(done_bytes, total_bytes)
    print(f"Download completed ({done_bytes // (1024*1024)} MB)")
    return sha256.hexdigest()


def extract_session_cookie(si):
    raw = getattr(si._stub, 'cookie', '')
    m = re.search(r"vmware_soap_session\s*=\s*\"?([A-Za-z0-9\-_]+)\"?", raw)
    if m:
        return m.group(1)
    return None


def vm_disk_vmdk_paths(vm):
    files = set()
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualDisk):
            backing = dev.backing
            fn = getattr(backing, 'fileName', None)
            if fn:
                files.add(fn)
    return list(files)


def vm_config_vmx_path(vm):
    return getattr(vm.config.files, 'vmPathName', None)


def parse_datastore_path(ds_file_ref):
    m = re.match(r"\[(?P<ds>[^\]]+)\]\s*(?P<path>.+)", ds_file_ref)
    if not m:
        raise ValueError(f"Unexpected datastore file format: {ds_file_ref}")
    return m.group('ds'), m.group('path')


def find_snapshot_by_name(snapshots, name):
    for snap in snapshots:
        if snap.name == name:
            return snap.snapshot
        if snap.childSnapshotList:
            found = find_snapshot_by_name(snap.childSnapshotList, name)
            if found:
                return found
    return None


def find_backup_snapshots(snapshots):
    found = []
    for snap in snapshots:
        is_backup = (
            (snap.name and (snap.name.startswith("backup-") or snap.name.startswith("cbt-activate-"))) or
            (snap.description and "backup snapshot" in snap.description.lower())
        )
        if is_backup:
            found.append(snap)
        if snap.childSnapshotList:
            found.extend(find_backup_snapshots(snap.childSnapshotList))
    return found


def remove_snapshot(snapshot_obj):
    print("Removing snapshot")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            task = snapshot_obj.RemoveSnapshot_Task(removeChildren=False)
            wait_for_task(task, 'RemoveSnapshot')
            print("Snapshot removed")
            return
        except Exception as e:
            print(f"Attempt {attempt+1} to remove snapshot failed: {e}")
            if attempt < max_retries - 1:
                print("Waiting 5 seconds before retrying...")
                time.sleep(5)
            else:
                raise e


# ── CBT (Changed Block Tracking) helpers ─────────────────────────────────────

def enable_cbt(vm, content):
    """Enable changeTrackingEnabled on a VM if not already set.
    
    CBT requires a snapshot cycle to activate. We create+delete a transient
    snapshot here so the flag takes effect before the real backup snapshot.
    Returns True if CBT was already enabled, False if we just enabled it.
    """
    cfg = vm.config
    if cfg.changeTrackingEnabled:
        print("CBT: changeTrackingEnabled is already ON")
        return True

    print("CBT: Enabling changeTrackingEnabled on VM…")
    spec = vim.vm.ConfigSpec()
    spec.changeTrackingEnabled = True
    task = vm.ReconfigVM_Task(spec=spec)
    wait_for_task(task, 'EnableCBT')
    print("CBT: changeTrackingEnabled set to True")

    # Force a snapshot cycle so CBT activates on all disks
    print("CBT: Creating transient activation snapshot…")
    act_snap_name = f"cbt-activate-{int(time.time())}"
    task = vm.CreateSnapshot_Task(
        name=act_snap_name,
        description="CBT activation (auto-deleted)",
        memory=False, quiesce=False
    )
    wait_for_task(task, 'CBTActivateSnapshot')

    # Immediately delete it
    snap_root = getattr(vm, 'snapshot', None)
    if snap_root and snap_root.rootSnapshotList:
        snap_obj = find_snapshot_by_name(snap_root.rootSnapshotList, act_snap_name)
        if snap_obj:
            task = snap_obj.RemoveSnapshot_Task(removeChildren=False)
            wait_for_task(task, 'CBTActivateSnapshotRemove')
    print("CBT: Transient snapshot removed — CBT is now active")
    return False


def get_disk_device_by_key(vm, device_key):
    """Return the VirtualDisk device object for a given device key."""
    for dev in vm.config.hardware.device:
        if isinstance(dev, vim.vm.device.VirtualDisk) and dev.key == device_key:
            return dev
    return None


def get_disk_change_id(snapshot_ref, device_key):
    """Return the changeId for a disk at a given snapshot.
    
    changeId '*' means "give me all changes since disk was created" — used
    to seed the first incremental after a full backup.
    """
    try:
        for disk_layout in snapshot_ref.config.hardware.device:
            if isinstance(disk_layout, vim.vm.device.VirtualDisk) and disk_layout.key == device_key:
                backing = disk_layout.backing
                cid = getattr(backing, 'changeId', None)
                if cid:
                    return cid
    except Exception as e:
        print(f"CBT: Could not get changeId for device key {device_key}: {e}")
    return None


def query_changed_areas(vm_snapshot, device_key, change_id, start_offset=0):
    """Call QueryChangedDiskAreas and return a list of {start, length} extents.
    
    change_id: the changeId from the *previous* backup snapshot.
               Use '*' to get all changed areas since disk creation.
    Returns list of dicts: [{'start': int, 'length': int}, ...]
    """
    extents = []
    try:
        result = vm_snapshot.QueryChangedDiskAreas(
            id=device_key,
            startOffset=start_offset,
            changeId=change_id
        )
        if result and result.changedArea:
            for area in result.changedArea:
                extents.append({'start': area.start, 'length': area.length})
        print(f"CBT: QueryChangedDiskAreas returned {len(extents)} extent(s), "
              f"{sum(e['length'] for e in extents) // (1024*1024)} MB changed")
    except vmodl.fault.InvalidArgument as e:
        print(f"CBT: InvalidArgument querying changed areas (changeId may be stale): {e}")
        raise
    except Exception as e:
        print(f"CBT: Error querying changed areas: {e}")
        raise
    return extents


def download_disk_changed_ranges(host, dc_name, ds_name, ds_path, extents,
                                  local_path, session_cookie,
                                  total_disk_size, verify_ssl=True,
                                  progress_cb=None):
    """Download only the changed byte extents from a flat VMDK via HTTP Range requests.

    Writes a sparse file: changed extents are filled with downloaded data;
    unchanged regions remain as zero bytes (seek over them).
    Returns (sha256_hex, bytes_downloaded).
    """
    encoded_path = urllib.parse.quote(ds_path, safe='/')
    url = (f"https://{host}/folder/{encoded_path}"
           f"?dcPath={urllib.parse.quote(dc_name)}&dsName={urllib.parse.quote(ds_name)}")
    headers_base = {"Cookie": f"vmware_soap_session={session_cookie}"}

    total_changed = sum(e['length'] for e in extents)
    print(f"CBT: Downloading {len(extents)} changed extent(s), "
          f"{total_changed // (1024*1024)} MB / "
          f"{total_disk_size // (1024*1024)} MB total")

    sha256 = hashlib.sha256()
    bytes_downloaded = 0

    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    # We build a sparse file matching the full disk geometry so restore works
    with open(local_path, 'wb') as f:
        # Pre-allocate to full disk size (sparse/hole-punched on Linux)
        if total_disk_size > 0:
            f.seek(total_disk_size - 1)
            f.write(b'\x00')
            f.seek(0)

        # Track file position for SHA-256 over the full logical disk
        # We hash the file after writing instead of on-the-fly to handle seeks correctly
        for i, extent in enumerate(extents):
            start = extent['start']
            length = extent['length']
            end_byte = start + length - 1

            range_header = f"bytes={start}-{end_byte}"
            req_headers = {**headers_base, "Range": range_header}

            with requests.get(url, headers=req_headers, stream=True,
                              verify=verify_ssl,
                              proxies={"http": None, "https": None},
                              timeout=30) as r:
                if r.status_code not in (200, 206):
                    raise Exception(f"HTTP {r.status_code} for Range {range_header}")

                f.seek(start)
                for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        bytes_downloaded += len(chunk)

            if progress_cb and total_changed > 0:
                progress_cb(bytes_downloaded, total_changed)

    # Compute SHA-256 of the resulting file
    sha256 = hashlib.sha256()
    with open(local_path, 'rb') as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            sha256.update(chunk)

    print(f"CBT: Incremental download complete — {bytes_downloaded // (1024*1024)} MB written")
    return sha256.hexdigest(), bytes_downloaded


CBT_STATE_FILENAME = 'cbt_state.json'


def load_cbt_state(vm_base_dir):
    """Load the CBT state dict from <vm_base_dir>/cbt_state.json.
    
    Returns dict with structure:
      { 'last_backup_ts': str, 'disks': { disk_path: { 'change_id': str, ... } } }
    or empty dict if not found.
    """
    state_path = os.path.join(vm_base_dir, CBT_STATE_FILENAME)
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"CBT: Could not read state file {state_path}: {e}")
        return {}


def save_cbt_state(vm_base_dir, state):
    """Persist the CBT state dict to <vm_base_dir>/cbt_state.json."""
    os.makedirs(vm_base_dir, exist_ok=True)
    state_path = os.path.join(vm_base_dir, CBT_STATE_FILENAME)
    try:
        with open(state_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
        print(f"CBT: State saved to {state_path}")
    except Exception as e:
        print(f"CBT: Could not save state file: {e}")


def get_file_sha256(filepath, decompress_if_zst=False):
    """Compute the SHA-256 hash of a file. Optionally decompress on-the-fly if it is a .zst file."""
    sha256 = hashlib.sha256()
    if decompress_if_zst and str(filepath).lower().endswith('.zst'):
        try:
            import zstandard as zstd
            with open(filepath, 'rb') as f:
                dctx = zstd.ZstdDecompressor()
                decompressor = dctx.read_to_iter(f, read_size=4*1024*1024)
                for chunk in decompressor:
                    sha256.update(chunk)
            return sha256.hexdigest()
        except Exception as e:
            print(f"Warning: Failed to decompress on the fly for SHA calculation: {e}. Falling back to raw file hash.")
            sha256 = hashlib.sha256()

    with open(filepath, 'rb') as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


def verify_backup_checksums(dest_dir):
    """Verify all files inside a backup directory using its manifest.json."""
    manifest_path = os.path.join(dest_dir, 'manifest.json')
    if not os.path.exists(manifest_path):
        print(f"No manifest.json found in {dest_dir}, skipping checksum verification.")
        return True

    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except Exception as e:
        print(f"Error reading manifest.json: {e}")
        return False

    print(f"Verifying checksums for backup of VM: {manifest.get('vm_name', 'unknown')}")
    all_ok = True
    for file_info in manifest.get('files', []):
        rel_path = file_info.get('path')
        expected_sha = file_info.get('sha256')

        # Determine actual file on disk (could be compressed with .zst extension)
        filepath = os.path.join(dest_dir, rel_path)
        actual_path = filepath
        decompress = False
        if not os.path.exists(filepath):
            if os.path.exists(filepath + '.zst'):
                actual_path = filepath + '.zst'
                decompress = True
            else:
                print(f"Verification FAILED: File not found: {filepath}")
                all_ok = False
                continue

        actual_sha = get_file_sha256(actual_path, decompress_if_zst=decompress)
        if actual_sha == expected_sha:
            print(f"Verification OK: {rel_path} (decompress={decompress})")
        else:
            print(f"Verification FAILED: {rel_path} (Expected: {expected_sha}, Got: {actual_sha})")
            all_ok = False

    return all_ok


def maybe_compress(path):
    try:
        import subprocess
        # Use level 3 (default), multi-threaded compression (threads=0), and delete original file on success (--rm)
        rc = subprocess.run(['zstd', '-3', '--threads=0', '--rm', path], check=False)
        if rc.returncode == 0:
            return path + '.zst'
    except FileNotFoundError:
        pass
    try:
        import zstandard as zstd
        out_path = path + '.zst'
        with open(path, 'rb') as ifh, open(out_path, 'wb') as ofh:
            # Use level 3, multi-threaded compression (threads=0 uses all cores)
            cctx = zstd.ZstdCompressor(level=3, threads=0)
            cctx.copy_stream(ifh, ofh)
        # Delete original file on success to save local storage space
        try:
            os.remove(path)
            print(f"Removed original file after compression: {path}")
        except Exception as e:
            print(f"Warning: Failed to remove original file {path} after compression: {e}")
        return out_path
    except Exception as e:
        print(f'Compression not available; skipping: {e}')
        return path


def upload_via_sftp(host, user, password, key_filename, local_path, remote_dir):
    if paramiko is None:
        raise RuntimeError("paramiko is required for SFTP upload")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if key_filename:
        client.connect(hostname=host, username=user, key_filename=key_filename)
    else:
        client.connect(hostname=host, username=user, password=password)
    sftp = client.open_sftp()
    try:
        try:
            sftp.chdir(remote_dir)
        except IOError:
            sftp.mkdir(remote_dir)
            sftp.chdir(remote_dir)
        fname = os.path.basename(local_path)
        print(f"Uploading {local_path} to {host}:{remote_dir}/{fname}")
        sftp.put(local_path, fname)
    finally:
        sftp.close()
        client.close()


def run_backup(host, user, password, vm_name, dest, compress=False, no_verify_ssl=False,
               sftp_host=None, sftp_user=None, sftp_password=None, sftp_key=None,
               log_path=None, progress_cb=None, disk_filter=None, job_id=None,
               is_cancelled_cb=None, use_cbt=False):
    """Run backup flow (full or CBT incremental).
    disk_filter: if not None, a set/list of VMDK file-ref strings to include.
                 The VMX config file is always included regardless.
    use_cbt: if True, attempt Changed Block Tracking incremental backup.
             Falls back to full download if CBT state is unavailable.
    """
    if log_path:
        thread_local_log.path = log_path
    else:
        thread_local_log.path = None

    try:
        return _run_backup_impl(host, user, password, vm_name, dest, compress, no_verify_ssl,
                                sftp_host, sftp_user, sftp_password, sftp_key,
                                progress_cb=progress_cb, disk_filter=disk_filter, job_id=job_id,
                                is_cancelled_cb=is_cancelled_cb, use_cbt=use_cbt)
    finally:
        thread_local_log.path = None


def _run_backup_impl(host, user, password, vm_name, dest, compress, no_verify_ssl,
                     sftp_host, sftp_user, sftp_password, sftp_key,
                     progress_cb=None, disk_filter=None, job_id=None,
                     is_cancelled_cb=None, use_cbt=False):
    def _prog(phase, pct, detail=''):
        if progress_cb:
            try:
                progress_cb({'phase': phase, 'pct': pct, 'detail': detail})
            except Exception:
                pass

    si = None
    started_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        _prog('connecting', 0, 'Connecting to vCenter…')
        si = get_si(host, user, password, no_verify_ssl=no_verify_ssl)
        content = si.RetrieveContent()

        _prog('connecting', 2, f'Looking up VM: {vm_name}')
        obj_view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        vm = None
        for v in obj_view.view:
            if v.name == vm_name:
                vm = v
                break
        obj_view.Destroy()
        if not vm:
            raise Exception(f"VM named {vm_name} not found")

        # ── Pre-flight check: consolidation check & orphaned snapshots cleanup ──
        _prog('snapshot', 0, 'Pre-flight check: checking VM disk state…')
        runtime = getattr(vm, 'runtime', None)
        if runtime and getattr(runtime, 'consolidationNeeded', False):
            print("Pre-flight: VM runtime indicates disk consolidation is needed. Consolidating...")
            try:
                task = vm.ConsolidateVMDisks_Task()
                wait_for_task(task, 'ConsolidateVMDisks')
                print("Pre-flight: Consolidation complete.")
            except Exception as ce:
                print(f"Pre-flight WARNING: VM disk consolidation failed: {ce}")

        # Check snapshot tree for orphaned backup snapshots
        snap_root = getattr(vm, 'snapshot', None)
        if snap_root and snap_root.rootSnapshotList:
            orphaned_snaps = find_backup_snapshots(snap_root.rootSnapshotList)
            if orphaned_snaps:
                print(f"Pre-flight: Found {len(orphaned_snaps)} orphaned backup snapshot(s). Cleaning up...")
                for snap_tree in orphaned_snaps:
                    print(f"Pre-flight: Removing orphaned snapshot '{snap_tree.name}'")
                    try:
                        remove_snapshot(snap_tree.snapshot)
                    except Exception as e:
                        print(f"Pre-flight ERROR: Failed to remove orphaned snapshot '{snap_tree.name}': {e}")
                        # Attempt consolidation before aborting
                        try:
                            print("Pre-flight: Triggering VM disk consolidation...")
                            task = vm.ConsolidateVMDisks_Task()
                            wait_for_task(task, 'ConsolidateVMDisks')
                            print("Pre-flight: Consolidation complete.")
                        except Exception as ce:
                            print(f"Pre-flight: Consolidation failed: {ce}")
                        raise Exception(f"Abort backup: VM has orphaned snapshot '{snap_tree.name}' which could not be deleted.")
                
                # Consolidate after deleting all old snapshots to merge deltas
                try:
                    print("Pre-flight: Triggering VM disk consolidation...")
                    task = vm.ConsolidateVMDisks_Task()
                    wait_for_task(task, 'ConsolidateVMDisks')
                    print("Pre-flight: Consolidation complete.")
                except Exception as ce:
                    print(f"Pre-flight: Consolidation failed: {ce}")

        snap_name = f"backup-{int(time.time())}"
        created_snapshot = False

        # ── CBT pre-snapshot setup ────────────────────────────────────────────
        # vm_base_dir is where cbt_state.json lives (shared across all run dirs)
        vm_base_dir = os.path.join(dest, vm_name) if not dest.endswith(vm_name) else dest
        # Normalize: dest passed in is already the run-specific dir (backup-YYYYMMDDHHMMSS)
        # so we go one level up to find the VM base dir for CBT state
        vm_base_dir = str(Path(dest).parent)

        cbt_state = {}
        if use_cbt:
            _prog('snapshot', 1, 'Enabling Changed Block Tracking (CBT)…')
            try:
                enable_cbt(vm, content)
                cbt_state = load_cbt_state(vm_base_dir)
                if cbt_state:
                    print(f"CBT: Found prior state from {cbt_state.get('last_backup_ts', 'unknown')}")
                else:
                    print("CBT: No prior state — this will be a FULL backup (seeding CBT)")
            except Exception as e:
                print(f"CBT: Failed to enable CBT, falling back to full backup: {e}")
                use_cbt = False

        try:
            _prog('snapshot', 3, 'Creating snapshot…')
            create_snapshot(vm, snap_name, desc="Automated backup snapshot", memory=False, quiesce=False)
            created_snapshot = True
            _prog('snapshot', 5, 'Snapshot created')

            session_cookie = extract_session_cookie(si)
            if not session_cookie:
                raise Exception('Could not extract session cookie for downloads')

            # Get VMDK paths and normalize them (strip snapshot suffixes like -000001)
            # so we always request the base VMDKs which vCenter streams as the full data disk
            raw_vmdk_refs = vm_disk_vmdk_paths(vm)
            vmdk_refs = [re.sub(r'-\d{6}\.vmdk$', '.vmdk', r, flags=re.IGNORECASE) for r in raw_vmdk_refs]
            vmx_ref   = vm_config_vmx_path(vm)

            # Build a map of normalized vmdk_ref -> VirtualDisk device for CBT
            disk_devices = {}
            for dev in vm.config.hardware.device:
                if isinstance(dev, vim.vm.device.VirtualDisk):
                    fn = getattr(dev.backing, 'fileName', None)
                    if fn:
                        norm = re.sub(r'-\d{6}\.vmdk$', '.vmdk', fn, flags=re.IGNORECASE)
                        disk_devices[norm] = dev

            # Locate the backup snapshot object for CBT queries
            snap_ref = None
            if use_cbt:
                try:
                    snap_root = getattr(vm, 'snapshot', None)
                    if snap_root and snap_root.rootSnapshotList:
                        snap_ref = find_snapshot_by_name(snap_root.rootSnapshotList, snap_name)
                    if not snap_ref:
                        print("CBT: Could not locate backup snapshot for QueryChangedDiskAreas — falling back to full")
                        use_cbt = False
                except Exception as e:
                    print(f"CBT: Snapshot lookup failed: {e} — falling back to full")
                    use_cbt = False

            # Apply disk filter — only download selected VMDKs
            if disk_filter is not None:
                disk_filter_set = {re.sub(r'-\d{6}\.vmdk$', '.vmdk', f, flags=re.IGNORECASE) for f in disk_filter}
                skipped = []
                filtered_vmdk_refs = []
                for raw_ref, norm_ref in zip(raw_vmdk_refs, vmdk_refs):
                    if norm_ref in disk_filter_set:
                        filtered_vmdk_refs.append(norm_ref)
                    else:
                        skipped.append(raw_ref)
                vmdk_refs = filtered_vmdk_refs
                if skipped:
                    print(f"Skipping {len(skipped)} disk(s) per disk_filter: {skipped}")
                if not vmdk_refs:
                    print("Warning: no disks selected — backing up VMX config only.")

            # ── Build download list ───────────────────────────────────────────
            # Descriptor (.vmdk) + flat data (-flat.vmdk) pairs, plus VMX
            # For CBT mode, we only do range-downloads on the flat file; the
            # small descriptor is always fetched in full.
            all_refs = []
            flat_vmdk_refs = set()   # track which refs are flat data disks
            for ref in vmdk_refs:
                all_refs.append(ref)  # descriptor (small)
                if ref.lower().endswith('.vmdk') and not ref.lower().endswith('-flat.vmdk'):
                    flat_ref = ref[:-5] + '-flat.vmdk'
                    all_refs.append(flat_ref)
                    flat_vmdk_refs.add(flat_ref)
            if vmx_ref:
                all_refs.append(vmx_ref)

            total_files = len(all_refs)
            # Download phase: 5% -> 90%
            DOWNLOAD_START = 5
            DOWNLOAD_END   = 90
            download_range = DOWNLOAD_END - DOWNLOAD_START

            downloaded_files = []
            files_manifest_info = []

            # Track CBT savings across all disks for manifest
            cbt_total_changed_bytes = 0
            cbt_total_disk_bytes = 0
            new_cbt_disk_state = {}   # updated state to persist after success

            for file_idx, ref in enumerate(all_refs):
                if is_cancelled_cb and is_cancelled_cb():
                    raise RuntimeError("Backup cancelled by user")
                ds_name, ds_path = parse_datastore_path(ref)
                dc = find_datacenter_for_datastore(content, ds_name)
                if not dc:
                    raise Exception(f"Datacenter for datastore {ds_name} not found")
                dc_name = dc.name
                safe_path = ds_path.replace('/', os.sep)
                local_file = os.path.join(dest, ds_name, safe_path)

                file_base_pct = DOWNLOAD_START + int((file_idx / total_files) * download_range)
                file_share    = download_range / total_files

                def make_dl_cb(fidx, total, base_pct, share, fname):
                    def _dl_cb(done, total_b):
                        if is_cancelled_cb and is_cancelled_cb():
                            raise RuntimeError("Backup cancelled by user")
                        if total_b > 0:
                            file_pct = done / total_b
                            overall_pct = int(base_pct + file_pct * share)
                            done_mb  = done // (1024 * 1024)
                            total_mb = total_b // (1024 * 1024)
                            detail = (f'File {fidx+1}/{total}: {fname} — '
                                      f'{done_mb} / {total_mb} MB '
                                      f'({int(file_pct*100)}%)')
                        else:
                            overall_pct = base_pct
                            detail = f'File {fidx+1}/{total}: {fname}'
                        _prog('downloading', overall_pct, detail)
                    return _dl_cb

                _prog('downloading', file_base_pct,
                      f'Starting file {file_idx+1}/{total_files}: {os.path.basename(ds_path)}')

                # ── CBT incremental path for flat VMDK data disks ─────────────
                is_flat_disk = ref in flat_vmdk_refs
                did_cbt = False
                file_sha = None
                bytes_downloaded_this_file = None

                if use_cbt and is_flat_disk and snap_ref:
                    # Find the device key for the descriptor that corresponds
                    # to this flat file (descriptor ref = flat_ref without -flat)
                    descriptor_ref = ref[:-len('-flat.vmdk')] + '.vmdk'
                    dev = disk_devices.get(descriptor_ref)

                    if dev:
                        device_key = dev.key
                        prior_disk_state = cbt_state.get('disks', {}).get(ref, {})
                        prior_change_id = prior_disk_state.get('change_id')
                        disk_size_bytes = (getattr(dev, 'capacityInKB', 0) or 0) * 1024

                        if prior_change_id:
                            # ── Incremental: query and download only changes ──
                            print(f"CBT: Incremental mode for {ref} "
                                  f"(prior changeId: {prior_change_id[:20]}…)")
                            try:
                                extents = query_changed_areas(
                                    snap_ref, device_key, prior_change_id
                                )
                                if not extents:
                                    print(f"CBT: No changes detected for {ref} — creating empty delta")
                                    os.makedirs(os.path.dirname(local_file), exist_ok=True)
                                    open(local_file, 'wb').close()
                                    file_sha = hashlib.sha256(b'').hexdigest()
                                    bytes_downloaded_this_file = 0
                                    did_cbt = True
                                else:
                                    total_extent_bytes = sum(e['length'] for e in extents)
                                    cbt_total_changed_bytes += total_extent_bytes
                                    cbt_total_disk_bytes += disk_size_bytes

                                    file_sha, bytes_downloaded_this_file = download_disk_changed_ranges(
                                        host, dc_name, ds_name, ds_path, extents,
                                        local_file, session_cookie,
                                        total_disk_size=disk_size_bytes,
                                        verify_ssl=not no_verify_ssl,
                                        progress_cb=make_dl_cb(
                                            file_idx, total_files,
                                            file_base_pct, file_share,
                                            f"[CBT] {os.path.basename(ds_path)}"
                                        )
                                    )
                                    did_cbt = True

                            except Exception as cbt_err:
                                print(f"CBT: Incremental download failed ({cbt_err}), "
                                      f"falling back to full download for {ref}")
                                did_cbt = False
                        else:
                            # No prior state: this is the seeding full backup
                            print(f"CBT: No prior changeId for {ref} — "
                                  f"performing FULL download to seed CBT")
                            cbt_total_disk_bytes += disk_size_bytes

                        # After snapshot, get the new changeId for next run
                        new_cid = get_disk_change_id(snap_ref, device_key)
                        new_cbt_disk_state[ref] = {
                            'change_id': new_cid or '*',
                            'backup_type': 'incremental' if (did_cbt and prior_change_id) else 'full',
                            'last_snapshot': snap_name,
                        }

                if not did_cbt:
                    # ── Full download path (also used for descriptors & VMX) ──
                    file_sha = download_datastore_file(
                        host, dc_name, ds_name, ds_path, local_file, session_cookie,
                        verify_ssl=not no_verify_ssl,
                        progress_cb=make_dl_cb(file_idx, total_files, file_base_pct,
                                               file_share, os.path.basename(ds_path))
                    )

                downloaded_files.append(local_file)

                file_size = os.path.getsize(local_file)
                print(f"SHA-256: {file_sha} (size: {file_size} bytes)")

                rel_path = os.path.relpath(local_file, dest).replace(os.sep, '/')
                manifest_entry = {
                    "path": rel_path,
                    "size_bytes": file_size,
                    "sha256": file_sha,
                }
                if use_cbt and is_flat_disk and ref in new_cbt_disk_state:
                    disk_st = new_cbt_disk_state[ref]
                    manifest_entry['backup_type'] = disk_st.get('backup_type', 'full')
                    if bytes_downloaded_this_file is not None:
                        manifest_entry['changed_bytes'] = bytes_downloaded_this_file
                files_manifest_info.append(manifest_entry)

            _prog('compressing', 90, 'Downloads complete. Creating manifest…')
            if is_cancelled_cb and is_cancelled_cb():
                raise RuntimeError("Backup cancelled by user")

            # Write manifest.json
            finished_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

            # Determine overall backup type label
            has_incremental = any(
                e.get('backup_type') == 'incremental'
                for e in files_manifest_info
            )
            overall_type = 'incremental' if has_incremental else 'full'

            manifest_data = {
                "job_id": job_id or "...",
                "vm_name": vm_name,
                "started": started_iso,
                "finished": finished_iso,
                "vcenter": host,
                "snapshot": snap_name,
                "backup_type": overall_type,
                "cbt_enabled": use_cbt,
                "files": files_manifest_info
            }

            if use_cbt and cbt_total_disk_bytes > 0:
                savings_pct = round(
                    (1 - cbt_total_changed_bytes / cbt_total_disk_bytes) * 100, 1
                )
                manifest_data['cbt_transfer_savings_pct'] = savings_pct
                manifest_data['cbt_changed_bytes'] = cbt_total_changed_bytes
                manifest_data['cbt_total_disk_bytes'] = cbt_total_disk_bytes
                print(f"CBT summary: {savings_pct}% transfer savings "
                      f"({cbt_total_changed_bytes // (1024*1024)} MB transferred of "
                      f"{cbt_total_disk_bytes // (1024*1024)} MB total)")

            manifest_path = os.path.join(dest, 'manifest.json')
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest_data, f, indent=2)
            print(f"Backup manifest created at {manifest_path}")

            # Persist CBT state for next incremental run
            if use_cbt and new_cbt_disk_state:
                updated_state = {
                    'last_backup_ts': finished_iso,
                    'backup_type': overall_type,
                    'disks': new_cbt_disk_state,
                }
                save_cbt_state(vm_base_dir, updated_state)

            final_files = []
            for f in downloaded_files:
                if is_cancelled_cb and is_cancelled_cb():
                    raise RuntimeError("Backup cancelled by user")
                if compress:
                    _prog('compressing', 92, f'Compressing {os.path.basename(f)}…')
                    cf = maybe_compress(f)
                    final_files.append(cf)
                else:
                    final_files.append(f)

            # manifest.json is added uncompressed
            final_files.append(manifest_path)

            if sftp_host:
                if not sftp_user:
                    raise Exception('SFTP user required')

                if is_cancelled_cb and is_cancelled_cb():
                    raise RuntimeError("Backup cancelled by user")

                # Verify checksums before upload
                _prog('uploading', 94, 'Verifying local checksums before SFTP upload…')
                print("Running pre-upload checksum verification...")
                if not verify_backup_checksums(dest):
                    raise Exception("Pre-upload checksum verification failed. Aborting SFTP upload to prevent remote corruption.")
                print("Checksum verification succeeded.")

                _prog('uploading', 95, f'Uploading to {sftp_host}…')
                for f in final_files:
                    upload_via_sftp(sftp_host, sftp_user, sftp_password, sftp_key, f, os.path.basename(dest))

            _prog('cleanup', 97, 'Removing snapshot…')
            print('Backup completed successfully')
        finally:
            if created_snapshot:
                try:
                    # Re-fetch vm snapshot state to avoid stale object references
                    content.rootFolder  # touch to keep session alive
                    vm_fresh = None
                    obj_view2 = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
                    for v in obj_view2.view:
                        if v.name == vm_name:
                            vm_fresh = v
                            break
                    obj_view2.Destroy()
                    target_vm = vm_fresh or vm
                    snap_root = getattr(target_vm, 'snapshot', None)
                    if snap_root and snap_root.rootSnapshotList:
                        snap_obj = find_snapshot_by_name(snap_root.rootSnapshotList, snap_name)
                        if snap_obj:
                            remove_snapshot(snap_obj)
                        else:
                            print('Snapshot already removed or not found in tree')
                    else:
                        print('No snapshots found on VM — may have already been removed')

                    # Post-flight check: consolidate if needed after removing snapshot
                    runtime = getattr(target_vm, 'runtime', None)
                    if runtime and getattr(runtime, 'consolidationNeeded', False):
                        print("Post-flight: VM runtime indicates disk consolidation is needed. Consolidating...")
                        task = target_vm.ConsolidateVMDisks_Task()
                        wait_for_task(task, 'ConsolidateVMDisks')
                        print("Post-flight consolidation complete.")
                except Exception as e:
                    print(f'Failed to remove snapshot or consolidate disks: {e}', file=sys.stderr)
        _prog('done', 100, 'Backup finished successfully')
    finally:
        if si:
            try:
                Disconnect(si)
            except Exception:
                pass
