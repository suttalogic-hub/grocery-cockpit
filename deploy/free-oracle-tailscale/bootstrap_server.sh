#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/grocery-cockpit}"
APP_USER="${APP_USER:-grocery}"

if [[ ! -d "$APP_DIR" ]]; then
  echo "Expected app directory at $APP_DIR"
  echo "Copy/unzip Grocery Cockpit there first, then rerun this script."
  exit 1
fi

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  ca-certificates \
  chromium \
  curl \
  fluxbox \
  fonts-liberation \
  fonts-noto-color-emoji \
  git \
  npm \
  python3 \
  python3-venv \
  rsync \
  tailscale \
  unzip \
  websockify \
  x11vnc \
  xvfb

if ! node -e "process.exit(Number(process.versions.node.split('.')[0]) >= 18 ? 0 : 1)" >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi

sudo useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER" 2>/dev/null || true
sudo mkdir -p "$APP_DIR/data"
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"

cd "$APP_DIR"
sudo -u "$APP_USER" npm ci --omit=dev

sudo cp deploy/free-oracle-tailscale/grocery-display.service /etc/systemd/system/grocery-display.service
sudo cp deploy/free-oracle-tailscale/grocery-fluxbox.service /etc/systemd/system/grocery-fluxbox.service
sudo cp deploy/free-oracle-tailscale/grocery-x11vnc.service /etc/systemd/system/grocery-x11vnc.service
sudo cp deploy/free-oracle-tailscale/grocery-novnc.service /etc/systemd/system/grocery-novnc.service
sudo cp deploy/free-oracle-tailscale/grocery-cockpit.service /etc/systemd/system/grocery-cockpit.service

sudo systemctl daemon-reload
sudo systemctl enable --now grocery-display grocery-fluxbox grocery-x11vnc grocery-novnc grocery-cockpit

echo ""
echo "Grocery Cockpit services are installed."
echo ""
echo "Next commands on this server:"
echo "  sudo tailscale up --ssh --hostname grocery-cockpit"
echo "  sudo tailscale serve --bg --https=443 http://127.0.0.1:8877"
echo "  sudo tailscale serve --bg --https=8443 http://127.0.0.1:6080"
echo ""
echo "Dashboard: use the HTTPS URL shown by 'tailscale serve status'."
echo "Setup browser/VNC: use the HTTPS :8443 URL shown by Tailscale when provider login is needed."
