import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from stock_targets import TARGETS
from vps_stock_monitor import make_alert, notify_product, run_once, update_product


class NotificationTest(unittest.TestCase):
    def test_browser_only_after_unknown_and_its_confirmation_can_notify(self):
        browser_calls, sent = [], []
        def http(t):
            return {'status': 'unknown' if t == TARGETS[1] else 'unavailable', 'stock': 0}
        def browser(t):
            browser_calls.append(t['id'])
            return {'status': 'available', 'stock': 2}
        with tempfile.TemporaryDirectory() as d:
            data = run_once(Path(d) / 'state.json', http, browser, lambda p, e: sent.append((p['id'], e)))
        self.assertEqual(browser_calls, [TARGETS[1]['id']])
        self.assertEqual(sent, [(TARGETS[1]['id'], 'stock')])
        self.assertEqual(data['products'][1]['query_location'], 'japan-home-vps')

    def test_unknown_does_not_reset_unavailable_episode(self):
        first = update_product(TARGETS[0], {'status': 'unavailable'}, None, '2026-09-21T12:00:00Z')
        missing = update_product(TARGETS[0], {'status': 'unknown'}, first, '2026-09-21T14:00:00Z')
        resumed = update_product(TARGETS[0], {'status': 'unavailable'}, missing, '2026-09-21T15:00:00Z')
        self.assertEqual(missing['unavailable_since'], first['last_checked'])
        self.assertEqual(resumed['unavailable_since'], first['last_checked'])
        restock = update_product(TARGETS[0], {'status': 'available'}, resumed, '2026-09-21T16:00:00Z')
        self.assertIsNone(restock['unavailable_since'])

    def test_each_product_first_stock_restart_and_restock(self):
        calls = []
        states = {t['id']: 'available' for t in TARGETS}
        def check(t):
            return {'status': states[t['id']], 'stock': 2}
        def send(p, event):
            calls.append((p['id'], event))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.json'
            run_once(path, check, check, send)
            self.assertEqual(calls, [(t['id'], 'stock') for t in TARGETS])
            run_once(path, check, check, send)
            self.assertEqual(len(calls), 4)
            states[TARGETS[0]['id']] = 'unknown'
            run_once(path, check, check, send)
            states[TARGETS[0]['id']] = 'available'
            run_once(path, check, check, send)
            self.assertEqual(len(calls), 4)
            states[TARGETS[2]['id']] = 'unavailable'
            run_once(path, check, check, send)
            states[TARGETS[2]['id']] = 'available'
            run_once(path, check, check, send)
            self.assertEqual(calls[-1], (TARGETS[2]['id'], 'stock'))
            self.assertEqual(len(calls), 5)

    def test_smtp_failure_pending_and_does_not_block_other_products(self):
        calls = []
        def send(p, event):
            calls.append(p['id'])
            if p['id'] == TARGETS[0]['id']:
                raise RuntimeError('SECRET PASSWORD SHOULD NOT BE LOGGED')
        check = lambda t: {'status': 'available', 'stock': 2}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.json'
            with self.assertLogs('vps-stock-monitor', level='ERROR') as logs:
                run_once(path, check, check, send)
            self.assertNotIn('SECRET', ' '.join(logs.output))
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual([p['stock_notified'] for p in data['products']], [False, True, True, True])
            calls.clear()
            run_once(path, check, check, lambda p, event: calls.append(p['id']))
            self.assertEqual(calls, [TARGETS[0]['id']])

    def test_error_threshold_retry_recovery_and_dedup(self):
        calls = []
        previous = None
        def step(status, sender=None):
            nonlocal previous
            previous = update_product(TARGETS[1], {'status': status}, previous, '2026-09-21T13:00:00Z')
            notify_product(previous, sender or (lambda p, e: calls.append(e)), 3)
        step('unknown'); step('unknown')
        self.assertEqual(calls, [])
        def fail(p, event):
            raise OSError('test')
        with self.assertLogs('vps-stock-monitor', level='ERROR'):
            step('unknown', fail)
        self.assertFalse(previous['error_notified'])
        step('unknown'); step('unknown')
        self.assertEqual(calls, ['error'])
        step('unavailable')
        self.assertFalse(previous['error_notified'])
        step('unknown'); step('unknown'); step('unknown')
        self.assertEqual(calls, ['error', 'error'])

    def test_corrupt_state_uses_silent_baseline(self):
        calls = []
        check = lambda t: {'status': 'available', 'stock': 1}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.json'
            path.write_text('broken')
            with self.assertLogs('vps-stock-monitor', level='ERROR'):
                run_once(path, check, check, lambda p, e: calls.append(e))
            run_once(path, check, check, lambda p, e: calls.append(e))
            self.assertEqual(calls, [])
            self.assertEqual(len(list(Path(d).glob('*.corrupt-*'))), 1)

    def test_message_contents_and_no_credentials(self):
        config = SimpleNamespace(smtp_from='sender@example.com', mail_to='recipient@example.com', smtp_password='SECRET')
        for target in TARGETS:
            product = update_product(target, {'status': 'available', 'stock': 3}, None, '2026-09-21T13:00:00Z')
            msg = make_alert(product, 'stock', config)
            self.assertIn(target['provider'], str(msg['Subject']))
            self.assertIn(target['product_name'], msg.get_content())
            self.assertIn(target['product_url'], msg.get_content())
            self.assertIn('21:00:00+08:00', msg.get_content())
            self.assertIn('3 Available', msg.get_content())
            self.assertNotIn('SECRET', msg.as_string())


if __name__ == '__main__':
    unittest.main()
