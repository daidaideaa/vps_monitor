"""Bounded checks. Proxy credentials and arbitrary browsing activity are never emitted."""
import concurrent.futures
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.request
import sys

WINDOWS = os.name=='nt'
def command(args, timeout=4):
    return subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=timeout, creationflags=0x08000000 if WINDOWS else 0)

def measured(fn):
    start=time.monotonic()
    try:
        extra=fn() or {}
        return {'state':'ok','ms':round((time.monotonic()-start)*1000,2),**extra}
    except Exception as exc:
        return {'state':'fail','ms':None,'reason':type(exc).__name__}

def tcp(host,port,bind=None):
    def run():
        if isinstance(bind,dict):
            with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
                sock.settimeout(2);sock.bind((bind['ip'],0))
                if WINDOWS:sock.setsockopt(socket.IPPROTO_IP,31,socket.htonl(bind['index']))
                sock.connect((host,port))
            return
        with socket.create_connection((host,port),timeout=2,source_address=(bind,0) if bind else None): pass
    return measured(run)

def ping(host,bind=None):
    args=['ping','-n','1','-w','900'] if WINDOWS else ['ping','-n','-c','1','-W','1']
    if bind: args+=['-S' if WINDOWS else '-I',bind]
    result=command(args+[host],2)
    if result.returncode: return {'state':'fail','ms':None,'reason':'icmp_timeout'}
    match=re.search(r'(?:time|时间)[=<]\s*([\d.]+)\s*ms',result.stdout,re.I)
    if not match: match=re.search(r'[=<]\s*([\d.]+)\s*ms',result.stdout)
    return {'state':'ok','ms':float(match[1]) if match else None}

def https(url,proxy=None,bind=None):
    # curl supports source binding and never inherits HTTP_PROXY from the environment.
    args=['curl.exe' if WINDOWS else 'curl','--silent','--show-error','--output',os.devnull,
          '--max-time','4','--connect-timeout','2','--write-out','%{http_code} %{time_total}',
          '--proxy',proxy or '', '--noproxy','' if proxy else '*']
    if bind: args+=['--interface',bind]
    try:result=command(args+[url],5)
    except FileNotFoundError:
        # Minimal Linux hosts use the standard library; Windows keeps curl source binding.
        if bind:return {'state':'error','reason':'bound_http_tool_missing'}
        def fetch():
            handler=urllib.request.ProxyHandler({'https':proxy,'http':proxy} if proxy else {})
            opener=urllib.request.build_opener(handler)
            request=urllib.request.Request(url,headers={'User-Agent':'vps-observatory/1.0'})
            with opener.open(request,timeout=4) as response:
                if not 200<=response.status<400:raise RuntimeError('http_status')
                return {'http_status':response.status}
        return measured(fetch)
    parts=result.stdout.strip().split()
    if result.returncode or len(parts)!=2:
        return {'state':'fail','ms':None,'reason':'curl_'+str(result.returncode),'detail':safe_text(result.stderr)[:300]}
    code=int(parts[0])
    return {'state':'ok' if 200<=code<400 else 'fail','ms':round(float(parts[1])*1000,2),'http_status':code}

def linux_host(services):
    host={}
    host['load']=list(os.getloadavg())
    mem={l.split(':')[0]:int(l.split()[1])*1024 for l in Path('/proc/meminfo').read_text().splitlines()}
    host['memory']={k:mem[k] for k in ('MemTotal','MemAvailable','SwapTotal','SwapFree')}
    host['netdev']={}
    for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
        nic,values=line.split(':',1);v=list(map(int,values.split()))
        host['netdev'][nic.strip()]={'rx_bytes':v[0],'rx_errors':v[2],'rx_drop':v[3],'tx_bytes':v[8],'tx_errors':v[10],'tx_drop':v[11]}
    lines=Path('/proc/net/snmp').read_text().splitlines()
    for i,line in enumerate(lines):
        if line.startswith('Udp:'):
            host['udp']=dict(zip(line.split()[1:],map(int,lines[i+1].split()[1:])));break
    host['services']={}
    for service in services:
        result=command(['systemctl','show',service,'-p','ActiveState,NRestarts,ExecMainStartTimestampMonotonic,ExecMainStatus'],2)
        v=dict(l.split('=',1) for l in result.stdout.splitlines() if '=' in l)
        host['services'][service]={'active':v.get('ActiveState'),'restarts':v.get('NRestarts'),'started_monotonic':v.get('ExecMainStartTimestampMonotonic'),'exit':v.get('ExecMainStatus')}
    host['clock_synchronized']=command(['timedatectl','show','-p','NTPSynchronized','--value'],2).stdout.strip()=='yes'
    return host

