#!/bin/bash
# Fixed sudo entry point. Only root can replace the approved source tree.
set -euo pipefail
[[ $# -eq 0 && $EUID -eq 0 ]]
stage=/opt/vps-monitor-approved
python3 -I - <<'PY'
from pathlib import Path
import stat
root = Path('/opt/vps-monitor-approved')
files = ['vmiss-stock-monitor/monitor.py', 'vmiss-stock-monitor/requirements.txt']
files += ['server/' + name for name in (
    'install-japan.sh', 'stock_targets.py', 'export_status.py', 'vps_stock_monitor.py',
    'run_japan_cycle.py', 'publish_status.py', 'vps-stock-japan.service',
    'vps-stock-japan.timer', 'vps-status-publish.service', 'vps-status-publish.timer')]
for path in [root / relative for relative in files]:
    if not path.is_file() or path.resolve() != path:
        raise SystemExit('Approved source missing or symlinked')
    for item in (path, *path.parents):
        info = item.stat()
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise SystemExit('Approved source must be root-owned and not writable by others')
PY
timer_active=$(systemctl is-active vps-stock-japan.timer || true)
trap 'if [[ "$timer_active" == active ]]; then systemctl start vps-stock-japan.timer; fi' EXIT
systemctl stop vps-stock-japan.timer
# Installer validates, backs up source/units, applies, verifies and rolls back on error.
# It refuses an in-progress stock check and never restarts the VPN.
bash "$stage/server/install-japan.sh" --apply
