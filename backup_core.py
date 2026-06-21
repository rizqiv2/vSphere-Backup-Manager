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
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import requests
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

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
    while task.info.state == vim.TaskInfo.State.running:
        time.sleep(1)
    if task.info.state == vim.TaskInfo.State.success:
        return task.info.result
    else:
        raise Exception(f"{action_name} did not complete successfully: {task.info.error}")


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
    """Download a file from a vSphere datastore. progress_cb(bytes_done, bytes_total) is optional."""
    # Keep slashes unencoded (safe='/') — vCenter's /folder/ API requires them in the URL path.
    encoded_path = urllib.parse.quote(ds_path, safe='/')
    url = (f"https://{host}/folder/{encoded_path}"
           f"?dcPath={urllib.parse.quote(dc_name)}&dsName={urllib.parse.quote(datastore_name)}")
    headers = {"Cookie": f"vmware_soap_session={session_cookie}"}
    print(f"Downloading {ds_path} from datastore {datastore_name} to {local_path}")
    print(f"  URL: {url}")
    with requests.get(url, headers=headers, stream=True, verify=verify_ssl, proxies={"http": None, "https": None}) as r:
        r.raise_for_status()
        total_bytes = int(r.headers.get('Content-Length', 0))
        print(f"  HTTP {r.status_code}, Content-Length: {total_bytes} bytes")
        done_bytes = 0
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    done_bytes += len(chunk)
                    if progress_cb:
                        progress_cb(done_bytes, total_bytes)
    print(f"Download completed ({done_bytes // (1024*1024)} MB)")


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


def remove_snapshot(snapshot_obj):
    print("Removing snapshot")
    task = snapshot_obj.RemoveSnapshot_Task(removeChildren=False)
    wait_for_task(task, 'RemoveSnapshot')
    print("Snapshot removed")


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
        rc = subprocess.run(['zstd', '-19', path], check=False)
        if rc.returncode == 0:
            return path + '.zst'
    except FileNotFoundError:
        pass
    try:
        import zstandard as zstd
        out_path = path + '.zst'
        with open(path, 'rb') as ifh, open(out_path, 'wb') as ofh:
            cctx = zstd.ZstdCompressor(level=19)
            cctx.copy_stream(ifh, ofh)
        return out_path
    except Exception:
        print('Compression not available; skipping')
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
               log_path=None, progress_cb=None, disk_filter=None, job_id=None):
    """Run full backup flow.
    disk_filter: if not None, a set/list of VMDK file-ref strings to include.
                 The VMX config file is always included regardless.
    """
    if log_path:
        logfile = open(log_path, 'a', encoding='utf-8', buffering=1)
        def _wrap():
            with redirect_stdout(logfile), redirect_stderr(logfile):
                return _run_backup_impl(host, user, password, vm_name, dest, compress, no_verify_ssl,
                                        sftp_host, sftp_user, sftp_password, sftp_key,
                                        progress_cb=progress_cb, disk_filter=disk_filter, job_id=job_id)
        try:
            return _wrap()
        finally:
            logfile.close()
    else:
        return _run_backup_impl(host, user, password, vm_name, dest, compress, no_verify_ssl,
                                sftp_host, sftp_user, sftp_password, sftp_key,
                                progress_cb=progress_cb, disk_filter=disk_filter, job_id=job_id)


def _run_backup_impl(host, user, password, vm_name, dest, compress, no_verify_ssl,
                     sftp_host, sftp_user, sftp_password, sftp_key,
                     progress_cb=None, disk_filter=None, job_id=None):
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

        snap_name = f"backup-{int(time.time())}"
        created_snapshot = False
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
            vmdk_refs = [re.sub(r'-\d+\.vmdk$', '.vmdk', r, flags=re.IGNORECASE) for r in raw_vmdk_refs]
            vmx_ref   = vm_config_vmx_path(vm)

            # Apply disk filter — only download selected VMDKs
            if disk_filter is not None:
                disk_filter_set = {re.sub(r'-\d+\.vmdk$', '.vmdk', f, flags=re.IGNORECASE) for f in disk_filter}
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

            all_refs = []
            for ref in vmdk_refs:
                all_refs.append(ref)
                if ref.lower().endswith('.vmdk') and not ref.lower().endswith('-flat.vmdk'):
                    all_refs.append(ref[:-5] + '-flat.vmdk')
            if vmx_ref:
                all_refs.append(vmx_ref)

            total_files = len(all_refs)
            # Download phase: 5% -> 90%
            DOWNLOAD_START = 5
            DOWNLOAD_END   = 90
            download_range = DOWNLOAD_END - DOWNLOAD_START

            downloaded_files = []
            files_manifest_info = []
            for file_idx, ref in enumerate(all_refs):
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
                download_datastore_file(
                    host, dc_name, ds_name, ds_path, local_file, session_cookie,
                    verify_ssl=not no_verify_ssl,
                    progress_cb=make_dl_cb(file_idx, total_files, file_base_pct,
                                           file_share, os.path.basename(ds_path))
                )
                downloaded_files.append(local_file)

                # Compute checksum immediately after download
                _prog('downloading', file_base_pct + int(file_share * 0.95), f'Calculating checksum for {os.path.basename(ds_path)}…')
                print(f"Calculating SHA-256 checksum for {local_file}")
                t0 = time.time()
                file_sha = get_file_sha256(local_file)
                file_size = os.path.getsize(local_file)
                print(f"SHA-256: {file_sha} (size: {file_size} bytes, took {time.time() - t0:.2f}s)")

                # Relative path from dest directory using forward slashes (e.g. "datastore1/Nakivo/Nakivo.vmdk")
                rel_path = os.path.relpath(local_file, dest).replace(os.sep, '/')
                files_manifest_info.append({
                    "path": rel_path,
                    "size_bytes": file_size,
                    "sha256": file_sha
                })

            _prog('compressing', 90, 'Downloads complete. Creating manifest…')

            # Write manifest.json
            finished_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
            manifest_data = {
                "job_id": job_id or "...",
                "vm_name": vm_name,
                "started": started_iso,
                "finished": finished_iso,
                "vcenter": host,
                "snapshot": snap_name,
                "files": files_manifest_info
            }
            manifest_path = os.path.join(dest, 'manifest.json')
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest_data, f, indent=2)
            print(f"Backup manifest created at {manifest_path}")

            final_files = []
            for f in downloaded_files:
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
                except Exception as e:
                    print(f'Failed to remove snapshot: {e}', file=sys.stderr)
        _prog('done', 100, 'Backup finished successfully')
    finally:
        if si:
            try:
                Disconnect(si)
            except Exception:
                pass
