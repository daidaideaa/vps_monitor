import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from observatory.stock import parse_message, accept, snapshot, parse_coupon
from observatory.store import Store
from observatory.incidents import Incidents, classify
from observatory.notify import drain
from observatory.vmiss_reservation import (Merchant, Page, Reservation, ReservationError,
    checkout_payload, coupon_candidates, order_signal, safe_url)


def reservation_checkout(product='JP.TKY.TRI.Basic', cycle='/yr', quantity='1', amount='120.00', credit=True):
    return Page(200, 'https://app.vmiss.com/cart.php?a=checkout', f'''
    <table><tr><td>{product}<span class="cart-item-price">$120.00 CAD{cycle}</span>
    <div hidden>$120.00 CAD/yr</div></td><td><input name="qty[0]" value="{quantity}"></td></tr></table>
    <div id="totalDueToday">${amount} CAD</div><form id="frmCheckout">
    <input name="token" value="csrf"><input name="account_id" type="radio" value="123" checked>
    <input name="paymentmethod" type="radio" value="stripe" checked>
    <input name="paymentmethod" type="radio" value="motionpayalipay">
    {'<input name="applycredit" type="radio" value="1">' if credit else ''}
    <input name="ccnumber" value="never-submit"><input name="accepttos" type="checkbox"></form>''')

class ObservatoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'state.sqlite');self.now=time.time()
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()
    def message(self,text=None,id=1,at=None):
        return parse_message('hostmonit',id,text or 'JP.TKY.TRI.Basic\n库存：2\nhttps://app.vmiss.com/cart.php?a=add&pid=101',at or self.now)
    def test_exact_product_and_region(self):
        self.assertIsNotNone(self.message())
        for text in ['JP.TKY.TRI.Basic\n库存：2\nhttps://app.vmiss.com/cart.php?pid=102',
                     '二手出一个 JP.TKY.TRI.Basic 库存：2 https://app.vmiss.com/',
                     'CN Premium Optimized Plan Mini (Tokyo)\n库存：2\nhttps://greencloudvps.com/billing/cart.php?pid=2305']:
            self.assertIsNone(self.message(text))
    def test_edited_sold_out_overrides_stale_quantity(self):
        p=self.message();accept(self.store,p,True)
        sold=self.message('JP.TKY.TRI.Basic\n库存：2\n已售罄\nhttps://app.vmiss.com/cart.php?pid=101',at=self.now+1)
        self.assertEqual(sold['status'],'unavailable');accept(self.store,sold,False,now=self.now+1)
        self.assertEqual(snapshot(self.store,self.now+2)['products'][0]['stock'],0)
    def test_baseline_dedup_catchup_old_and_new(self):
        accept(self.store,self.message(),True)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        accept(self.store,self.message(),False)
        sold=self.message('JP.TKY.TRI.Basic\n售罄\nhttps://app.vmiss.com/?pid=101',2,self.now+1)
        accept(self.store,sold,False,now=self.now+1)
        p=self.message(id=3,at=self.now+2);accept(self.store,p,False,now=self.now+2);accept(self.store,p,False,now=self.now+2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)
        accept(self.store,self.message(id=4,at=self.now-3600),False)
        self.assertEqual(self.store.get('stock:'+p['id'])['message_id'],3)
    def test_republishing_does_not_refresh_observation(self):
        accept(self.store,self.message(),True)
        data=snapshot(self.store,self.now+1801)
        self.assertEqual(data['products'][0]['status'],'unknown')
        self.assertEqual(data['products'][0]['event_at'],self.now)
    def test_coupon_requires_complete_rules_and_expiry(self):
        c=parse_coupon('价格 USD 20/年\n优惠码 SAVE20 年付 8折 循环',self.now)
        self.assertEqual(c['discounted']['amount'],'16.00')
        self.assertIsNone(parse_coupon('USD 20/年\n优惠码 SAVE20 8折',self.now)['discounted'])
        self.assertIsNone(parse_coupon('$20/年\n优惠码 SAVE20 年付 8折',self.now)['discounted'])
        self.assertEqual(parse_coupon('优惠码 OLD 年付 8折 有效期至 2020-01-01',self.now)['validity'],'expired')
    def test_new_coupon_once_while_available(self):
        p=self.message();accept(self.store,p,True)
        p=self.message('JP.TKY.TRI.Basic\n库存：2\n优惠码 YEAR 年付 8折\nhttps://app.vmiss.com/?pid=101',2,self.now+1)
        accept(self.store,p,now=self.now+1);accept(self.store,p,now=self.now+1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)
    def test_incomplete_discount_and_negative_renewal(self):
        c=parse_coupon('USD 20/年\n优惠码 FIRST 年付 最高8折 不循环',self.now)
        self.assertIsNone(c['discounted']);self.assertFalse(c['recurring'])
    def test_evidence_bounds_and_startup_coverage_are_explicit(self):
        machine=Incidents(self.store,'windows');sample=self.sample(0,'fail')
        self.store.add_sample(sample);incident=machine.observe(sample)[0]
        incident['evidence']['details']={'oversize':'x'*90000};self.store.incident(incident)
        self.assertLess(len(json.dumps(self.store.incidents()[0]).encode()),64000)
        machine.observe(self.sample(61))
        self.assertTrue(any('10 分钟' in v for v in self.store.incidents()[0]['report']['missing']))
    def test_service_exit_collects_evidence(self):
        s=self.sample(0);s['checks']['test.service']={'state':'fail','new':True}
        self.assertEqual(Incidents(self.store,'vmiss').observe(s)[0]['target'],'test.service')
    def test_coupon_only_message_does_not_refresh_stock(self):
        accept(self.store,self.message(),True)
        p=self.message('JP.TKY.TRI.Basic\n优惠码 NEWYEAR 年付 8折\nhttps://app.vmiss.com/?pid=101',2,self.now+20)
        accept(self.store,p,now=self.now+20)
        data=snapshot(self.store,self.now+21)['products'][0]
        self.assertEqual(data['event_at'],self.now)
        self.assertEqual(data['coupon']['code'],'NEWYEAR')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)
    def test_resume_sends_latest_without_waiting_for_old_buffer(self):
        for n in range(30):self.store.add_sample(self.sample(n))
        batch=self.store.pending(5)
        self.assertEqual(batch[0][1]['seq'],29)
        self.assertEqual([s['seq'] for _,s in batch[1:]],[0,1,2,3])
    def test_notification_failure_retries_without_losing_other_channel(self):
        self.store.enqueue('one','test')
        def fail(*_):raise ConnectionError()
        calls=[]
        drain(self.store,{},senders={'telegram':fail,'email':lambda *_:calls.append('email')})
        self.assertEqual(calls,['email'])
        self.assertEqual(self.store.db.execute("SELECT attempts,sent FROM outbox WHERE channel='telegram'").fetchone(),(1,None))
        drain(self.store,{},senders={'telegram':fail,'email':lambda *_:calls.append('email')})
        self.assertEqual(calls,['email'])
    def sample(self,seq,state='ok',new=True):
        return {'source':'windows','boot_id':'boot-test','seq':seq,'captured_at':self.now+seq*5,'monotonic_ms':seq*5000,
                'checks':{'vmiss.hy2':{'state':state,'new':new},'vmiss.tcp':{'state':'ok','new':new}}}
    def test_freezes_previous_ten_minutes_and_post_tail(self):
        machine=Incidents(self.store,'windows')
        for n in range(-120,0):self.store.add_sample(self.sample(n))
        s=self.sample(0,'fail');self.store.add_sample(s);events=machine.observe(s)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['end_at'],self.now+300)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM incident_evidence').fetchone()[0],121)
        for n,state in [(1,'fail'),(2,'fail'),(3,'ok'),(4,'ok')]:
            s=self.sample(n,state);self.store.add_sample(s);machine.observe(s)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],4)
        machine.observe(self.sample(61))
        self.assertEqual(self.store.incidents()[0]['status'],'complete')
        self.assertEqual(len(machine.observe(self.sample(62,'fail'))),0)
    def test_cached_results_and_offline_do_not_add_failures(self):
        machine=Incidents(self.store,'windows')
        machine.observe(self.sample(0,'fail'))
        for n in range(1,10):machine.observe(self.sample(n,'fail',False))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        s=self.sample(10,'error');machine.observe(s)
        self.assertEqual(machine.state['vmiss.hy2']['fails'],1)
    def test_icmp_alone_does_not_prove_outage_or_carrier_fault(self):
        s=self.sample(0);s['checks']['vmiss.icmp']={'state':'fail'}
        report=classify([s]);self.assertTrue(any('仅 ICMP' in x for x in report['inferences']))
        self.assertTrue(any('不能确认运营商' in x for x in report['missing']))
    def test_offline_buffer_ack_retains_original_time(self):
        self.store.add_sample(self.sample(1))
        pending=self.store.pending()
        self.assertEqual(pending[0][1]['captured_at'],self.now+5)
        self.store.acknowledge([pending[0][0]])
        self.assertEqual(self.store.pending(),[])
        self.assertEqual(len(self.store.window(self.now,self.now+10)),1)

    def test_reservation_checkout_policy(self):
        data, amount = checkout_payload(reservation_checkout())
        self.assertEqual((data['applycredit'], data['paymentmethod'], str(amount)), ('1', 'motionpayalipay', '120.00'))
        self.assertNotIn('ccnumber', data)
        for change in [dict(product='JP.TKY.TRI.Pro'), dict(cycle='/mo'), dict(quantity='2'), dict(amount='200.00'), dict(credit=False)]:
            with self.subTest(change=change), self.assertRaises(ReservationError):
                checkout_payload(reservation_checkout(**change))
        for url in ['https://evil.example/checkout', 'http://app.vmiss.com/', 'https://app.vmiss.com@evil.example/']:
            with self.subTest(url=url), self.assertRaises(ReservationError):
                safe_url(url)

    def test_reservation_lifecycle(self):
        from unittest.mock import Mock
        invoice = {'status': 'held', 'invoice_id': '456', 'invoice_url': 'https://app.vmiss.com/viewinvoice.php?id=456'}
        for scenario in ('success', 'timeout', 'crash', 'restock'):
            with self.subTest(scenario=scenario):
                root = Path(self.tmp.name) / scenario
                merchant = Mock()
                merchant.prepare.return_value = ({'status': 'prepared', 'total': '120.00'}, {})
                merchant.inspect_invoice.return_value = None
                merchant.invoice.return_value = invoice

                def submit(_):
                    self.assertEqual(json.loads((root/'state.json').read_text())['status'], 'submitting')
                    if scenario == 'timeout':
                        raise ReservationError('network_failure')
                    return Page(302, 'https://app.vmiss.com/cart.php?a=checkout', '')

                merchant.submit.side_effect = submit
                worker = Reservation(root, self.store, merchant)
                if scenario == 'crash':
                    worker.save(status='submitting')
                elif scenario == 'restock':
                    merchant.prepare.return_value = ({'status': 'unavailable'}, None)
                    self.assertEqual(worker.attempt(signal='first'), 'unavailable')
                    merchant.prepare.return_value = ({'status': 'prepared', 'total': '120.00'}, {})
                self.assertEqual(worker.attempt(signal='next'), 'manual_required' if scenario == 'timeout' else 'held')
                queued = self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
                Reservation(root, self.store, merchant).attempt(signal='duplicate')
                self.assertEqual(merchant.submit.call_count, 0 if scenario == 'crash' else 1)
                self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], queued)

    def test_reservation_coupon_fallback(self):
        self.assertEqual(coupon_candidates({'coupon': {'code': '10%OFF', 'cycle': '年'}}, self.now), ['10%OFF'])
        for coupon in [dict(code='OLD', expires_at=self.now-1), dict(code='MONTH', cycle='月'), dict(code='x&submit=true')]:
            with self.subTest(coupon=coupon):
                self.assertEqual(coupon_candidates({'coupon': coupon}, self.now), [])
        merchant = object.__new__(Merchant)
        merchant.credentials = {'fallback_coupons': ['10%OFF']}
        merchant.login = lambda: None
        merchant.invoice = lambda: None
        merchant.invoice_links = lambda: []
        merchant.existing_service = lambda: False
        merchant.configure = lambda page: {}
        tried = []

        def request(path, data=None):
            if data and 'promocode' in data:
                tried.append(data['promocode'])
                if data['promocode'] == '10%OFF':
                    page = reservation_checkout(amount='108.00')
                    return Page(page.status, page.url, page.html + '<p>10%OFF</p>')
            return reservation_checkout()

        merchant.request = request
        prepared, data = merchant.prepare({'coupon': {'code': 'EXPIRED_AT_VENDOR'}})
        self.assertEqual(tried, ['EXPIRED_AT_VENDOR', '10%OFF'])
        self.assertEqual((prepared['coupon'], prepared['total'], data['applycredit']), ('10%OFF', '108.00', '1'))

    def test_reservation_signals(self):
        p = self.message()
        order_signal(self.store, p, baseline=True, now=self.now)
        for change in [dict(event_at=self.now-1801), dict(event_at=self.now+1), dict(id='other')]:
            order_signal(self.store, {**p, **change}, now=self.now)
        self.assertIsNone(self.store.get('vmiss_order_signal'))
        accept(self.store, p, baseline=True, now=self.now)
        coupon = self.message('JP.TKY.TRI.Basic\n优惠码 NEW 年付 8折\nhttps://app.vmiss.com/?pid=101', 2, self.now+1)
        accept(self.store, coupon, now=self.now+1)
        self.assertIsNone(self.store.get('vmiss_order_signal'))
        fresh = self.message(id=3, at=self.now+2)
        accept(self.store, fresh, now=self.now+2)
        signal = self.store.get('vmiss_order_signal')
        self.assertIsNotNone(signal)
        accept(self.store, fresh, now=self.now+2)
        self.assertEqual(self.store.get('vmiss_order_signal'), signal)

    def test_reservation_invoice_reconciliation(self):
        merchant = object.__new__(Merchant)
        listing = Page(200, 'https://app.vmiss.com/clientarea.php?action=invoices', '''
        <a href="logout.php">Logout</a><table><tbody>
        <tr data-url="viewinvoice.php?id=2"><td>未付款</td></tr>
        <tr data-url="viewinvoice.php?id=3"><td>未付款</td></tr></tbody></table>''')
        topup = Page(200, 'https://app.vmiss.com/viewinvoice.php?id=2', '<table><tr><td>账户充值</td></tr></table>')
        target = Page(200, 'https://app.vmiss.com/viewinvoice.php?id=3', '''
        <span class="invoice-status">未付款</span><table><tr><td>JP.TKY.TRI.Basic</td></tr>
        <tr><td>余额</td><td>$15.00 CAD</td></tr><tr><td>结余</td><td>$93.00 CAD</td></tr></table>''')
        merchant.request = lambda path: listing if 'invoices' in path else topup if 'id=2' in path else target
        result = merchant.invoice()
        self.assertEqual((result['invoice_id'], result['credit_applied'], result['balance_due']), ('3', '15.00', '93.00'))
        self.assertIsNone(merchant.invoice(exclude_ids=['3']))
        self.assertEqual(Merchant.inspect_invoice(Page(200, target.url, target.html.replace('未付款', '已付款')))['status'], 'paid_with_credit')

    def channel_message(self, source='vmisstz', ident=1, state='✅ 补货', code='10%OFF', offset=0, plan='JP.TKY.TRI.Basic'):
        return parse_message(source, ident, f'VMISS - {plan}\n价格：$5.00 CAD 每月\n优惠码：{code}\n{state}',
            self.now+offset, ['https://app.vmiss.com/cart.php?a=add&pid=101'])

    def test_vmisstz_exact_plan_and_vendor_link(self):
        from observatory.targets import CHANNELS, VENDOR_SOURCES
        self.assertIn('vmisstz', CHANNELS)
        self.assertNotIn('vmisstz', VENDOR_SOURCES)
        p = self.channel_message()
        self.assertEqual((p['status'], p['coupon']['code'], p['prices'][0]['currency'], p['prices'][0]['cycle']), ('available', '10%OFF', 'CAD', '月'))
        for plan in ('JP.TKY.IIJ.Basic', 'JP.TKY.BGP.Core', 'JP.OSA.IIJ.Basic', 'US.LA.TRI.Basic'):
            self.assertIsNone(self.channel_message(plan=plan))
        self.assertIsNone(parse_message('vmisstz', 3, 'JP.TKY.TRI.Basic\n✅ 补货', self.now))

    def test_vmisstz_edits_and_cross_source_dedup(self):
        accept(self.store, self.channel_message(), baseline=True, now=self.now, channels=('email',))
        accept(self.store, self.channel_message('hostmonit', 2, offset=1), now=self.now+1, channels=('email',))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
        sold = self.channel_message(state='❌ 售罄', offset=2)
        accept(self.store, sold, now=self.now+2, channels=('email',))
        self.assertEqual(self.store.get('stock:'+sold['id'])['status'], 'unavailable')
        for p in [self.channel_message(ident=3, offset=3), self.channel_message(ident=3, offset=3),
                  self.channel_message('hostmonit', 4, offset=4), self.channel_message(ident=5, code='SAVE20', offset=5),
                  self.channel_message(ident=5, code='SAVE20', offset=5)]:
            accept(self.store, p, now=self.now+6, channels=('email',))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 2)


if __name__=='__main__':unittest.main()
