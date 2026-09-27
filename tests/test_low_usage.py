import copy
import gzip
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from observatory.hub_service import Login, handler_for
from observatory.hub_store import Hub
from observatory.low_state import LowIncidents
from observatory.store import Store
from observatory.stock import accept, parse_message


class LowUsage(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'probe.sqlite',capture_evidence=False)
        self.hub=Hub(self.root/'hub.sqlite');self.now=time.time()
    def tearDown(self):
        self.store.db.close();self.hub.close();self.tmp.cleanup()
    def sample(self,seq=1,at=None,state='ok',kind='regular'):
        return {'source':'windows','boot_id':'test','seq':seq,'captured_at':at or self.now,'kind':kind,'interval':120,
                'checks':{'vmiss.hy2':{'state':state,'ms':50,'kind':kind,'new':True}}}
    def test_failure_fast_confirmation_recovery_and_no_notification(self):
        m=LowIncidents(self.store,'windows')
        self.assertEqual(m.observe('vmiss.hy2',{'state':'fail'},self.now),self.now+5)
        self.assertEqual(m.observe('vmiss.hy2',{'state':'fail'},self.now+5,'retry'),self.now+10)
        self.assertIsNone(m.observe('vmiss.hy2',{'state':'fail'},self.now+10,'retry'))
        self.assertEqual(self.store.incidents()[0]['confirmed_at'],self.now+10)
        for n in range(4):m.observe('vmiss.hy2',{'state':'fail'},self.now+120*(n+1))
        self.assertEqual(len(self.store.incidents()),1)
        self.assertEqual(m.observe('vmiss.hy2',{'state':'ok'},self.now+500),self.now+505)
        m.observe('vmiss.hy2',{'state':'ok'},self.now+505,'retry')
        self.assertEqual(self.store.incidents()[0]['recovered_at'],self.now+505)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
    def test_gap_breaks_failure_streak_and_retry_does_not_inflate_rates(self):
        m=LowIncidents(self.store,'windows');m.observe('vmiss.hy2',{'state':'fail'},self.now)
        m.gap();m.observe('vmiss.hy2',{'state':'fail'},self.now+200)
        self.assertIsNone(self.store.incidents()[0]['confirmed_at'])
        samples=[self.sample(),self.sample(2,self.now+1,'fail','retry'),self.sample(3,self.now+2,'fail')]
        self.hub.ingest('windows',{'samples':samples},self.now+10)
        stat=self.hub.statistics(self.now-60,self.now+60)['regular_checks']['windows']['vmiss.hy2']
        self.assertEqual((stat['good'],stat['bad']),(1,1))
    def test_replay_duplicates_out_of_order_and_foreign_source(self):
        data={'samples':[self.sample(2,self.now),self.sample(1,self.now-120)]}
        self.hub.ingest('windows',data);self.hub.ingest('windows',data)
        self.assertEqual(self.hub.db.execute('SELECT count(*) FROM samples').fetchone()[0],2)
        self.assertEqual(self.hub.latest()['sources']['windows']['sample']['seq'],2)
        foreign=copy.deepcopy(data);foreign['samples'][0]['source']='vmiss'
        with self.assertRaises(ValueError):self.hub.ingest('windows',foreign)
        self.assertEqual(self.hub.db.execute('SELECT count(*) FROM samples').fetchone()[0],2)
    def test_evidence_uses_raw_reference_not_duplicate_copies(self):
        for i in range(6):self.store.add_sample(self.sample(i,self.now-600+i*120))
        m=LowIncidents(self.store,'windows');m.observe('vmiss.hy2',{'state':'fail'},self.now)
        m.finish_evidence(self.now+301)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM incident_evidence').fetchone()[0],0)
        evidence=self.store.incidents()[0]
        self.assertIn('transitions',evidence['evidence'])
        self.assertLess(len(json.dumps(evidence).encode()),16000)
    def test_retention_and_stale_are_not_packet_loss(self):
        self.hub.ingest('windows',{'samples':[self.sample(at=self.now-400)]})
        self.assertEqual(self.hub.latest()['sources']['windows']['state'],'stale')
        self.hub.trim(self.now+8*86400)
        self.assertEqual(self.hub.db.execute('SELECT count(*) FROM samples').fetchone()[0],0)
        self.assertEqual(self.hub.db.execute('SELECT SUM(bad) FROM rollups').fetchone()[0],0)
    def test_only_new_checks_aggregate(self):
        s=self.sample();s['checks']['vmiss.hy2']['new']=False
        self.hub.ingest('windows',{'samples':[s]})
        self.assertEqual(self.hub.db.execute('SELECT COUNT(*) FROM rollups').fetchone()[0],0)
    def test_stock_email_only_and_cancel_legacy_backlog(self):
        p=parse_message('hostmonit',1,'JP.TKY.TRI.Basic\n库存：1\nhttps://app.vmiss.com/cart.php?pid=101',self.now)
        accept(self.store,p,channels=('email',));accept(self.store,p,channels=('email',))
        self.assertEqual(self.store.db.execute('SELECT channel FROM outbox').fetchall(),[('email',)])
        self.store.enqueue('link-down:old','old');self.store.cancel_legacy_notifications()
        rows=self.store.db.execute('SELECT id FROM outbox WHERE sent IS NULL AND cancelled IS NULL').fetchall()
        self.assertEqual(len(rows),1);self.assertTrue(rows[0][0].startswith('stock:'))
    def test_login_single_use_rate_limit_and_expiry(self):
        sent=[];login=Login({'notify':{}},lambda c,b:sent.append(b))
        try:
            c=login.challenge();login.pool.shutdown(wait=True)
            code=sent[0].split('] ')[1].splitlines()[0]
            token=login.verify(c['challenge'],code)['token'];self.assertTrue(login.valid(token))
            with self.assertRaises(ValueError):login.verify(c['challenge'],code)
            with self.assertRaises(ValueError):login.challenge()
            login.logout(token);self.assertFalse(login.valid(token))
        finally:login.pool.shutdown(wait=True)
    def test_api_enforces_private_reads_and_per_source_ingest(self):
        sent=[];config={'state_dir':str(self.root),'started_at':self.now,'origins':['https://owner.example'],
                        'probe_tokens':{'windows':'write-only'},'notify':{}}
        login=Login(config,lambda c,b:sent.append(b));server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(config,login))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)+'/monitor'
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def req(path,body=None,headers=None):
            data=json.dumps(body).encode() if body is not None else None
            with opener.open(urllib.request.Request(base+path,data,headers=headers or {}),timeout=3) as r:return json.load(r)
        try:
            for path in ['/api/latest','/api/history','/api/incidents','/api/evidence?id=x','/api/reports']:
                with self.assertRaises(urllib.error.HTTPError) as e:req(path,headers={'Authorization':'Bearer write-only'})
                self.assertEqual(e.exception.code,401)
            result=req('/ingest',{'samples':[self.sample()]},{'Authorization':'Bearer write-only','X-Probe-Source':'windows'})
            self.assertEqual(result['accepted'],1)
            public=req('/status.json');self.assertNotIn('sources',public);self.assertNotIn('samples',public)
            c=req('/auth/request',{}, {'Origin':'https://owner.example'});login.pool.shutdown(wait=True)
            code=sent[0].split('] ')[1].splitlines()[0]
            t=req('/auth/verify',{'challenge':c['challenge'],'code':code},{'Origin':'https://owner.example'})['token']
            self.assertIn('windows',req('/api/latest',headers={'Authorization':'Bearer '+t})['sources'])
            with self.assertRaises(urllib.error.HTTPError):req('/api/latest',headers={'Authorization':'Bearer '+t,'Origin':'https://evil.example'})
        finally:server.shutdown();server.server_close();login.pool.shutdown(wait=True)


if __name__=='__main__':unittest.main()
