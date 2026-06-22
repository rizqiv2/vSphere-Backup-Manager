#!/usr/bin/env bash
# =============================================================================
#  setup.sh — One-time setup for vSphere Backup Manager (Nginx + Gunicorn + HTTPS)
#  Run as a user with sudo privileges.
#  Usage:  bash setup.sh
# =============================================================================
set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSL_DIR="/etc/ssl/vsphere-backup"
NGINX_CONF="/etc/nginx/sites-available/vsphere-backup-manager"
NGINX_LINK="/etc/nginx/sites-enabled/vsphere-backup-manager"

echo ""
echo "=========================================="
echo "  vSphere Backup Manager — Setup Script"
echo "=========================================="
echo ""

# ── 1. Install system packages ────────────────────────────────────────────────
echo "[1/6] Installing Nginx (forcing IPv4 to avoid IPv6 timeouts)..."
sudo apt-get update
sudo apt-get install -y nginx

# ── 2. Install Python packages ────────────────────────────────────────────────
echo "[2/6] Installing Python dependencies (including gunicorn)..."
# If you use a virtualenv, activate it first:
#   source ./venv/bin/activate
pip install -r "$APP_DIR/requirements.txt"

# ── 3. Generate self-signed SSL certificate (10-year validity) ────────────────
echo "[3/6] Generating self-signed TLS certificate (10 years)..."
sudo mkdir -p "$SSL_DIR"
sudo openssl req -x509 -nodes -newkey rsa:4096 \
    -days 3650 \
    -keyout "$SSL_DIR/key.pem" \
    -out    "$SSL_DIR/cert.pem" \
    -subj "/C=XX/ST=Local/L=Local/O=vSphereBackup/CN=vsphere-backup-manager" \
    -addext "subjectAltName=IP:127.0.0.1,IP:$(hostname -I | awk '{print $1}')"
sudo chmod 600 "$SSL_DIR/key.pem"
sudo chmod 644 "$SSL_DIR/cert.pem"
echo "    Certificate saved to: $SSL_DIR"

# ── 4. Install Nginx config ───────────────────────────────────────────────────
echo "[4/6] Installing Nginx config..."
sudo cp "$APP_DIR/nginx.conf" "$NGINX_CONF"
# Remove default site if it exists
sudo rm -f /etc/nginx/sites-enabled/default
# Create symlink to enable our site
sudo ln -sf "$NGINX_CONF" "$NGINX_LINK"
sudo nginx -t                      # validate config before applying
sudo systemctl enable nginx
sudo systemctl restart nginx
echo "    Nginx configured and running."

# ── 5. Create logs directory ──────────────────────────────────────────────────
echo "[5/6] Creating logs directory..."
mkdir -p "$APP_DIR/logs"

# ── 6. (Re)start with PM2 ────────────────────────────────────────────────────
echo "[6/6] Starting app with PM2..."
cd "$APP_DIR"
# Stop old instance if running (ignore error if not)
pm2 delete vsphere-backup-manager 2>/dev/null || true
pm2 start ecosystem.config.js
pm2 save

echo ""
echo "=========================================="
echo "  Setup complete!"
echo ""
echo "  Access the app at:"
echo "    https://$(hostname -I | awk '{print $1}')"
echo ""
echo "  First visit: browser will warn about self-signed cert."
echo "  Click: Advanced → Proceed  (one-time per browser)"
echo "=========================================="
echo ""
