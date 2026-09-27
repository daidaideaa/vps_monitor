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

if __name__=='__main__':unittest.main()
