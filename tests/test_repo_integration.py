"""Exercise the actual bundled monitor API without browsers or real email."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from email.message import Message
from contextlib import nullcontext
from io import BytesIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
sys.path.insert(0, str(ROOT / 'vmiss-stock-monitor'))
import monitor
import run_japan_cycle
import vps_stock_monitor
from export_status import snapshot


class IntegrationTests(unittest.TestCase):
    def test_chinese_stock_count_precedes_order_button(self):
        self.assertEqual(monitor.parse_region('0 可用', ['立即订购']).status, 'unavailable')
        self.assertEqual(monitor.parse_region('3 可用').status, 'available')
        for text in ('-1 可用', '1.5 可用', '3 可用 5 Available', 'NaN 可用'):
            with self.subTest(text=text):
                self.assertEqual(monitor.parse_region(text, ['立即订购']).status, 'unknown')

    def test_http_catalog_jsd_is_not_a_challenge(self):
        response = MagicMock(status=200, url=vps_stock_monitor.TARGETS[1]['product_url'])
        response.headers = Message()
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        response.read.return_value = (b'<div>JP2-CO-Micro-Lite 0 Available JP2-CO-Mini-Lite</div>'
                                    b'<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>')
        with patch.object(vps_stock_monitor, 'build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value = response
            self.assertEqual(vps_stock_monitor.check_http(vps_stock_monitor.TARGETS[1])['status'], 'unavailable')
            response.headers['cf-mitigated'] = 'challenge'
            self.assertEqual(vps_stock_monitor.check_http(vps_stock_monitor.TARGETS[1])['status'], 'unknown')

    def test_vmiss_accepts_normal_navigation_after_initial_challenge(self):
        cfg = monitor.Config()
        with tempfile.TemporaryDirectory() as tmp, patch.object(monitor, 'ROOT', Path(tmp)), patch.object(monitor, 'sync_playwright') as playwright:
            context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            page = MagicMock()
            context.pages = [page]
            page.title.return_value = 'VMISS'
            page.url = cfg.product_url
            page.goto.return_value.status = 403
            page.goto.return_value.headers = {"content-type": "text/html"}
            page.evaluate.return_value = {'title': 'VMISS', 'body': '0 可用', 'challenge': False,
                                          'count': 1, 'text': '0 可用', 'buttons': ['立即订购']}
            def navigated(*args):
                response = MagicMock(status=200, headers={'content-type': 'text/html'}, frame=page.main_frame)
                response.request.is_navigation_request.return_value = True
                page.on.call_args.args[1](response)
            page.wait_for_timeout.side_effect = navigated
            result = monitor.check_stock(cfg)
            self.assertEqual(result.status, 'unavailable')
            page.goto.assert_called_once()

    def test_rfchost_navigates_once_and_accepts_catalog_without_clearance(self):
        target = vps_stock_monitor.TARGETS[1]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(vps_stock_monitor, 'STATE', Path(tmp)/'state.json'), \
             patch.object(vps_stock_monitor, 'virtual_display', return_value=nullcontext()), \
             patch.object(vps_stock_monitor.subprocess, 'Popen') as native, \
             patch.object(vps_stock_monitor, 'build_opener') as opener, \
             patch('playwright.sync_api.sync_playwright') as playwright:
            native.return_value.poll.return_value = None
            opener.return_value.open.return_value = BytesIO(b'[{"type":"page","url":"about:blank"}]')
            context = MagicMock()
            playwright.return_value.__enter__.return_value.chromium.connect_over_cdp.return_value.contexts = [context]
            page = MagicMock(url=target['product_url'])
            context.pages = [page]
            page.content.return_value = ('<div>JP2-CO-Micro-Lite 0 Available JP2-CO-Mini-Lite</div>'
                                         '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>')
            def navigated(*args, **kwargs):
                response = MagicMock(status=200, headers={'content-type': 'text/html'}, frame=page.main_frame)
                response.request.is_navigation_request.return_value = True
                page.on.call_args.args[1](response)
            page.goto.side_effect = navigated
            self.assertEqual(vps_stock_monitor.check_rfchost_browser(target)['status'], 'unavailable')
            page.goto.assert_called_once()
            self.assertEqual(native.call_args.args[0][-1], 'about:blank')
            context.cookies.assert_not_called()

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

