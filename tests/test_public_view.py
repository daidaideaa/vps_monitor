import json
from pathlib import Path
import tempfile
import time
import unittest
from observatory.hub_store import Hub
from observatory import public_view
from observatory.targets import trusted_message
from observatory.stock import parse_message, accept, snapshot
from observatory.store import Store


class PublicView(unittest.TestCase):
    def test_retired_target_and_daily_client_do_not_inflate_vmiss_events(self):
        with tempfile.TemporaryDirectory() as d:
            hub=Hub(Path(d)/'hub.sqlite');now=time.time()-10
            sample={'source':'windows','boot_id':'test','seq':0,'captured_at':now,'interval':120,
                    'checks':{k:{'state':'fail','kind':'regular'} for k in ('home.hy2','vmiss.hy2','client.http')}}
            events=[{'id':key,'source':'windows','target':key,'started_at':now,'confirmed_at':now}
                    for key in ('home.hy2','vmiss.hy2','client.http')]
            hub.ingest('windows',{'samples':[sample],'incidents':events})
            latest=public_view.latest(hub)
            self.assertEqual(latest['statistics']['confirmed_incidents']['windows'],1)
            self.assertNotIn('home.hy2',hub.statistics(now-60,now+60)['regular_checks']['windows'])
            self.assertNotIn('home.hy2',latest['sources']['windows']['sample']['checks'])
            self.assertNotIn('home.hy2',latest['statistics']['regular_checks']['windows'])
            self.assertEqual(latest['sources']['windows']['coverage']['regular_samples'],1)
            self.assertEqual([i['target'] for i in public_view.incidents(hub)['incidents']],['vmiss.hy2'])
            history=public_view.history(hub)['samples']
            self.assertEqual(set(history[0]['checks']),{'vmiss.hy2'})
            self.assertEqual(history[0]['checks']['vmiss.hy2']['bad'],1)
            self.assertIsNone(history[0]['checks']['vmiss.hy2']['ms'])
            hub.close()

    def test_public_projection_never_copies_raw_fields_or_strings(self):
        with tempfile.TemporaryDirectory() as d:
            hub=Hub(Path(d)/'hub.sqlite');now=time.time();secret='SECRET_203.0.113.9_password'
            sample={'source':'windows','boot_id':secret,'seq':0,'captured_at':now,'interval':120,
                'client':{'profile':secret},'network':{'ip':secret},'host':{'cpu_percent':secret,'memory':{'available':secret}},
                'checks':{'vmiss.hy2':{'state':'fail','reason':secret,'ms':secret,'kind':'regular'},secret:{'state':'ok'}},
                'budget':{'monthly_estimate_bytes':0,'measured_upload_payload_bytes':secret},'upload':{'state':secret},'traffic':{'rx_bytes':secret}}
            incident={'id':secret,'source':'windows','target':'vmiss.hy2','started_at':now,'confirmed_at':now,
                      'report':{'facts':[secret]},'evidence':{'logs':secret}}
            hub.ingest('windows',{'samples':[sample],'incidents':[incident]})
            for fn in (public_view.latest,public_view.history,public_view.incidents):
                result=fn(hub);self.assertNotIn(secret,json.dumps(result));self.assertNotIn('boot_id',json.dumps(result))
            self.assertEqual(public_view.latest(hub)['sources']['windows']['sample']['checks']['vmiss.hy2']['state'],'fail')
            hub.close()

    def test_customer_groups_only_accept_admin_original_posts(self):
        self.assertFalse(trusted_message('vmisscom',4,{5}))
        self.assertFalse(trusted_message('vmisscom',5,{5},True))
        self.assertTrue(trusted_message('vmisscom',5,{5}))
        self.assertTrue(trusted_message('dmitnews',4,set()))

    def test_vendor_series_coupon_does_not_claim_tiny_stock(self):
        p=parse_message('vmiss_com',59,'JP.TKY.TRI series\nDiscount Code: TEST20',time.time())
        self.assertIsNotNone(p);self.assertEqual(p['status'],'unknown')
        self.assertIsNone(parse_message('vmiss_com',60,'JP.TKY.TRI except Basic\nDiscount Code: TEST20',time.time()))

    def test_greencloud_pid_baseline_uses_original_date(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'stock.sqlite');old=time.time()-60*86400
            p=parse_message('hostmonit',49225,'CN Premium Optimized Plan Mini (Tokyo)\n库存：0\nhttps://greencloudvps.com/billing/aff.php?pid=2213',old)
            accept(store,p,baseline=True);item=snapshot(store)['products'][1]
            self.assertEqual(item['last_confirmed'],'unavailable');self.assertEqual(item['status'],'unknown')
            self.assertEqual(item['event_at'],old);self.assertEqual(store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
            store.db.close()