def windows_network():
    script=r"""
    $ErrorActionPreference='Stop'
    $r=Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' | Where-Object {$_.NextHop -ne '0.0.0.0'} | Sort-Object RouteMetric
    $a=Get-NetAdapter -Physical | Where-Object Status -eq 'Up'
    $ips=Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.InterfaceIndex -in $a.ifIndex}
    @{routes=@($r|Select-Object InterfaceIndex,InterfaceAlias,NextHop,RouteMetric);adapters=@($a|Select-Object Name,ifIndex,Status,LinkSpeed);addresses=@($ips|Select-Object InterfaceIndex,IPAddress)}|ConvertTo-Json -Depth 5 -Compress
    """
    result=command(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],7)
    if result.returncode: raise RuntimeError('network_inventory_failed')
    data=json.loads(result.stdout);physical={a['ifIndex'] for a in data['adapters']}
    route=next((r for r in data['routes'] if r['InterfaceIndex'] in physical),None)
    data['physical_route']=route
    data['bind_ip']=next((i['IPAddress'] for i in data['addresses'] if route and i['InterfaceIndex']==route['InterfaceIndex']),None)
    data['fingerprint']=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()[:16]
    return data

def windows_host():
    import psutil
    mem=psutil.virtual_memory()
    return {'cpu_percent':psutil.cpu_percent(interval=None),'memory':{'total':mem.total,'available':mem.available},
        'netdev':{k:{'rx_bytes':v.bytes_recv,'tx_bytes':v.bytes_sent,'rx_drop':v.dropin,'tx_drop':v.dropout}
                  for k,v in psutil.net_io_counters(pernic=True).items()}}

def pipe_api(pipe,secret,path):
    with open(pipe,'r+b',buffering=0) as handle:
        class PipeSocket:
            def makefile(self,*args,**kwargs):return handle
        handle.write((f'GET {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {secret}\r\nConnection: close\r\n\r\n').encode())
        response=http.client.HTTPResponse(PipeSocket());response.begin()
        if response.status!=200:raise RuntimeError('client_controller_failed')
        return json.loads(response.read(1024*1024))

def client_state(root):
    import yaml
    import psutil
    root=Path(root)
    config=yaml.safe_load((root/'work/config.yaml').read_text(encoding='utf-8')) or {}
    profiles=yaml.safe_load((root/'profile.yaml').read_text(encoding='utf-8')) or {}
    current=next((p.get('name') for p in profiles.get('items',[]) if p.get('id')==profiles.get('current')),profiles.get('current'))
    safe={k:config.get(k) for k in ('mode','mixed-port','tun','interface-name')}
    safe['profile']=current
    safe['config_fingerprint']=hashlib.sha256(json.dumps(safe,sort_keys=True).encode()).hexdigest()
    safe['pid']=[p.pid for p in psutil.process_iter(['name']) if (p.info['name'] or '').lower()=='mihomo.exe']
    pipe=config.get('external-controller-pipe')
    if not pipe:
        try:pipe=next(('\\\\.\\pipe\\'+name for name in os.listdir('\\\\.\\pipe\\') if name.startswith('MihomoParty\\mihomo-')),None)
        except OSError:pass
    if pipe:
        try:
            proxies=pipe_api(pipe,str(config.get('secret','')),'/proxies')['proxies']
            safe['groups']={k:v.get('now') for k,v in proxies.items() if 'now' in v}
            actual=pipe_api(pipe,str(config.get('secret','')),'/configs')
            safe['tun']=actual.get('tun',safe['tun'])
        except Exception as exc:safe['controller_error']=type(exc).__name__
    safe['listener']=tcp('127.0.0.1',7890)['state']
    safe['config_fingerprint']=hashlib.sha256((root/'work/config.yaml').read_bytes()).hexdigest()
    return safe

def safe_text(text):
    text=re.sub(r'\x1b\[[0-9;]*[a-zA-Z]','',str(text))
    text=re.sub(r'https?://[^\s"\']+','[URL]',text)
    text=re.sub(r'(?i)(password|token|secret|authorization|auth)\s*[=:]\s*[^,\s}]+',r'\1=[REDACTED]',text)
    return text[:18000]

