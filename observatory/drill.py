"""Isolated Linux integration drill. Uses loopback ports and disposable HY2 processes.

No firewall changes, production service restarts, or external notifications.
The evidence window uses an explicitly accelerated clock; network requests are real.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from . import probes
from .collector import upload
from .incidents import Incidents
from .store import Store
from .udp_metadata import capture

class Handler(BaseHTTPRequestHandler):
    reject=True
    received=[]
    def log_message(self,*args):pass
    def do_GET(self):
        self.send_response(204);self.end_headers()
    def do_POST(self):
        value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.reject:self.send_response(503);self.end_headers();return
        self.received.append(value)
        self.send_response(200);self.end_headers()
        self.wfile.write(json.dumps({'server_at':time.time()}).encode())

def run(binary):
    result={'started_at':time.time(),'mode':'isolated_loopback_real_io_accelerated_evidence_clock','checks':{}}
    children=[];pool=ThreadPoolExecutor(2)
    origin=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=origin.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='vps-observatory-drill-') as directory:
        root=Path(directory);store=Store(root/'drill.sqlite');machine=Incidents(store,'drill')
        reserve=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);reserve.bind(('127.0.0.1',0));udp=reserve.getsockname()[1];reserve.close()
        reserve=socket.socket();reserve.bind(('127.0.0.1',0));http=reserve.getsockname()[1];reserve.close()
        def start(mode,path):
            p=subprocess.Popen([binary,mode,'-c',str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);children.append(p);return p
        try:
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(root/'key.pem'),'-out',str(root/'cert.pem'),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            password=secrets.token_urlsafe(24)
            (root/'server.json').write_text(json.dumps({'listen':'127.0.0.1:'+str(udp),'tls':{'cert':str(root/'cert.pem'),'key':str(root/'key.pem')},'auth':{'type':'password','password':password}}))
            (root/'client.json').write_text(json.dumps({'server':'127.0.0.1:'+str(udp),'auth':password,'tls':{'sni':'localhost','ca':str(root/'cert.pem')},'http':{'listen':'127.0.0.1:'+str(http)}}))
            server=start('server',root/'server.json');client=start('client',root/'client.json');time.sleep(2)
            url='http://127.0.0.1:'+str(origin.server_port)+'/origin'
            request=lambda:probes.https(url,'http://127.0.0.1:'+str(http))
            baseline=request();assert baseline['state']=='ok',('baseline',baseline)
            result['checks']['hy2_baseline']=baseline
            base=time.time();seq=0
            def record(state,at):
                nonlocal seq
                s={'source':'drill','boot_id':'drill-boot','seq':seq,'captured_at':at,'monotonic_ms':seq*1000,'checks':{'vmiss.hy2':{'state':state,'new':True,'sampled_at':at},'vmiss.tcp':{'state':'ok','new':True}}};seq+=1
                store.add_sample(s);machine.observe(s);return s
            for n in range(-120,0):record('ok',base+n*5)
            server.terminate();server.wait(timeout=3)
            blackhole=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);blackhole.bind(('127.0.0.1',udp));blackhole.settimeout(.5)
            count=[0];reading=threading.Event();reading.set()
            def discard():
                while reading.is_set():
                    try:blackhole.recvfrom(65535);count[0]+=1
                    except socket.timeout:pass
                    except OSError:return
            reader=threading.Thread(target=discard,daemon=True);reader.start()
            failures=[request() for _ in range(3)]
            for n,value in enumerate(failures):record(value['state'],base+n*5)
            assert all(v['state']=='fail' for v in failures),failures
            assert count[0]>0,'No UDP reached isolated blackhole'
            result['checks']['udp_blackhole']={'failed_requests':len(failures),'discarded_datagrams':count[0]}
            reading.clear();reader.join(timeout=1);blackhole.close()
            server=start('server',root/'server.json')
            # A fresh test process isolates recovery from the old client's reconnect delay.
            client.terminate();client.wait(timeout=3);client=start('client',root/'client.json');time.sleep(2)
            recovered=[request(),request()];assert all(v['state']=='ok' for v in recovered),recovered
            record('ok',base+20);record('ok',base+25);record('ok',base+301)
            incident=store.incidents()[0]
            assert incident['status']=='complete' and incident['recovered_at']==base+25
            assert store.db.execute('SELECT count(*) FROM outbox').fetchone()[0]==4
            result['checks']['evidence_and_notifications']={'pre_seconds':incident['evidence']['available_pre_seconds'],'tail_seconds':incident['end_at']-incident['started_at'],'outbox_events':4,'delivery':'local outbox only; live Telegram requires authorization','report':incident['report']}
            dns=probes.measured(lambda:socket.getaddrinfo('does-not-exist.vps-drill.invalid',443));assert dns['state']=='fail';result['checks']['dns_failure']=dns
            process=subprocess.Popen(['python3','-c','import time;time.sleep(30)']);process.terminate();process.wait(timeout=3)
            s={'source':'drill','boot_id':'drill-boot','seq':seq,'captured_at':base+400,'monotonic_ms':seq*1000,'checks':{'dummy.service':{'state':'fail','new':True}}}
            assert machine.observe(s)[0]['target']=='dummy.service';result['checks']['service_exit']={'exit_code':process.returncode,'incident_triggered':True}
            cfg={'source':'drill','upload_url':'http://127.0.0.1:'+str(origin.server_port)+'/ingest','upload_token':'drill-only'}
            pending=store.pending(5)
            try:upload(cfg,[s for _,s in pending],[]);raise AssertionError('503 accepted')
            except __import__('urllib.error',fromlist=['HTTPError']).HTTPError as exc:assert exc.code==503
            assert len(store.pending(5))==5
            Handler.reject=False;upload(cfg,[s for _,s in pending],[]);store.acknowledge([i for i,_ in pending])
            assert Handler.received[-1]['samples'][1]['captured_at']==pending[1][1]['captured_at']
            result['checks']['upload_failure_replay']={'retained_on_503':True,'original_capture_time_preserved':True}
            before=machine.state['vmiss.hy2']['fails']
            machine.observe({'captured_at':base+420,'checks':{},'gap':{'seconds':90}})
            assert machine.state['vmiss.hy2']['fails']==before;result['checks']['probe_offline']={'missing_samples_not_counted_as_loss':True}
            meta=pool.submit(capture,None,2,40,udp);time.sleep(.2)
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
                sender.sendto(b'PAYLOAD_MUST_NOT_PERSIST',('127.0.0.1',udp))
                sender.sendto(b'OTHER_PORT',('127.0.0.1',udp+1 if udp<65535 else udp-1))
            data=meta.result(timeout=5)
            assert data['headers'] and all(udp in (r['sport'],r['dport']) for r in data['headers'])
            assert 'PAYLOAD_MUST_NOT_PERSIST' not in json.dumps(data) and data['payload_persisted'] is False
            result['checks']['udp_metadata']={'headers_captured':len(data['headers']),'target_filter_verified':True,'payload_persisted':False}
            result['status']='passed';result['finished_at']=time.time();return result
        finally:
            for child in children:
                if child.poll() is None:child.terminate()
            for child in children:
                try:child.wait(timeout=3)
                except subprocess.TimeoutExpired:child.kill();child.wait()
            store.db.close();origin.shutdown();origin.server_close();pool.shutdown()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--binary',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    result=run(args.binary);Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({'status':result['status'],'checks':list(result['checks'])}))
