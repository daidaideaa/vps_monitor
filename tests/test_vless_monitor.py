import tempfile
import time
import unittest
from pathlib import Path
from observatory.budget import project, RESERVE, MONTH
from observatory.hub_store import Hub
from observatory import public_view
from observatory.incidents import classify


class VlessMonitor(unittest.TestCase):
    def test_partial_day_projection_uses_actual_elapsed(self):
        b={'started_at':100,'estimated_probe_bytes':500000,'measured_upload_payload_bytes':100000}
        six=project(b,100+6*3600);day=project(b,100+86400)
        self.assertEqual(six['monthly_estimate_bytes']-RESERVE,4*(day['monthly_estimate_bytes']-RESERVE))
        self.assertTrue(six['provisional']);self.assertFalse(day['provisional'])
        self.assertEqual(project(b,100)['monthly_estimate_bytes'],round(600000/60*MONTH+RESERVE))

    def test_switch_does_not_relabel_or_count_old_hy2_failures(self):
        with tempfile.TemporaryDirectory() as d:
            hub=Hub(Path(d)/'hub.sqlite');now=time.time()-10
            def sample(seq,metric,at,state):
                return {'source':'windows','boot_id':'b','seq':seq,'captured_at':at,'interval':300,
                        'checks':{metric:{'state':state,'ms':200,'kind':'regular'}}}
            hub.ingest('windows',{'samples':[sample(1,'vmiss.hy2',now-600,'fail'),sample(2,'vmiss.vless',now,'ok')],
                'incidents':[{'source':'windows','id':'old','target':'vmiss.hy2','started_at':now-600,'confirmed_at':now-590}]})
            data=public_view.latest(hub)
            self.assertEqual(data['primary_protocol'],'vless')
            self.assertNotIn('vmiss.hy2',data['statistics']['regular_checks']['windows'])
            self.assertEqual(data['sources']['windows']['coverage']['regular_samples'],1)
            self.assertEqual(public_view.incidents(hub)['incidents'][0]['target'],'vmiss.hy2')
            metrics={k for s in public_view.history(hub)['samples'] for k in s['checks']}
            self.assertEqual(metrics,{'vmiss.hy2','vmiss.vless'})
            hub.close()

    def test_vless_failure_is_not_attributed_to_udp(self):
        report=classify([{'captured_at':time.time(),'checks':{'vmiss.vless':{'state':'fail'},'vmiss.tcp':{'state':'ok'}}}])
        self.assertTrue(any('TLS' in s for s in report['inferences']))
        self.assertFalse(any('可能涉及 UDP' in s for s in report['inferences']))
