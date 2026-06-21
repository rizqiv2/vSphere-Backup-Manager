module.exports = {
  apps: [
    {
      name: 'vsphere-backup-gui',
      script: 'gui_app.py',
      // If you are using a virtual environment (recommended), set the interpreter to:
      // interpreter: './venv/bin/python3',
      interpreter: 'python3',
      cwd: './',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',
      env: {
        PORT: '5000',
        SECRET_KEY: 'vsphere-backup-production-key-change-this'
      },
      error_file: './logs/pm2_err.log',
      out_file: './logs/pm2_out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss'
    }
  ]
};
