import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from export_status import snapshot


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

    def test_missing_state_exports_only_vmiss(self):
        result = snapshot({})
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['product_name'], 'JP.TKY.TRI.Basic')
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('products', result)
        self.assertNotIn('ZgoCloud', str(result))
        self.assertNotIn('RFCHOST', str(result))

    def test_persistent_history_uses_check_time_and_handles_gaps(self):
        def checked(at, status='unavailable', errors=0, previous=None):
            state = self.state(status, errors)
            state['last_checked'] = '2026-09-21T' + at + '+08:00'
            return snapshot(state, previous=previous)
        first = checked('08:00:00')
        self.assertIsNone(first['last_available_at'])
        repeated = checked('08:00:00', previous=first)
        second = checked('08:03:00', previous=repeated)
        self.assertEqual(second['unavailable_since'], first['last_checked'])
        available = checked('08:06:00', 'available', previous=second)
        self.assertIsNone(available['unavailable_since'])
        unknown = checked('08:09:00', 'available', 1, previous=available)
        self.assertEqual(unknown['last_available_at'], available['last_checked'])
        resumed = checked('08:12:00', previous=unknown)
        self.assertEqual(resumed['unavailable_since'], resumed['last_checked'])
        gap = checked('08:22:00', previous=resumed)
        self.assertEqual(gap['unavailable_since'], gap['last_checked'])
        self.assertEqual(gap['last_available_at'], available['last_checked'])


if __name__ == '__main__':
    unittest.main()
