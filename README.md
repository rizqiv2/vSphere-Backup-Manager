# vSphere Backup Manager

A web interface and CLI tool to automate, schedule, and manage snapshot-based backups for virtual machines on VMware vCenter/ESXi.


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

## Web GUI

A Flask-based web interface to manage your backups, schedules, and NFS mounts.

### Running with PM2 (Recommended for Linux production)

PM2 natively supports Python applications and can keep the server running across restarts.

1. **Install PM2** (requires Node.js):
   ```bash
   npm install -g pm2
   ```

2. **Start the Web GUI**:
   Using the provided `ecosystem.config.js`:
   ```bash
   pm2 start ecosystem.config.js
   ```

   *(Optional)* If you are running inside a Python virtual environment (e.g. `venv`), edit `ecosystem.config.js` to uncomment and point the `interpreter` to your venv's python executable:
   ```javascript
   interpreter: './venv/bin/python3'
   ```

3. **Useful PM2 Commands**:
   - Status: `pm2 status`
   - Logs: `pm2 logs vsphere-backup-manager`
   - Restart: `pm2 restart vsphere-backup-manager`
   - Stop: `pm2 stop vsphere-backup-manager`
   - Setup auto-start on server boot: `pm2 startup` and then run the command it outputs, followed by `pm2 save`.


