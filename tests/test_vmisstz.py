"""Replay the new channel without sending mail or touching the live session."""
import tempfile
import time
import unittest
from pathlib import Path

from observatory.stock import accept, parse_message
from observatory.store import Store
from observatory.targets import CHANNELS, VENDOR_SOURCES


class VmissChannelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'stock.sqlite')
        self.at = time.time()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def message(self, source='vmisstz', ident=1, state='✅ 补货', code='10%OFF', offset=0, plan='JP.TKY.TRI.Basic'):
        text = f'VMISS - {plan}\n价格：$5.00 CAD 每月\n优惠码：{code}\n{state}'
        return parse_message(source, ident, text, self.at + offset,
                             ['https://app.vmiss.com/cart.php?a=add&pid=101'])

    def test_exact_plan_link_and_coupon(self):
        self.assertIn('vmisstz', CHANNELS)
        self.assertNotIn('vmisstz', VENDOR_SOURCES)
        p = self.message()
        self.assertEqual(p['status'], 'available')
        self.assertEqual(p['coupon']['code'], '10%OFF')
        self.assertEqual(p['prices'][0]['currency'], 'CAD')
        self.assertEqual(p['prices'][0]['cycle'], '月')
        for plan in ('JP.TKY.IIJ.Basic', 'JP.TKY.BGP.Core', 'JP.OSA.IIJ.Basic', 'US.LA.TRI.Basic'):
            self.assertIsNone(self.message(plan=plan))
        self.assertIsNone(parse_message('vmisstz', 3, 'JP.TKY.TRI.Basic\n✅ 补货', self.at))

    def test_baseline_cross_source_duplicate_edit_and_new_coupon(self):
        accept(self.store, self.message(), baseline=True, now=self.at, channels=('email',))
        accept(self.store, self.message('hostmonit', 2, offset=1), now=self.at+1, channels=('email',))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
        sold = self.message(state='❌ 售罄', offset=2)
        accept(self.store, sold, now=self.at+2, channels=('email',))
        self.assertEqual(self.store.get('stock:'+sold['id'])['status'], 'unavailable')
        restock = self.message(ident=3, offset=3)
        accept(self.store, restock, now=self.at+3, channels=('email',))
        accept(self.store, restock, now=self.at+4, channels=('email',))
        accept(self.store, self.message('hostmonit', 4, offset=4), now=self.at+4, channels=('email',))
        coupon = self.message(ident=5, code='SAVE20', offset=5)
        accept(self.store, coupon, now=self.at+5, channels=('email',))
        accept(self.store, coupon, now=self.at+6, channels=('email',))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
