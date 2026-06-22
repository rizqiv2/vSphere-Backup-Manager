module.exports = {
  apps: [
    {
      name: 'vsphere-backup-manager',
      // Gunicorn inside the virtualenv as the production WSGI server
      script: './venv/bin/gunicorn',
      args: [
        '--workers', '1',
        '--threads', '4',
        '--bind', '0.0.0.0:5000',
        '--timeout', '300',          // long timeout for backup operations
        '--keep-alive', '5',
        '--log-level', 'info',
        '--access-logfile', './logs/gunicorn_access.log',
        '--error-logfile',  './logs/gunicorn_error.log',
        'gui_app:app'
      ].join(' '),
      interpreter: 'none',           // gunicorn is its own executable
      cwd: './',
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',
      env: {
        SECRET_KEY: 'vsphere-backup-production-key-change-this'
        // PORT is no longer used; gunicorn binds via --bind above
      },
      error_file: './logs/pm2_err.log',
      out_file: './logs/pm2_out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss'
    }
  ]
};
