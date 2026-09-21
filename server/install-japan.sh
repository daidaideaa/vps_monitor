#!/bin/bash
# Run from an uploaded server/ directory after installing VMISS and Chromium.
set -euo pipefail
cd -- "$(dirname -- "$0")"
test "$(id -u)" = 0
test -x /opt/vmiss-stock-monitor/.venv/bin/python
test -f /opt/vmiss-stock-monitor/monitor.py
test -f /opt/vmiss-stock-monitor/.env
test -f /etc/vps-status-publisher.env
id vmiss-monitor >/dev/null
install -d -m 755 /opt/vmiss-public-status
install -d -o vmiss-monitor -g vmiss-monitor -m 700 /var/lib/vps-stock-monitor
install -d -o vmiss-monitor -g vmiss-monitor -m 755 /var/lib/vmiss-public-status
install -m 644 stock_targets.py export_status.py vps_stock_monitor.py run_japan_cycle.py publish_status.py /opt/vmiss-public-status/
install -m 644 vps-stock-japan.service vps-stock-japan.timer vps-status-publish.service vps-status-publish.timer /etc/systemd/system/
chown vmiss-monitor:vmiss-monitor /etc/vps-status-publisher.env /opt/vmiss-stock-monitor/.env
chmod 600 /etc/vps-status-publisher.env /opt/vmiss-stock-monitor/.env
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/vps-stock-japan.service /etc/systemd/system/vps-status-publish.service
echo 'Installed. Validate one round before enabling vps-stock-japan.timer and vps-status-publish.timer.'
