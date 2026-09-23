#!/bin/bash
# Validate -> explicitly apply -> verify. Does not start monitoring or send email.
set -euo pipefail
cd -- "$(dirname -- "$0")"
mode=${1:---validate}
[[ "$mode" == --validate || "$mode" == --apply || "$mode" == --verify ]]
python=/opt/vmiss-stock-monitor/.venv/bin/python
check() {
  test -x "$python"
  test -f /opt/vmiss-stock-monitor/.env
  test -f /etc/vps-status-publisher.env
  id vmiss-monitor >/dev/null
  "$python" -c 'import ast,pathlib; [ast.parse(p.read_text()) for p in list(pathlib.Path(".").glob("*.py"))+list(pathlib.Path("../vmiss-stock-monitor").glob("*.py"))]; import dotenv,playwright'
  VMISS_MONITOR_ROOT="$(cd ../vmiss-stock-monitor && pwd)" "$python" -c 'import run_japan_cycle; from vps_stock_monitor import mail_config, virtual_display; assert callable(virtual_display); assert callable(run_japan_cycle.monitor.check_stock); assert hasattr(run_japan_cycle.monitor.Config(), "error_after")'
  systemd-analyze verify vps-stock-japan.service vps-status-publish.service
}
verify() {
  VMISS_MONITOR_ROOT=/opt/vmiss-stock-monitor PYTHONPATH=/opt/vmiss-public-status "$python" -c 'import run_japan_cycle; assert callable(run_japan_cycle.monitor.check_stock); assert hasattr(run_japan_cycle.monitor.Config(), "error_after")'
  systemd-analyze verify /etc/systemd/system/vps-stock-japan.service /etc/systemd/system/vps-status-publish.service
}
if [[ "$mode" == --verify ]]; then verify; exit; fi
check
if [[ "$mode" == --validate ]]; then echo 'Deployment validation passed; no changes applied.'; exit; fi
test "$(id -u)" = 0
case "$(systemctl show -p ActiveState --value vps-stock-japan.service)" in
  active|activating|deactivating|reloading) echo 'A stock check is running; apply after it finishes.' >&2; exit 1;;
esac
backup=$(mktemp -d /opt/vmiss-stock-monitor/repo-split-backup.XXXXXX)
chmod 700 "$backup"
files=(/opt/vmiss-stock-monitor/monitor.py /opt/vmiss-stock-monitor/access_policy.py /opt/vmiss-stock-monitor/requirements.txt)
for name in stock_targets.py export_status.py vps_stock_monitor.py run_japan_cycle.py publish_status.py; do files+=("/opt/vmiss-public-status/$name"); done
for name in vps-stock-japan.service vps-stock-japan.timer vps-status-publish.service vps-status-publish.timer; do files+=("/etc/systemd/system/$name"); done
for path in "${files[@]}"; do
  if [[ -f "$path" ]]; then
    mkdir -p -- "$backup$(dirname -- "$path")"
    cp -p -- "$path" "$backup$path"
  else
    printf '%s\n' "$path" >> "$backup/new-paths"
  fi
done
rollback() {
  for prefix in opt etc; do [[ ! -d "$backup/$prefix" ]] || cp -a "$backup/$prefix/." "/$prefix/"; done
  if [[ -f "$backup/new-paths" ]]; then while IFS= read -r path; do rm -f -- "$path"; done < "$backup/new-paths"; fi
  systemctl daemon-reload
  echo "Install failed; code/units restored from $backup" >&2
}
trap rollback ERR
install -d -m 755 /opt/vmiss-public-status
install -d -o vmiss-monitor -g vmiss-monitor -m 700 /var/lib/vps-stock-monitor
install -d -o vmiss-monitor -g vmiss-monitor -m 755 /var/lib/vmiss-public-status
install -m 644 ../vmiss-stock-monitor/monitor.py ../vmiss-stock-monitor/access_policy.py ../vmiss-stock-monitor/requirements.txt /opt/vmiss-stock-monitor/
install -m 644 stock_targets.py export_status.py vps_stock_monitor.py run_japan_cycle.py publish_status.py /opt/vmiss-public-status/
install -m 644 vps-stock-japan.service vps-stock-japan.timer vps-status-publish.service vps-status-publish.timer /etc/systemd/system/
systemctl daemon-reload
verify
trap - ERR
echo "Installed and verified; code/unit rollback backup: $backup. Existing timers and VPN were not restarted."
