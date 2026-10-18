#!/usr/bin/env bash
# Update PanelPlay to the latest code from GitHub. Run INSIDE the container as root, or from the Proxmox host:
#
#   pct exec <CTID> -- bash /opt/panelplay/deploy/redeploy.sh
#
# Settings, the catalogue, render history and the voice cache live in /var/lib/panelplay and are untouched.
# Restarting stops a render in progress; it shows as failed and can be queued again.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo ">> Pulling the latest code..."
git pull --ff-only
"$APP_DIR/venv/bin/pip" install -q -r requirements.txt

echo ">> Restarting..."
cp deploy/panelplay.service /etc/systemd/system/panelplay.service
systemctl daemon-reload
systemctl restart panelplay
systemctl is-active panelplay
echo ">> Now at $(git log -1 --format='%h %s')"
