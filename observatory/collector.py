"""Five-second local journal with independent, asynchronous upload and evidence capture."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import functools
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request
import uuid
from .store import Store
from .incidents import Incidents
from . import probes
from .notify import drain
from .log_watch import LogWatch

def deliver_outbox(path,config):
    store=Store(path)
    try:drain(store,config)
    finally:store.db.close()

def upload(config, samples, incidents):
    data=json.dumps({'source':config['source'],'samples':samples,'incidents':incidents},ensure_ascii=False).encode()
    request=urllib.request.Request(config['upload_url'],data=data,headers={
        'Content-Type':'application/json','User-Agent':'vps-observatory/1.0 (+https://github.com/daidaideaa/vps_monitor)',
        'Authorization':'Bearer '+config['upload_token'],'X-Probe-Source':config['source']})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    before=time.time()
    with opener.open(request,timeout=8) as response:
        result=json.load(response)
    after=time.time()
    return {'at':after,'clock_offset_estimate_ms':round((result['server_at']-(before+after)/2)*1000),
            'clock_uncertainty_ms':round((after-before)*500),'requests':result.get('requests',[])}

def cold(config, target):
    core=target.get('cold_command')
    if not core:return {'state':'not_configured','reason':'cold_core_not_configured'}
    with subprocess.Popen(core,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=0x08000000 if os.name=='nt' else 0) as process:
        try:
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if process.poll() is not None:return {'state':'fail','reason':'core_exited'}
                if probes.tcp('127.0.0.1',target['cold_port'])['state']=='ok':break
                time.sleep(.1)
            value=probes.https(config['test_url'],'http://127.0.0.1:'+str(target['cold_port']))
            value['session']='new_core';return value
        finally:
            process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--mark',action='store_true')
    args=parser.parse_args()
    config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    root=Path(config['state_dir']);root.mkdir(parents=True,exist_ok=True)
    if args.mark:
        (root/'manual-trigger').write_text(str(time.time()));print('Fault time recorded locally.');return
    store=Store(root/'probe.sqlite')
    boot=str(uuid.uuid4());seq=0
    # Each process has a fresh boot ID; monotonic elapsed time also exposes suspend gaps.
    incidents=Incidents(store,config['source'])
    from .bridge import start as start_bridge
    bridge=start_bridge(config)
    watcher=LogWatch(config['client_root']) if probes.WINDOWS else None
    pool=ThreadPoolExecutor(max_workers=16,thread_name_prefix='probe')
    tasks={};last_checks={};host={};network={};client={};last_tick=None
    upload_task=None;evidence_tasks={};post_tasks={};last_upload={'state':'waiting','last_error':None}
    next_net=next_hy2=next_cold=next_host=next_inventory=next_trim=next_upload=next_notify=0
    delivery=None
    incident_versions={};upload_failures=0;running=True
    def stop(*_):
        nonlocal running;running=False
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    def launch(key,fn):
        if key not in tasks:tasks[key]=pool.submit(fn)
    try:
        while running:
            tick=time.monotonic();now=time.time();burst=incidents.burst(now)
            for c in last_checks.values():c['new']=False
            for key,future in list(tasks.items()):
                if not future.done():continue
                del tasks[key]
                try:value=future.result()
                except Exception as exc:value={'state':'error','reason':type(exc).__name__}
                if key=='_host':
                    host={**value,'observed_at':now}
                    for service,state in host.get('services',{}).items():
                        last_checks[service]={'state':'ok' if state.get('active')=='active' else 'fail','reason':'service_'+str(state.get('active')),'sampled_at':now,'new':True}
                elif key=='_network':network={**value,'observed_at':now}
                elif key=='_client':client={**value,'observed_at':now}
                else:last_checks[key]={**value,'sampled_at':now,'new':True}
            if now>=next_inventory:
                if probes.WINDOWS:launch('_network',probes.windows_network)
                next_inventory=now+5
            if now>=next_host:
                launch('_host',probes.windows_host if probes.WINDOWS else functools.partial(probes.linux_host,config.get('services',[])))
                if probes.WINDOWS:launch('_client',functools.partial(probes.client_state,config['client_root']))
                next_host=now+5
            bind=network.get('bind_ip') if probes.WINDOWS else None
            if bind:last_checks.pop('physical_interface',None)
            if now>=next_net:
                if probes.WINDOWS and not bind:
                    last_checks['physical_interface']={'state':'error','reason':'physical_route_unknown','new':False}
                else:
                    gateway=(network.get('physical_route') or {}).get('NextHop',config.get('gateway'))
                    if gateway:launch('gateway.icmp',functools.partial(probes.ping,gateway,bind))
                    if probes.WINDOWS:launch('china.icmp',functools.partial(probes.ping,config.get('china_control','223.5.5.5'),bind))
                    for t in config['targets']:
                        launch(t['id']+'.icmp',functools.partial(probes.ping,t['host'],bind))
                        tcp_bind={'ip':bind,'index':network['physical_route']['InterfaceIndex']} if probes.WINDOWS else None
                        launch(t['id']+'.tcp',functools.partial(probes.tcp,t['host'],t['tcp_port'],tcp_bind))
                next_net=now+(1 if burst else 5)
            if now>=next_hy2:
                for t in config['targets']:
                    if t.get('proxy_port'):
                        launch(t['id']+'.hy2',functools.partial(probes.https,config['test_url'],'http://127.0.0.1:'+str(t['proxy_port'])))
                    if t.get('panel_url'):launch(t['id']+'.panel',functools.partial(probes.https,t['panel_url']))
                if probes.WINDOWS:launch('client.http',functools.partial(probes.https,config['test_url'],'http://127.0.0.1:7890'))
                else:
                    launch('egress.https',functools.partial(probes.https,config['test_url']))
                    launch('egress.dns',lambda:probes.measured(lambda:{'answers':len(socket.getaddrinfo('www.cloudflare.com',443))}))
                next_hy2=now+(5 if burst else 15)
            if now>=next_cold:
                for t in config['targets']:
                    launch(t['id']+'.hy2_new',functools.partial(cold,config,t))
                next_cold=now+300
            pending_count=store.db.execute('SELECT COUNT(*) FROM samples WHERE sent=0').fetchone()[0]
            sample={'schema_version':1,'source':config['source'],'boot_id':boot,'seq':seq,'captured_at':now,
                'monotonic_ms':round(tick*1000),'clock':{'offset_estimate_ms':last_upload.get('clock_offset_estimate_ms'),
                'uncertainty_ms':last_upload.get('clock_uncertainty_ms'),'method':'collector_to_cloud_http_midpoint'},
                'checks':{k:dict(v) for k,v in last_checks.items()},'host':host,'network':network,'client':client,
                'upload':{**last_upload,'pending_samples':pending_count},'gap':None,
                'retention_warning':store.get('retention_warning')}
            if watcher:
                events=watcher.poll()
                if events:
                    sample['client']={**client,'new_session_errors':events}
                    sample['checks']['client.session']={'state':'fail','new':True,'reason':'client_log_event','sampled_at':now}
                elif sample['checks'].get('client.http',{}).get('new'):
                    sample['checks']['client.session']={**sample['checks']['client.http'],'reason':'current_client_http_result'}
            if last_tick and tick-last_tick>20:sample['gap']={'seconds':round(tick-last_tick),'reason':'suspend_or_collector_delay_unconfirmed'}
            last_tick=tick
            manual=root/'manual-trigger'
            if manual.exists():
                sample['checks']['manual.hy2']={'state':'fail','new':True,'reason':'user_reported','marked_at':float(manual.read_text())}
                manual.unlink()
            store.add_sample(sample);seq+=1
            for incident in incidents.observe(sample):
                evidence_tasks[incident['id']]=pool.submit(probes.evidence,config,incident['target'])
            for key,future in list(evidence_tasks.items()):
                if future.done():
                    incident=next(i for i in store.incidents() if i['id']==key)
                    try:incident['evidence']['details']=future.result();incident['evidence']['capture']='complete'
                    except Exception as exc:incident['evidence']['capture']=type(exc).__name__
                    store.incident(incident);del evidence_tasks[key]
            for i in store.incidents():
                if i['status']=='complete' and not i.get('evidence',{}).get('post_logs_done') and i['id'] not in post_tasks:
                    post_tasks[i['id']]=pool.submit(probes.log_events,config,window=i)
            for key,future in list(post_tasks.items()):
                if future.done():
                    incident=next(i for i in store.incidents() if i['id']==key)
                    try:incident['evidence']['post_logs']=future.result()
                    except Exception as exc:incident['evidence']['post_log_error']=type(exc).__name__
                    incident['evidence']['post_logs_done']=True;store.incident(incident);del post_tasks[key]
            if upload_task and upload_task[0].done():
                future,ids,versions=upload_task
                try:
                    uploaded=future.result()
                    for job in uploaded.pop('requests',[]):
                        if job.get('source')!=config['source'] or store.get('evidence_job:'+job['id']):continue
                        incident={**job,'status':'collecting','recovered_at':None,
                            'report':{'facts':['其他观测点请求关联本机证据。'],'inferences':['待关联判断。'],'missing':[]},
                            'evidence':{'pre_seconds':600,'post_seconds':300,'capture':'pending'}}
                        history=store.window(job['started_at']-600,job['started_at'])
                        incident['evidence']['available_pre_seconds']=min(600,job['started_at']-history[0]['captured_at']) if history else 0
                        store.incident(incident);store.set('evidence_job:'+job['id'],True)
                        evidence_tasks[job['id']]=pool.submit(probes.evidence,config,job['target'],job)
                    last_upload={**uploaded,'state':'ok','last_error':None}
                    store.acknowledge(ids);incident_versions.update(versions);upload_failures=0;next_upload=now
                except Exception as exc:
                    upload_failures+=1
                    last_upload={**last_upload,'state':'failed','last_error':type(exc).__name__,'failed_at':now}
                    next_upload=now+min(60,2**min(upload_failures,6))
                upload_task=None
            if not upload_task and now>=next_upload:
                pending=store.pending(10)
                changed=[i for i in store.incidents() if json.dumps(i,sort_keys=True)!=incident_versions.get(i['id'])][:2]
                while pending and len(json.dumps({'samples':[s for _,s in pending],'incidents':changed},ensure_ascii=False).encode())>220000:
                    pending.pop()
                # An older offline buffer is replayed with its original timestamps.
                upload_task=(pool.submit(upload,config,[s for _,s in pending],changed),[i for i,_ in pending],
                    {i['id']:json.dumps(i,sort_keys=True) for i in changed})
            if now>=next_trim:
                store.trim(max_bytes=config.get('buffer_mb',80)*1024*1024);next_trim=now+60
            if now>=next_notify and (delivery is None or delivery.done()):
                if delivery:
                    try:delivery.result()
                    except Exception as exc:print('Notification worker:',type(exc).__name__,flush=True)
                delivery=pool.submit(deliver_outbox,root/'probe.sqlite',config.get('notify',{}));next_notify=now+3
            if args.once:
                print(json.dumps({'source':config['source'],'state':'sampled','checks':list(last_checks)}));break
            time.sleep(max(.05,(1 if burst else 5)-(time.monotonic()-tick)))
    finally:
        bridge.shutdown();bridge.server_close()
        pool.shutdown(wait=False,cancel_futures=True);store.db.close()

if __name__=='__main__':main()
