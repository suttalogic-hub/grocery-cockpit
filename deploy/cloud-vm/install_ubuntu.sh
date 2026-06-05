#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  ca-certificates \
  chromium \
  curl \
  fonts-liberation \
  fonts-noto-color-emoji \
  git \
  nodejs \
  npm \
  python3 \
  python3-venv \
  rsync

sudo useradd --system --create-home --shell /usr/sbin/nologin grocery 2>/dev/null || true
sudo mkdir -p /opt/grocery-cockpit /opt/grocery-cockpit/data
sudo chown -R grocery:grocery /opt/grocery-cockpit

echo "Ubuntu packages installed. Copy the app into /opt/grocery-cockpit, then run npm ci and enable the service."
