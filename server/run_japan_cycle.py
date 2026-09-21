"""Run all five targets sequentially on the small Japan VPS, preserving state."""
import fcntl
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

os.environ['QUERY_LOCATION'] = 'japan-home-vps'
os.environ.pop('JAPAN_PROXY_URL', None)
sys.path.insert(0, '/opt/vmiss-stock-monitor')
import monitor
from vps_stock_monitor import run_once, send_alert, STATE, LOG as STOCK_LOG

def main():
    cfg = replace(monitor.load_config(require_smtp=True), japan_proxy_url='')
    monitor.setup_logging(False, cfg.smtp_password)
    STOCK_LOG.handlers[:] = monitor.LOG.handlers
    STOCK_LOG.setLevel(logging.INFO)
    STOCK_LOG.propagate = False
    with STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with monitor.single_instance():
            path = monitor.ROOT / 'state.json'
            state = monitor.load_state(path, cfg)
            monitor.LOG.info('Checking %s directly on JP-HOME-HY2',cfg.product_name)
            result = replace(monitor.monitored_check(cfg,state,path),query_location='japan-home-vps')
            monitor.log_result(result)
            monitor.process_result(cfg,state,result,path)
        run_once(sender=send_alert,error_after=cfg.error_alert_after)

if __name__ == '__main__': main()
