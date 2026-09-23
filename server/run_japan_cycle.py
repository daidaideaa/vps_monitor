"""Run all three targets sequentially on the small Japan VPS, preserving state."""
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get('VMISS_MONITOR_ROOT', str(Path(__file__).resolve().parents[1] / 'vmiss-stock-monitor')))
import monitor
from vps_stock_monitor import run_once, send_alert, LOG as STOCK_LOG
from access_policy import retry_remaining

def main():
    os.environ['QUERY_LOCATION'] = 'japan-home-vps'
    os.environ.pop('JAPAN_PROXY_URL', None)
    cfg = monitor.load_config(require_smtp=True)
    monitor.setup_logging(False)
    STOCK_LOG.handlers[:] = monitor.LOG.handlers
    STOCK_LOG.setLevel(logging.INFO)
    STOCK_LOG.propagate = False
    # Same lock as the standalone monitor, held for the complete cycle.
    with monitor.single_instance():
        path = monitor.ROOT / 'state.json'
        state = monitor.load_state(path, cfg)
        if not retry_remaining(state):
            monitor.LOG.info('Checking %s directly on Japan VPS', cfg.product_name)
            result = monitor.check_stock(cfg)
            monitor.log_result(cfg, result)
            monitor.process_result(cfg, state, result, path)
        run_once(sender=send_alert, error_after=cfg.error_after)

if __name__ == '__main__': main()