def log_events(config, since=600, window=None):
    if WINDOWS:
        directory=Path(config['client_root'])/'logs'
        files=sorted(directory.glob('core-*.log'),key=lambda p:p.stat().st_mtime,reverse=True)[:1]
        if not files:return []
        with files[0].open('rb') as f:
            f.seek(max(0,files[0].stat().st_size-180000));text=f.read().decode('utf-8','replace')
        return [safe_text(line)[:400] for line in text.splitlines() if re.search(r'JP-Home|VMISS|HY2|TUN|tun.*fail|network.*chang|no recent network|stateless reset',line,re.I)][-30:]
    args=['journalctl','--since','@'+str(int(window['started_at']-600)) if window else f'{since} seconds ago','--no-pager','-o','json','-n','150']
    if window:args+=['--until','@'+str(int(min(time.time(),window['end_at'])))]
    for service in config.get('services',[]):args+=['-u',service]
    result=command(args,4);logs=[]
    for line in result.stdout.splitlines():
        try:
            record=json.loads(line);message=record.get('MESSAGE','')
            if isinstance(message,list):message=bytes(message).decode('utf-8','replace')
            logs.append({'at':record.get('__REALTIME_TIMESTAMP'),'service':record.get('_SYSTEMD_UNIT'),'message':safe_text(message)[:400]})
        except (ValueError,TypeError):pass
    return logs[-30:]

def evidence(config,target,window=None):
    if target.endswith('.service'):target='server.local'
    result={'captured_at':time.time(),'logs':log_events(config,window=window),'limitations':[]}
    host=next((t['host'] for t in config['targets'] if target.startswith(t['id'])),config['targets'][0]['host'] if config['targets'] else None)
    if host:
        try:
            args=['tracert','-d','-h','12','-w','300',host] if WINDOWS else ['tracepath','-n',host]
            result['route']=safe_text(command(args,18).stdout)
        except FileNotFoundError:
            try:
                from .route_metadata import trace
                result['route']=trace(host)
            except Exception as exc:result['limitations'].append('route:'+type(exc).__name__)
        except Exception as exc:result['limitations'].append('route:'+type(exc).__name__)
    if WINDOWS:
        # PktMon counters/drop counters expose metadata only. Capturing payloads is not enabled.
        try:
            metadata=Path(config['state_dir']).parent/'pktmon-latest.json'
            if metadata.exists():
                data=json.loads(metadata.read_text(encoding='utf-8-sig'))
                result['udp_metadata']={'tool':'pktmon','state':'ok' if time.time()-data['captured_at']<20 else 'stale','counters':data}
            else:
                r=command(['pktmon','counters','--json'],4)
                result['udp_metadata']={'tool':'pktmon','state':'ok' if r.returncode==0 else 'requires_admin_or_active_session','counters':safe_text(r.stdout)}
        except Exception as exc:result['limitations'].append('pktmon:'+type(exc).__name__)
        script="$since=(Get-Date).AddMinutes(-15); @('Microsoft-Windows-WLAN-AutoConfig/Operational','System') | ForEach-Object { Get-WinEvent -FilterHashtable @{LogName=$_;StartTime=$since} -MaxEvents 30 -ErrorAction SilentlyContinue | Where-Object {$_.Id -in 1,42,107,506,507,8001,8003} | Select-Object TimeCreated,Id,ProviderName } | ConvertTo-Json -Compress"
        try:result['power_wifi_events']=safe_text(command(['powershell.exe','-NoProfile','-Command',script],7).stdout)
        except Exception as exc:result['limitations'].append('power_events:'+type(exc).__name__)
    elif host or target=='server.local':
        try:
            from .udp_metadata import capture
            if target=='server.local':
                if window and window['end_at']<time.time():result['limitations'].append('Historical UDP metadata unavailable; original-time logs retained.')
                else:result['udp_metadata']=capture(None,port=config['local_udp_port'])
            else:result['udp_metadata']=capture(host)
        except Exception as exc:result['limitations'].append('udp_metadata:'+type(exc).__name__)
        try:
            start=int(window['started_at']-600) if window else int(time.time()-600)
            kernel=command(['journalctl','-k','--since','@'+str(start),'--no-pager','-n','150'],4).stdout
            result['kernel_events']=[safe_text(l)[:500] for l in kernel.splitlines() if re.search(r'oom|out of memory|killed process|link.*down|NETDEV WATCHDOG|drop',l,re.I)][-20:]
        except Exception as exc:result['limitations'].append('kernel_log:'+type(exc).__name__)
    return result
