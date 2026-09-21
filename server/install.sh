#!/bin/bash
# Run as root after uploading this complete server directory to the VPS.
set -euo pipefail
source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
destination=/opt/vmiss-public-status
python=/opt/vmiss-stock-monitor/.venv/bin/python
test "$(id -u)" -eq 0
test -x "$python"
id vmiss-monitor >/dev/null
"$python" -c 'import dotenv, playwright.sync_api'
backup=/var/backups/vps-monitor/$(date -u +%Y%m%dT%H%M%SZ)
install -d -m 700 "$backup"
cp -a "$destination" "$backup/public-status" 2>/dev/null || true
cp -a /opt/vmiss-stock-monitor/.env "$backup/vmiss.env"
for state_dir in /var/lib/vps-stock-monitor /var/lib/vmiss-public-status; do
    if [ -d "$state_dir" ]; then cp -a "$state_dir" "$backup/"; fi
done
# Keep publishing saved status even if a new checker fails during installation.
trap 'systemctl start vmiss-public-status.timer || true' EXIT
systemctl stop vmiss-public-status.timer
systemctl stop vmiss-public-status.service
# Stop any earlier VPS version before replacing it.
if systemctl cat http-stock-monitor.timer >/dev/null 2>&1; then
    systemctl disable --now http-stock-monitor.timer
fi
if systemctl cat http-stock-monitor.service >/dev/null 2>&1; then
    systemctl stop http-stock-monitor.service
fi
systemctl stop vps-stock-monitor.timer vps-stock-monitor.service 2>/dev/null || true
install -d -m 755 "$destination"
for file in stock_targets.py vps_stock_monitor.py export_status.py; do
    install -m 644 "$source_dir/$file" "$destination/$file"
done
for unit in vps-stock-monitor.service vps-stock-monitor.timer vmiss-public-status.service vmiss-public-status.timer; do
    install -m 644 "$source_dir/$unit" "/etc/systemd/system/$unit"
done
install -d -o vmiss-monitor -g vmiss-monitor -m 700 /var/lib/vps-stock-monitor
if [ -f /var/lib/vps-stock-monitor/http-status.json ]; then
    chown vmiss-monitor:vmiss-monitor /var/lib/vps-stock-monitor/http-status.json
fi
systemctl daemon-reload
# Change only the interval; preserve SMTP credentials and VMISS state.
"$python" - <<'PY'
from dotenv import set_key
from pathlib import Path
import os
path = Path('/opt/vmiss-stock-monitor/.env')
owner = path.stat()
set_key(str(path), 'CHECK_INTERVAL_SECONDS', '300', quote_mode='never')
os.chown(path, owner.st_uid, owner.st_gid)
PY
chmod 600 /opt/vmiss-stock-monitor/.env
systemctl restart vmiss-stock-monitor
systemctl start vps-stock-monitor.service
systemctl enable --now vps-stock-monitor.timer
systemctl start vmiss-public-status.service
systemctl enable --now vmiss-public-status.timer
printf 'Installed VPS checks. Backup: %s\n' "$backup"
systemctl is-active vmiss-stock-monitor vps-stock-monitor.timer vmiss-public-status.timer
