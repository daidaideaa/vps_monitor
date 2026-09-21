import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from vps_stock_monitor import parse_stock, run_once, update_product
from stock_targets import TARGETS
from export_status import combined_snapshot


class ParserTest(unittest.TestCase):
    def test_rfchost(self):
        def parse(card):
            return parse_stock('<div>JP2-CO-Micro-Lite ' + card + ' JP2-CO-Mini-Lite 12 Available</div>', 'RFCHOST')
        self.assertEqual(parse('0 Available'), {'status': 'unavailable', 'stock': 0})
        self.assertEqual(parse('3 Available'), {'status': 'available', 'stock': 3})
        for card in ['0 Available 3 Available', '3 Available Out of Stock', '-1 Available',
                     '1.5 Available', '1,000 Available', 'no stock', 'JP2-CO-Micro-Lite 3 Available',
                     'JP2-CO-Standard 3 Available', '9007199254740992 Available', '3 Available N/A Available']:
            with self.subTest(card=card):
                self.assertEqual(parse(card)['status'], 'unknown')
        self.assertEqual(parse('<!-- 3 Available --><script>3 Available</script>0 Available')['stock'], 0)
        self.assertEqual(parse('Verify you are human 3 Available')['status'], 'unknown')

    def test_zgo(self):
        def parse(card):
            return parse_stock('<div>Tokyo Intel VPS Starter ' + card + ' Standard Continue</div>', 'ZgoCloud')
        self.assertEqual(parse('Out of stock!')['status'], 'unavailable')
        self.assertEqual(parse('<button type="submit">Continue</button>')['status'], 'available')
        for card in ['Continue', '<button disabled>Continue</button>', '<a href="#">Continue</a>',
                     '<div hidden><button>Continue</button></div>',
                     '<button>Continue</button> Out of stock!', 'nothing', 'Pro Continue', 'Starter Continue', 'captcha Continue']:
            self.assertEqual(parse(card)['status'], 'unknown')
        self.assertEqual(parse('<template>Continue</template>Out of stock!')['status'], 'unavailable')
        self.assertEqual(parse_stock('<div>Starter Continue Standard Continue</div>', 'ZgoCloud')['status'], 'unknown')

    def test_missing_or_reversed_boundaries(self):
        for html in ['JP2-CO-Micro-Lite 3 Available', '<div>JP2-CO-Micro-Lite 3 Available</div>',
                     '<div>JP2-CO-Mini-Lite 3 Available JP2-CO-Micro-Lite</div>',
                     '<div class="cf-turnstile">JP2-CO-Micro-Lite 3 Available JP2-CO-Mini-Lite</div>']:
            self.assertEqual(parse_stock(html, 'RFCHOST')['status'], 'unknown')


class StateTest(unittest.TestCase):
    def test_unknown_gap_restock_and_restart_history(self):
        def update(at, status, previous=None):
            return update_product(TARGETS[1], {'status': status, 'stock': 1 if status == 'available' else 0}, previous, '2026-09-21T' + at + '+00:00')
        first = update('00:00:00', 'unavailable')
        second = update('00:03:00', 'unavailable', json.loads(json.dumps(first)))
        self.assertEqual(second['unavailable_since'], first['last_checked'])
        available = update('00:06:00', 'available', second)
        unknown = update('00:09:00', 'unknown', available)
        self.assertEqual(unknown['last_confirmed'], 'available')
        self.assertIsNone(unknown['stock'])
        self.assertIsNone(unknown['unavailable_since'])
        resumed = update('00:12:00', 'unavailable', unknown)
        self.assertEqual(resumed['unavailable_since'], resumed['last_checked'])
        gap = update('00:22:00', 'unavailable', resumed)
        self.assertEqual(gap['unavailable_since'], gap['last_checked'])
        self.assertEqual(gap['last_available_at'], available['last_checked'])

    def test_http_success_skips_browser_unknown_falls_back(self):
        browser_calls = []
        def http(target):
            return {'status': 'unavailable' if target['provider'] == 'ZgoCloud' else 'unknown', 'stock': 0}
        def browser(target):
            browser_calls.append(target['provider'])
            return {'status': 'available', 'stock': 2}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            result = run_once(path, http, browser)
            self.assertEqual(browser_calls, ['RFCHOST'])
            self.assertEqual([p['status'] for p in result['products']], ['unavailable', 'available'])
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), result)

    def test_first_result_survives_later_unexpected_failure(self):
        def http(target):
            if target['provider'] == 'RFCHOST':
                raise RuntimeError('interrupted')
            return {'status': 'unavailable', 'stock': 0}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            with self.assertRaises(RuntimeError):
                run_once(path, http)
            self.assertEqual(json.loads(path.read_text())['products'][0]['status'], 'unavailable')

    def test_export_three_vps_products_and_no_credentials(self):
        old = update_product(TARGETS[1], {'status': 'available', 'stock': 3}, None, '2026-09-21T00:00:00+00:00')
        current = update_product(TARGETS[1], {'status': 'unknown', 'explanation': 'secret-cookie'}, old, '2026-09-21T00:03:00+00:00')
        current['SMTP_PASSWORD'] = 'secret-password'
        result = combined_snapshot({}, {'products': [current]})
        self.assertEqual([p['provider'] for p in result['products']], ['VMISS', 'ZgoCloud', 'RFCHOST'])
        self.assertEqual([p['status'] for p in result['products']], ['unknown'] * 3)
        self.assertTrue(all(p['query_location'] == 'hong-kong-vps' for p in result['products']))
        self.assertEqual(result['products'][2]['last_available_at'], old['last_checked'])
        self.assertIsNone(result['products'][2]['stock'])
        self.assertNotIn('secret-', json.dumps(result))
        self.assertIsNone(result['products'][1]['last_checked'])

    def test_vmiss_history_survives_schema_migration(self):
        state = {'target': {'product_name': 'JP.TKY.TRI.Basic', 'product_url': 'https://app.vmiss.com/store/jp-tokyo-tri'},
                 'last_confirmed': 'unavailable', 'last_checked': '2026-09-21T00:03:00+00:00'}
        previous = {**state['target'], 'schema_version': 1, 'status': 'unavailable',
                    'last_checked': '2026-09-21T00:00:00+00:00', 'unavailable_since': '2026-09-20T23:00:00+00:00'}
        first = combined_snapshot(state, previous=previous)
        second = combined_snapshot(state, previous=first)
        self.assertEqual(second['products'][0]['unavailable_since'], previous['unavailable_since'])


if __name__ == '__main__':
    unittest.main()
