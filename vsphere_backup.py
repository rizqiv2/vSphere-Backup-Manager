#!/usr/bin/env python3
"""
vsphere_backup.py

Automates a simple VMware vSphere VM backup using snapshots:
- create snapshot
- download VM disk files (.vmdk) and .vmx
- optional compression (zstd) locally
- optional upload to backup server via SFTP
- delete snapshot

Requires: pyvmomi, requests, paramiko, zstandard (optional)
"""

import argparse
import atexit
import getpass
import os
import re
import ssl
import sys
import time
import urllib.parse
from pathlib import Path

import requests
from backup_core import run_backup
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

try:
    import paramiko
except Exception:
    paramiko = None


def parse_args():
    p = argparse.ArgumentParser(description="vSphere VM snapshot + download backup tool")
    p.add_argument("--host", required=True, help="vCenter/ESXi host")
    p.add_argument("--user", required=True, help="Username")
    p.add_argument("--password", help="Password (prompted if omitted)")
    p.add_argument("--vm", required=True, help="VM name to backup")
    p.add_argument("--dest", required=True, help="Local destination directory for backups")
    p.add_argument("--compress", action="store_true", help="Compress downloaded files with zstd -19 if available")
    p.add_argument("--no-verify-ssl", action="store_true", help="Do not verify SSL certs")
    p.add_argument("--sftp-host", help="Optional: upload files to SFTP host")
    p.add_argument("--sftp-user", help="SFTP username")
    p.add_argument("--sftp-password", help="SFTP password (or use key via --sftp-key)")
    p.add_argument("--sftp-key", help="Path to private key for SFTP auth")
    return p.parse_args()


def get_si(host, user, pwd, no_verify_ssl=False):
    context = None
    if no_verify_ssl:
        context = ssl._create_unverified_context()
    si = SmartConnect(host=host, user=user, pwd=pwd, sslContext=context)
    atexit.register(Disconnect, si)
    return si


def find_vm_by_name(content, vm_name):
    obj_view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    for vm in obj_view.view:
        if vm.name == vm_name:
            obj_view.Destroy()
            return vm
    obj_view.Destroy()
    return None


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
        # childEntity can include folders; ensure it's a Datacenter
        if isinstance(dc, vim.Datacenter):
            for ds in dc.datastore:
                if ds.info.name == datastore_name:
                    return dc
    return None


def download_datastore_file(si, host, dc_name, datastore_name, ds_path, local_path, session_cookie, verify_ssl=True):
    # ds_path is like "folder/file.vmdk" without leading slash
    encoded_path = urllib.parse.quote(ds_path, safe='')
    url = f"https://{host}/folder/{encoded_path}?dcPath={urllib.parse.quote(dc_name)}&dsName={urllib.parse.quote(datastore_name)}"
    headers = {"Cookie": f"vmware_soap_session={session_cookie}"}
    print(f"Downloading {ds_path} from datastore {datastore_name} to {local_path}")
    with requests.get(url, headers=headers, stream=True, verify=verify_ssl, proxies={"http": None, "https": None}) as r:
        r.raise_for_status()
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=10 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
    print("Download completed")


def extract_session_cookie(si):
    # si._stub.cookie looks like 'vmware_soap_session="xxx"; Path=/'
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
    # vm.config.files.vmPathName e.g. '[datastore1] vmfolder/vm.vmx'
    return getattr(vm.config.files, 'vmPathName', None)


def parse_datastore_path(ds_file_ref):
    # ds_file_ref like "[datastore1] vmfolder/vm.vmdk"
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


def maybe_compress(path):
    # Try system zstd first
    try:
        import subprocess
        rc = subprocess.run(['zstd', '-19', path], check=False)
        if rc.returncode == 0:
            return path + '.zst'
    except FileNotFoundError:
        pass
    # fallback to python zstandard
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


def main():
    args = parse_args()
    password = args.password or getpass.getpass('Password: ')
    dest = os.path.abspath(args.dest)
    os.makedirs(dest, exist_ok=True)
    # Delegate to backup_core.run_backup which handles logging when called by GUI
    try:
        run_backup(
            args.host,
            args.user,
            password,
            args.vm,
            dest,
            compress=args.compress,
            no_verify_ssl=args.no_verify_ssl,
            sftp_host=args.sftp_host,
            sftp_user=args.sftp_user,
            sftp_password=args.sftp_password,
            sftp_key=args.sftp_key,
        )
    except Exception as e:
        print(f'Backup failed: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
