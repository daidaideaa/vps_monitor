import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from export_status import combined_snapshot, snapshot


class SnapshotTest(unittest.TestCase):
    def state(self, confirmed='unavailable', errors=0):
        return {'target': {'product_name': 'JP.TKY.TRI.Basic',
                           'product_url': 'https://app.vmiss.com/store/jp-tokyo-tri'},
                'last_confirmed': confirmed, 'last_checked': '2026-09-21T08:00:00+08:00',
                'unknown_count': errors, 'last_unknown_reason': 'HTTP 403 / Cloudflare challenge'}

    def test_confirmed_states(self):
        for value in ('available', 'unavailable', 'unknown'):
            self.assertEqual(snapshot(self.state(value))['status'], value)

    def test_unknown_never_reuses_previous_available_as_current(self):
        result = snapshot(self.state('available', 1))
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['last_confirmed'], 'available')
        self.assertIn('403', result['explanation'])

    def test_only_public_fields_are_exported(self):
        state = self.state()
        state.update(SMTP_PASSWORD='private-test-value', mail_to='private@example.invalid')
        state['last_unknown_reason'] = 'private-test-value'
        public = snapshot(state)
        self.assertNotIn('private-test-value', str(public))
        self.assertNotIn('mail_to', public)
        self.assertNotIn('SMTP_PASSWORD', public)

    def test_target_and_interval_follow_config(self):
        state = self.state()
        state['target']['product_name'] = 'JP.TKY.BGP.Pro'
        result = snapshot(state, 60)
        self.assertEqual(result['product_name'], 'JP.TKY.BGP.Pro')
        self.assertEqual(result['check_interval_seconds'], 60)

    def test_combined_snapshot_contains_three_targets(self):
        http_state = {'products': [
            {'id': 'zgocloud-tokyo-intel-starter', 'provider': 'ZgoCloud',
             'product_name': 'Tokyo Intel VPS · Starter',
             'product_url': 'https://clients.zgovps.com/index.php?/cart/tokyo-intel-vps/',
             'status': 'unavailable', 'last_confirmed': 'unavailable',
             'last_checked': '2026-09-21T08:00:00+08:00', 'unknown_count': 0, 'stock': 0},
            {'id': 'rfchost-jp2-co-micro-lite', 'provider': 'RFCHOST',
             'product_name': 'JP2-CO-Micro-Lite',
             'product_url': 'https://my.rfchost.com/index.php?rp=/store/jp-2-china-optimization-network-lite',
             'status': 'unavailable', 'last_confirmed': 'unavailable',
             'last_checked': '2026-09-21T08:00:00+08:00', 'unknown_count': 0, 'stock': 0},
        ]}
        result = combined_snapshot(self.state(), http_state, 180)
        self.assertEqual(result['schema_version'], 2)
        self.assertEqual([p['provider'] for p in result['products']], ['VMISS', 'ZgoCloud', 'RFCHOST'])
        self.assertTrue(all(p['check_interval_seconds'] == 180 for p in result['products']))


if __name__ == '__main__':
    unittest.main()
