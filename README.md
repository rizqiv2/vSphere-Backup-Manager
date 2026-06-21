# vSphere Backup Manager

An enterprise-ready web interface and CLI tool to automate, schedule, and manage snapshot-based backups for virtual machines on VMware vCenter/ESXi. Designed for performance, reliability, and security, it includes advanced features such as Change-based checksumming, automated retention policies, and grouped batch executions.

---

## Key Features

- **Grouped Sequential Batch Backups**: Select multiple VMs to execute sequentially in a single job. This protects vCenter/ESXi storage datastores from network I/O congestion and merges execution logs and progress indicators into a single view.
- **SHA-256 Checksum Verification & Cataloging**: Computes SHA-256 signatures immediately after each VMDK/VMX file download and generates a machine-readable `manifest.json` catalog alongside each backup run.
- **Pre-Upload Validation**: Automatically validates local checksums prior to remote transfers (e.g., SFTP) to protect storage vaults against silent write errors or network package loss.
- **On-the-Fly ZST Verification**: Supports stream-decompression on the fly to verify `.zst` archives against original manifest signatures without needing local disk extraction.
- **Safe Force Stop (Cancellation)**: Safely halt running backups via the Web UI. The engine immediately aborts socket downloads and **automatically cleans up the VM snapshot** on the ESXi host before gracefully terminating.
- **Automated Retention Policies**: Define count-based (`keep_count` to keep the last $N$ backups) or age-based (`keep_days` to clean up backups older than $N$ days) retention policies per VM to manage storage space automatically.
- **Resilient Scheduling**: Uses APScheduler to schedule daily, weekly, monthly (with specific weekday or day number rules), or interval backups. Schedules are written to disk (`jobs.json`) and automatically re-registered upon app restarts.
- **Integrated NFS Mount Manager**: View, mount, and manage NFS/CIFS shares directly from the Web GUI, showing real-time mount statuses, total size, used capacity, and free disk space.

---

## Requirements

- Python 3.8+
- System packages listed in `requirements.txt`:
  - `pyvmomi` (VMware vSphere API Python SDK)
  - `requests` (vCenter HTTPS folder API transfers)
  - `paramiko` (SFTP remote storage replication)
  - `zstandard` (High-ratio backup compression)
  - `APScheduler` (Recurring backups scheduling)

---

## Installation

1. **Clone the repository**:
   ```bash
   git clone <repository_url>
   cd backupvmware
   ```

2. **Set up a Python Virtual Environment**:
   - **Linux**:
     ```bash
     python3 -m venv venv
     source venv/bin/activate
     ```
   - **Windows**:
     ```powershell
     python -m venv venv
     .\venv\Scripts\Activate.ps1
     ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

---

## Web GUI Setup

A Flask-based web interface utilizing a premium glassmorphic dark theme to manage backups, schedules, mounts, and real-time logs.

### Running with PM2 (Recommended for Production)

PM2 natively supports Python applications and keeps the server running across restarts or process crashes.

1. **Install PM2** (requires Node.js):
   ```bash
   npm install -g pm2
   ```

2. **Start the Web GUI**:
   Using the provided `ecosystem.config.js`:
   ```bash
   pm2 start ecosystem.config.js
   ```

   *(Optional)* If you are running inside a Python virtual environment (e.g. `venv`), edit `ecosystem.config.js` to point the `interpreter` to your venv's python executable:
   ```javascript
   interpreter: './venv/bin/python3'
   ```

3. **Useful PM2 Commands**:
   - **Status Dashboard**: `pm2 status`
   - **Real-time Console Logs**: `pm2 logs vsphere-backup-manager`
   - **Restart Application**: `pm2 restart vsphere-backup-manager`
   - **Stop Application**: `pm2 stop vsphere-backup-manager`
   - **Enable Auto-start on Boot**: Run `pm2 startup` and execute the command it prints, followed by `pm2 save`.

---

## CLI Usage

You can also execute standalone backups directly from the command line:

### Basic Backup
```bash
python vsphere_backup.py --host vc.example.com --user administrator@vsphere.local --vm MyVM --dest /mnt/nfs-backup --compress
```

### Backup with Remote SFTP Replication
```bash
python vsphere_backup.py --host vc.example.com --user administrator@vsphere.local --vm MyVM --dest /tmp/backups --sftp-host backup-vault.local --sftp-user vault-user --sftp-password vault-pass
```

---

## Safety & Architecture

1. **Snapshot Isolation**: The backup engine creates a temporary snapshot on the target VM, downloads the locked base files (such as `.vmdk` descriptors, `-flat.vmdk` disk data, and `.vmx` configurations) directly from the Datastore HTTP gateway, and deletes the snapshot immediately afterwards.
2. **SSL Configuration**: Custom certificate verification options (`--no-verify-ssl` or Web checkbox) allow connecting to environments using self-signed vCenter certificates.
3. **Database Integrity**: Job records, statuses, and scheduling data are written safely using thread-safe synchronization locks to prevent state corruption.
