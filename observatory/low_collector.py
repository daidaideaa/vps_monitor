"""120-second small probes, bounded verification and batched VPS upload."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import functools
import gzip
import json
import os
from pathlib import Path
import signal
import socket
import time
import urllib.request
import uuid
from . import probes
from .collector import cold
from .log_watch import LogWatch
from .low_state import LowIncidents, budget_level
from .store import Store


def send(config, samples, incidents):
    raw=json.dumps({'source':config['source'],'samples':samples,'incidents':incidents},ensure_ascii=False).encode()
    body=gzip.compress(raw)
    if len(body)>250000:raise ValueError('upload_batch_too_large')
    request=urllib.request.Request(config['upload_url'],body,headers={'Content-Type':'application/json',
        'Content-Encoding':'gzip','Authorization':'Bearer '+config['upload_token'],'X-Probe-Source':config['source']})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    before=time.time()
    with opener.open(request,timeout=8) as r:
        response=r.read(65536);result=json.loads(response)
    elapsed=time.time()-before
    return {'at':time.time(),'state':'ok','payload_bytes':len(body)+len(response),'budget_level':result.get('budget_level',0),
        'clock_offset_estimate_ms':round((result['server_at']-before-elapsed/2)*1000),
        'clock_uncertainty_ms':round(elapsed*500)}


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--once',action='store_true');args=p.parse_args()
    config_path=Path(args.config);config=json.loads(config_path.read_text(encoding='utf-8-sig'))
    root=Path(config['state_dir']);root.mkdir(parents=True,exist_ok=True)
    store=Store(root/'probe.sqlite',capture_evidence=False);store.cancel_legacy_notifications()
    incidents=LowIncidents(store,config['source']);incidents.gap()
    watcher=LogWatch(config['client_root']) if probes.WINDOWS else None
    boot=str(uuid.uuid4());seq=0;running=True;last_tick=time.time()
    interval=int(config.get('interval',120));next_regular=next_log=next_upload=next_trim=next_budget=0
    next_cold=time.time()+int(config.get('cold_interval',1800))
    network={};host={};client={};checks={};due={};upload_state={'state':'waiting'};upload_job=None;versions={};failures=0
    traffic=store.get('traffic',{'rx_bytes':0,'tx_bytes':0,'last':{},'started_at':time.time()})
    budget=store.get('budget',{'started_at':time.time(),'estimated_probe_bytes':0,'measured_upload_payload_bytes':0,'level':0,'checks':0})
    pool=ThreadPoolExecutor(max_workers=4);uploader=ThreadPoolExecutor(max_workers=1)
    def stop(*_):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)

    def jobs():
        funcs={}
        bind=network.get('bind_ip') if probes.WINDOWS else None
        if probes.WINDOWS and not bind:return funcs
        tcp_bind={'ip':bind,'index':network['physical_route']['InterfaceIndex']} if bind else None
        gateway=(network.get('physical_route') or {}).get('NextHop')
        if gateway:funcs['gateway.icmp']=functools.partial(probes.ping,gateway,bind)
        if probes.WINDOWS:funcs['china.icmp']=functools.partial(probes.ping,'223.5.5.5',bind)
        for t in config.get('targets',[]):
            if not t.get('enabled',True):continue
            funcs[t['id']+'.icmp']=functools.partial(probes.ping,t['host'],bind)
            funcs[t['id']+'.tcp']=functools.partial(probes.tcp,t['host'],t['tcp_port'],tcp_bind)
            if t.get('proxy_port'):funcs[t['id']+'.hy2']=functools.partial(probes.https,config['test_url'],'http://127.0.0.1:'+str(t['proxy_port']))
        if probes.WINDOWS:funcs['client.http']=functools.partial(probes.https,config['test_url'],'http://127.0.0.1:7890')
        else:
            funcs['egress.https']=functools.partial(probes.https,config['test_url'])
            funcs['egress.dns']=lambda:probes.measured(lambda:{'answers':len(socket.getaddrinfo('www.gstatic.com',443))})
            def service_check(name):
                value=probes.linux_host([name])['services'][name]
                return {'state':'ok' if value['active']=='active' else 'fail','reason':'service_'+str(value['active'])}
            for name in config.get('services',[]):funcs[name]=functools.partial(service_check,name)
        return funcs

    def append(results,kind,logs=None,gap=None):
        nonlocal seq
        now=time.time()
        for c in checks.values():c['new']=False
        for key,value in results.items():
            value={**value,'sampled_at':now,'kind':kind,'new':True};checks[key]=value
            if key.endswith(('.hy2','.service')) or key=='client.http':
                deadline=incidents.observe(key,value,now,kind)
                if deadline:due[key]=deadline
                else:due.pop(key,None)
            budget['checks']+=1
            # Conservative transport allowance, not a claim to measure ISP billable bytes.
            budget['estimated_probe_bytes']+=256 if key.endswith('.icmp') else 2048 if key.endswith(('.tcp','.dns')) else 8192 if not key.endswith('.service') else 0
        sample={'schema_version':2,'source':config['source'],'boot_id':boot,'seq':seq,'captured_at':now,
            'monotonic_ms':round(time.monotonic()*1000),'kind':kind,'interval':interval,
            'checks':{k:dict(v) for k,v in checks.items()},'host':host,'network':network,'client':{**client,**({'new_session_errors':logs} if logs else {})},
            'gap':gap,'upload':{**upload_state,'pending_samples':store.db.execute('SELECT COUNT(*) FROM samples WHERE sent=0').fetchone()[0]},
            'budget':dict(budget),'traffic':{k:v for k,v in traffic.items() if k!='last'}}
        store.add_sample(sample);seq+=1
        # Bounded local evidence only: no trace process, packet capture or repeated full log dump.
        for i in store.incidents():
            if not i.get('evidence',{}).get('details'):
                i['evidence']['details']={'log_source':'Clash Party core log' if probes.WINDOWS else 'systemd journal',
                    'log_window_start':i['started_at']-600,'log_window_end':i['end_at'],
                    'log_excerpt':[str(line)[:300] for line in probes.log_events(config)[-8:]],
                    'services':host.get('services',{}),'route_fingerprint':network.get('fingerprint'),
                    'limitations':['原日志为主证据；采样按时间引用，不重复复制。无包捕获或路由探测。']}
                store.incident(i)
        incidents.finish_evidence(now)
        for i in store.incidents():
            if i.get('status')=='complete' and not i['evidence'].get('tail_logs_saved'):
                details=i['evidence'].get('details',{})
                old=details.get('log_excerpt',[])
                details['log_excerpt']=list(dict.fromkeys(old+[str(line)[:300] for line in probes.log_events(config,window=i)[-8:]]))[-10:]
                i['evidence'].update(details=details,tail_logs_saved=True)
                store.incident(i)

    def execute(funcs,kind):
        futures={k:pool.submit(fn) for k,fn in funcs.items()};results={}
        for k,f in futures.items():
            try:results[k]=f.result()
            except Exception as exc:results[k]={'state':'error','reason':type(exc).__name__}
        if results:append(results,kind)

    try:
        while running:
            now=time.time()
            if now-last_tick>max(30,interval*2):
                gap={'seconds':round(now-last_tick),'reason':'sleep_or_probe_gap_unconfirmed'}
                incidents.gap();due.clear();append({},'gap',gap=gap);next_regular=0
            last_tick=now
            if now>=next_regular:
                config=json.loads(config_path.read_text(encoding='utf-8-sig'))
                interval=300 if budget['level']>=2 else int(config.get('interval',120))
                if probes.WINDOWS:
                    try:network=probes.windows_network()
                    except Exception as exc:network={'state':'unknown','reason':type(exc).__name__}
                    host=probes.windows_host()
                    try:client=probes.client_state(config['client_root'])
                    except Exception as exc:client={'state':'unknown','reason':type(exc).__name__}
                else:host=probes.linux_host(config.get('services',[]))
                nics=host.get('netdev',{})
                selected=[network.get('physical_route',{}).get('InterfaceAlias')] if probes.WINDOWS else [n for n in nics if n!='lo' and not n.startswith(('veth','docker','br-','tun'))]
                for nic in selected:
                    value=nics.get(nic,{})
                    for key in ('rx_bytes','tx_bytes'):
                        if key not in value:continue
                        ident=nic+':'+key;last=traffic['last'].get(ident,value[key])
                        traffic[key]+=value[key]-last if value[key]>=last else value[key]
                        traffic['last'][ident]=value[key]
                store.set('traffic',traffic)
                service_results={k:{'state':'ok' if v['active']=='active' else 'fail','reason':'service_'+str(v['active'])} for k,v in host.get('services',{}).items()}
                if probes.WINDOWS and not network.get('bind_ip'):
                    incidents.gap();due.clear();append({'physical_interface':{'state':'unknown','reason':'physical_route_missing'}},'regular')
                else:
                    funcs=jobs()
                    funcs.update({k:functools.partial(dict,v) for k,v in service_results.items()})
                    execute(funcs,'regular')
                next_regular=now+interval
            if watcher and now>=next_log:
                logs=watcher.poll();next_log=now+5
                if logs:
                    append({},'log',logs=logs)
                    # Logs refer to the selected client; never label Flower as VMISS.
                    s=incidents.states.get('client.http',{})
                    if now-s.get('last_log_verify',-1e12)>=600:
                        s['last_log_verify']=now
                        if 'client.http' in incidents.states:incidents.save()
                        due['client.http']=now
            selected={k:v for k,v in jobs().items() if due.get(k,float('inf'))<=now}
            for key in selected:due.pop(key,None)
            if selected:execute(selected,'retry')
            if now>=next_cold and budget['level']==0:
                execute({t['id']+'.hy2_new':functools.partial(cold,config,t) for t in config.get('targets',[]) if t.get('enabled',True) and t.get('cold_command')},'cold')
                next_cold=now+int(config.get('cold_interval',1800))
            if upload_job and upload_job[0].done():
                future,ids,changed=upload_job
                try:
                    upload_state=future.result();store.acknowledge(ids);versions.update(changed);failures=0
                    budget['measured_upload_payload_bytes']+=upload_state['payload_bytes'];next_upload=time.time()+120
                    budget['level']=max(budget['level'],upload_state.get('budget_level',0))
                except Exception as exc:
                    failures+=1;upload_state={'state':'failed','reason':type(exc).__name__,'failed_at':time.time()}
                    next_upload=time.time()+min(900,120*2**min(failures-1,3))
                upload_job=None
            if not upload_job and now>=next_upload:
                pending=store.pending(30)
                changed=[i for i in store.incidents() if json.dumps(i,sort_keys=True)!=versions.get(i['id'])][:5]
                while pending and len(json.dumps({'samples':[s for _,s in pending],'incidents':changed}).encode())>800000:pending.pop()
                if pending or changed:
                    upload_job=(uploader.submit(send,config,[s for _,s in pending],changed),[i for i,_ in pending],{i['id']:json.dumps(i,sort_keys=True) for i in changed})
                else:next_upload=now+120
            if now>=next_budget:
                elapsed=max(86400,now-budget['started_at'])
                # Reserve 5 MiB/day for keepalives, protocol overhead and private UI usage.
                budget['monthly_estimate_bytes']=round((budget['estimated_probe_bytes']+budget['measured_upload_payload_bytes'])/elapsed*30*86400+150*1024*1024)
                if now-budget['started_at']>=86400:budget['level']=budget_level(budget['monthly_estimate_bytes'],budget['level'])
                budget['accounting']='payload_measured_plus_probe_estimates_and_keepalive_reserve'
                store.set('budget',budget);next_budget=now+86400 if now-budget['started_at']>=86400 else now+3600
            if now>=next_trim:
                store.trim(max_bytes=int(config.get('buffer_mb',40))*1024*1024);next_trim=now+3600
            if args.once:
                if upload_job:upload_job[0].result(timeout=15)
                break
            time.sleep(1)
    finally:
        store.set('budget',budget);store.db.close();pool.shutdown(wait=False,cancel_futures=True);uploader.shutdown(wait=False,cancel_futures=True)


if __name__=='__main__':main()
