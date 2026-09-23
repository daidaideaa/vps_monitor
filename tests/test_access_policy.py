"""Offline regression checks for request pacing and final-document handling."""
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import vps_stock_monitor as stock
import access_policy as policy
import run_japan_cycle as cycle


class ObservationTest(unittest.TestCase):
    def observe(self, responses, parsed=None, timeout=False):
        clock = [100.0]
        page = MagicMock()
        page.url = 'https://my.rfchost.com/index.php?secret=hidden#private'
        page.title.return_value = 'Products'
        main = object()
        page.main_frame = main
        handler = []
        page.on.side_effect = lambda event, callback: handler.append(callback)
        def response(status, headers):
            return SimpleNamespace(status=status, headers=headers, frame=main,
                                   request=SimpleNamespace(is_navigation_request=lambda: True))
        docs = [response(code, {'content-type': 'text/html', **headers}) for code, headers in responses]
        def goto(*args, **kwargs):
            handler[0](docs.pop(0))
            if timeout:
                raise TimeoutError('private URL')
        def wait(ms):
            clock[0] += ms / 1000
            if docs:
                handler[0](docs.pop(0))
        page.goto.side_effect = goto
        page.wait_for_timeout.side_effect = wait
        parse = MagicMock(return_value=parsed or {'status': 'available', 'stock': 2})
        with patch.object(policy.time, 'monotonic', side_effect=lambda: clock[0]):
            result = policy.observe_page(page, page.url, 'RFCHOST', parse, lambda url: True, 2000)
        page.goto.assert_called_once()
        page.reload.assert_not_called()
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('private', json.dumps(result))
        return result, parse

    def test_natural_challenge_completion_uses_final_document(self):
        result, _ = self.observe([(403, {'cf-mitigated': 'challenge'}), (200, {'cf-ray': 'abc-NRT'})], timeout=True)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['diagnostics']['http_status'], 200)
        self.assertEqual(result['diagnostics']['cf_ray'], 'abc-NRT')
        self.assertFalse(result['diagnostics']['challenge_observed'])

    def test_403_and_cf_header_never_parse_stock(self):
        for status, headers, category in [(403, {}, 'http_403'), (200, {'cf-mitigated': 'challenge'}, 'cf_mitigated_challenge')]:
            result, parse = self.observe([(status, headers)])
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(result['diagnostics']['failure_category'], category)
            parse.assert_not_called()

    def test_200_challenge_and_parse_failure_are_distinct(self):
        for challenge, category in [(True, 'challenge_page'), (False, 'parse_failure')]:
            result, _ = self.observe([(200, {})], {'status': 'unknown', 'challenge': challenge})
            self.assertEqual(result['diagnostics']['failure_category'], category)

    def test_transport_categories_and_safe_fields(self):
        for exc, category in [(socket.gaierror('private'), 'dns_failure'),
                              (ssl.SSLError('private'), 'tls_failure'),
                              (OSError('private'), 'network_failure'),
                              (TimeoutError('private'), 'browser_timeout')]:
            self.assertEqual(policy.exception_category(exc, True), category)
        diag = policy.diagnostic('VMISS', 403, {'set-cookie': 'secret', 'cf-ray': 'ray-NRT'},
                                 'https://user:password@app.vmiss.com/store?token=secret#fragment')
        self.assertEqual(diag['final_url_host'], 'app.vmiss.com')
        self.assertEqual(diag['final_url_path'], '/store')
        self.assertNotIn('secret', json.dumps(diag))
        self.assertNotIn('password', json.dumps(diag))


class BackoffTest(unittest.TestCase):
    def test_restart_escalation_and_confirmed_recovery(self):
        state = {}
        for expected in (1800, 3600, 7200, 7200):
            state = policy.backoff(json.loads(json.dumps(state)), 'unknown', {'failure_category': 'http_403'}, now=100)
            self.assertEqual(policy.retry_remaining(state, now=100), expected)
        network = policy.backoff(state, 'unknown', {'failure_category': 'network_failure'}, now=100)
        self.assertEqual(network['challenge_consecutive_count'], 4)
        reset = policy.backoff(network, 'unavailable', {}, now=100)
        self.assertEqual(reset['challenge_consecutive_count'], 0)
        self.assertEqual(policy.retry_remaining(reset, now=100), 0)

    def test_rfchost_browser_first_and_backoff_does_not_delay_other_merchants(self):
        target = stock.TARGETS[1]
        http = MagicMock(return_value={'status': 'unavailable', 'stock': 0})
        browser = MagicMock(return_value={'status': 'unknown', 'diagnostics': {'failure_category': 'http_403'}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            first = stock.run_once(path, http, browser)
            old = first['products'][1]
            second = stock.run_once(path, http, browser)
            self.assertEqual(second['products'][1], old)
            self.assertEqual(browser.call_count, 1)
            self.assertEqual(http.call_count, 6)
            self.assertNotIn(target, [c.args[0] for c in http.call_args_list])

    def test_vmiss_backoff_preserves_state_and_runs_other_merchants(self):
        state = {'challenge_next_check_at': policy.time.time() + 1800}
        with patch.object(cycle.monitor, 'single_instance'), patch.object(cycle.monitor, 'load_config'), \
             patch.object(cycle.monitor, 'load_state', return_value=state), patch.object(cycle.monitor, 'setup_logging'), \
             patch.object(cycle.monitor, 'check_stock') as check, patch.object(cycle.monitor, 'process_result') as save, \
             patch.object(cycle, 'run_once') as other:
            cycle.main()
        check.assert_not_called()
        save.assert_not_called()
        other.assert_called_once()

    def test_vmiss_backoff_keeps_stock_dedup_and_resets_on_recovery(self):
        m = cycle.monitor
        cfg = m.Config()
        sender = MagicMock(return_value=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            state = m.process_result(cfg, m.fresh_state(cfg), m.Result('available'), path, sender)
            state = m.process_result(cfg, state, m.Result(diagnostics={'failure_category': 'cf_mitigated_challenge'}), path, sender)
            self.assertTrue(state['alerted_for_current_stock'])
            self.assertEqual(m.load_state(path, cfg)['challenge_consecutive_count'], 1)
            state = m.process_result(cfg, state, m.Result('available'), path, sender)
            self.assertEqual(state['challenge_consecutive_count'], 0)
            self.assertEqual(sender.call_count, 1)
