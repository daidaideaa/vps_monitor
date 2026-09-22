"""Exercise the actual bundled monitor API without browsers or real email."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
sys.path.insert(0, str(ROOT / 'vmiss-stock-monitor'))
import monitor
import run_japan_cycle
import vps_stock_monitor
from export_status import snapshot


class IntegrationTests(unittest.TestCase):
    def test_runner_uses_bundled_api(self):
        cfg = monitor.Config()
        with patch.dict(os.environ, {}), tempfile.TemporaryDirectory() as tmp, patch.object(monitor, 'ROOT', Path(tmp)), \
             patch.object(monitor, 'load_config', return_value=cfg), \
             patch.object(monitor, 'check_stock', return_value=monitor.Result('unavailable', '0 Available')), \
             patch.object(run_japan_cycle, 'run_once') as others:
            run_japan_cycle.main()
            self.assertEqual(json.loads((Path(tmp)/'state.json').read_text())['last_status'], 'unavailable')
            self.assertEqual(others.call_args.kwargs['error_after'], cfg.error_after)

    def test_legacy_state_preserves_alerts_and_creates_backup(self):
        cfg = monitor.Config()
        old = {'version': 1, 'target': {'product_name': cfg.product_name, 'product_url': cfg.product_url},
               'last_confirmed': 'available', 'unknown_count': 4, 'stock_notified': True,
               'error_notified': True, 'last_checked': '2026-09-22T00:00:00Z',
               'last_unknown_reason': 'timeout', 'baseline_required': False}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.json'; path.write_text(json.dumps(old))
            state = monitor.load_state(path, cfg)
            self.assertTrue(state['alerted_for_current_stock'])
            self.assertEqual(json.loads(path.read_text()), old)
            sent = []
            updated = monitor.process_result(cfg, state, monitor.Result('available', '1 Available'), path,
                                              sender=lambda *args: sent.append(args))
            self.assertFalse(sent)
            reloaded = monitor.load_state(path, cfg)
            self.assertEqual(reloaded['consecutive_errors'], 0)
            self.assertEqual(json.loads(path.with_name('state.json.before-repo-split').read_text()), old)
            public = snapshot(updated)
            self.assertEqual(public['status'], 'available')
            self.assertEqual(public['last_checked'], updated['last_check'])

    def test_new_smtp_signature_and_failure(self):
        cfg = monitor.Config(smtp_from='from@example.invalid', mail_to='to@example.invalid')
        product = {**vps_stock_monitor.TARGETS[0], 'last_checked': '2026-09-22T00:00:00Z', 'stock': 1}
        with patch.object(vps_stock_monitor, 'mail_config', return_value=(cfg, lambda *args: False)):
            with self.assertRaises(RuntimeError):
                vps_stock_monitor.send_alert(product, 'stock')


if __name__ == '__main__':
    unittest.main()
