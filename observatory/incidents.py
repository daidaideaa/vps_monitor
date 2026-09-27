"""Evidence-led incidents. No single timeout is attributed to a carrier."""
import time
import uuid

def classify(samples):
    facts, inference, missing = [], [], []
    checks = {}; observed={}; events=[]
    for sample in samples:
        for key, value in sample.get('checks', {}).items():
            if value.get('state') in ('ok', 'fail'):
                checks[key] = value
                observed.setdefault(key,set()).add(value['state'])
                if value.get('new') is not False:
                    events.append({'metric':key,'state':value['state'],'at':value.get('sampled_at',sample['captured_at'])})
    failed = [k for k,v in observed.items() if 'fail' in v]
    good = [k for k,v in observed.items() if 'ok' in v]
    facts += ['时间窗内曾失败：'+', '.join(failed)] if failed else []
    facts += ['时间窗内曾成功：'+', '.join(good)] if good else []
    if any(k in failed for k in ('gateway.icmp', 'china.icmp')):
        inference.append('本机或本地接入优先排查；须结合远端观测确认。')
    if 'client.http' in failed and any(k.endswith('.hy2') for k in good):
        inference.append('当前客户端与独立探针结果不同，优先检查客户端配置、TUN、路由或会话。')
    for k in failed:
        if k.endswith('.hy2') and k.removesuffix('.hy2')+'.tcp' in good:
            inference.append(k+': TCP 可达，HY2 请求失败；可能涉及 UDP、认证、远端出口或主机网络栈，待定位。')
        if k.endswith('.dns') or k.endswith('.https'):
            inference.append('存在 DNS/HTTPS 出口检查失败；结合 HY2 握手与服务日志判断。')
    if failed and all(k.endswith('.icmp') for k in failed) and any(k.endswith(('.tcp','.hy2')) for k in good):
        inference.append('仅 ICMP 异常，现有证据不支持判定代理断线。')
    for sample in samples:
        for service, state in sample.get('host',{}).get('services',{}).items():
            if state.get('active') not in (None,'active'):
                facts.append(service+' 服务状态：'+str(state.get('active')))
                inference.append('存在 VPS 服务侧异常证据。')
        if sample.get('gap'):
            facts.append('采集出现间隔：'+str(sample['gap']))
    missing += ['单点观测不能确认运营商故障；需关联其他探针和两端 UDP 元数据。']
    if not inference: inference.append('待定位：现有证据不足以确定故障层。')
    # A compact timeline supports correlation without downloading every raw sample.
    relevant=[e for e in events if e['metric'].endswith(('.hy2','.tcp')) or e['metric'] in ('gateway.icmp','china.icmp','client.http')]
    # Keep state transitions (including a brief failure) before periodic observations.
    transitions=[]; previous={}
    for event in relevant:
        if previous.get(event['metric']) != event['state']:transitions.append(event)
        previous[event['metric']]=event['state']
    periodic=relevant[::max(1,len(relevant)//60)]
    timeline={ (e['metric'],e['at']):e for e in periodic+transitions }
    if len(timeline)>180:missing.append('简要时间线已裁剪；完整采样见证据下载。')
    return {'facts': list(dict.fromkeys(facts)), 'inferences': list(dict.fromkeys(inference)), 'missing': missing,'events':sorted(timeline.values(),key=lambda e:e['at'])[-180:]}

class Incidents:
    def __init__(self, store, source, evidence=None):
        self.store, self.source, self.evidence = store, source, evidence
        self.state = store.get('incident_state', {})

    def observe(self, sample, manual=False):
        now = sample['captured_at']
        triggered = []
        for target, value in sample.get('checks',{}).items():
            if not (target.endswith(('.hy2','.service')) or target in ('client.http','client.session') or manual):
                continue
            if value.get('state') not in ('ok','fail'): continue
            if value.get('new') is False: continue
            s = self.state.setdefault(target, {'fails':0, 'success':0, 'last_trigger':0, 'active':None,'alerted':False})
            if value['state']=='fail':
                s['fails']+=1; s['success']=0
                if not s['active'] and now-s['last_trigger']>=600:
                    incident={'id':str(uuid.uuid4()),'source':self.source,'target':target,
                        'started_at':now,'end_at':now+300,'status':'collecting','recovered_at':None,
                        'report':classify(self.store.window(now-600,now)),
                        'evidence':{'pre_seconds':600,'post_seconds':300,'capture':'pending'}}
                    history=self.store.window(now-600,now)
                    incident['evidence']['available_pre_seconds']=min(600,now-history[0]['captured_at']) if history else 0
                    if incident['evidence']['available_pre_seconds']<590:
                        incident['report']['missing'].append('此前 10 分钟记录不完整，可能因为探针刚启动或本地缓存已裁剪。')
                    s.update(active=incident['id'],last_trigger=now)
                    self.store.incident(incident);triggered.append(incident)
                if s['fails']==3:
                    s['alerted']=True
                    self.store.enqueue('link-down:'+str(s['active'] or now),
                        f'[链路异常] {self.source} / {target}\n连续 3 次实际探测失败。首次异常已留证据，原因待定位。')
            else:
                s['success']+=1;s['fails']=0
                if s['success']==2 and s.get('active'):
                    incident=next((i for i in self.store.incidents() if i['id']==s['active']),None)
                    if incident:
                        incident['recovered_at']=now
                        self.store.incident(incident)
                    if s.get('alerted'):
                        self.store.enqueue('link-up:'+s['active'],f'[链路恢复] {self.source} / {target}\n连续 2 次成功；故障证据继续保存。')
                    s['alerted']=False
                    s['active']=None
        # Collection keeps its five-minute tail even after a fast recovery.
        for i in self.store.incidents():
            if i['status']=='collecting' and now>=i['end_at']:
                i['status']='complete';i['report']=classify(self.store.window(i['started_at']-600,i['end_at']))
                if i.get('evidence',{}).get('available_pre_seconds',0)<590:
                    i['report']['missing'].append('此前 10 分钟记录不完整，详见 available_pre_seconds。')
                self.store.incident(i)
        self.store.set('incident_state',self.state)
        return triggered

    def burst(self, now=None):
        now=now or time.time()
        return any(i['status']=='collecting' and now<i['end_at'] for i in self.store.incidents(now-600))
