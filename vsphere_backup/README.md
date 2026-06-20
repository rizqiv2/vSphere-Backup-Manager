# vSphere Snapshot Backup Tool

Simple CLI to automate the snapshot -> copy -> compress -> delete workflow for a VM on vCenter/ESXi.

Requirements
- Python 3.8+
- See `requirements.txt` (pyvmomi, requests, paramiko, zstandard)

Basic usage

```bash
python vsphere_backup.py --host vc.example.local --user administrator@vsphere.local --vm MyVM --dest /backups/MyVM --compress
```

Optional SFTP upload

```bash
python vsphere_backup.py --host vc.example.local --user admin --vm MyVM --dest /tmp/backups --sftp-host backup.example.com --sftp-user backup --sftp-password secret
```

Notes & caveats
- The script creates a snapshot on the VM and downloads the VM's `.vmdk` and `.vmx` files from the datastore while the snapshot exists — do NOT copy `.vmdk` without snapshot.
- The script attempts to use `zstd -19` if available; otherwise it falls back to Python `zstandard`.
- SSL verification is disabled with `--no-verify-ssl` for convenience with self-signed vCenter/ESXi certs.
- Test carefully in dev before using in production. This is a minimal DIY backup tool and does not replace a full backup product.
