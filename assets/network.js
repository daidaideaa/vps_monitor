'use strict';
let protocol='hy2';
let VMISS_METRICS = ['vmiss.icmp', 'vmiss.tcp', 'vmiss.hy2'];
const EVENT_TARGETS = ['vmiss.hy2', 'vmiss-hy2.service', 'vmiss.vless', 'vmiss-vless.service'];
function recentEvents(list, now) {
 return list.filter(i=>EVENT_TARGETS.includes(i.target)&&['windows','vmiss'].includes(i.source)&&i.started_at<=now&&(!(i.recovered_at||i.monitoring_ended_at)||(i.recovered_at||i.monitoring_ended_at)>=now-86400));
}
function historyRange(samples, now) {
 const times=samples.filter(s=>s.source==='windows'&&s.captured_at>=now-86400&&s.captured_at<=now&&VMISS_METRICS.some(k=>s.checks?.[k])).map(s=>s.captured_at);
 return {start:times.length?Math.max(now-86400,Math.min(...times)):now-3600,end:now,hasData:times.length>0};
}
function chartSeries(samples,metric,start,end) {
 const points=samples.filter(s=>s.source==='windows'&&s.captured_at>=start&&s.captured_at<=end&&s.checks?.[metric]).map(s=>({t:s.captured_at,v:s.checks[metric],interval:s.interval||300})).sort((a,b)=>a.t-b.t);
 const valid=points.filter(p=>p.v.state==='ok'&&Number.isFinite(p.v.ms)&&p.v.ms>=0);
 const ceiling=Math.max(100,Math.ceil(Math.max(0,...valid.map(p=>p.v.ms))/100)*100);
 const segments=[],failed=[];let segment=[],previous=null;
 const flush=()=>{if(segment.length)segments.push(segment);segment=[];};
 for(const p of points){
  if(previous&&p.t-previous.t>Math.max(p.interval,previous.interval)*1.5)flush();
  if(p.v.state!=='ok'||!Number.isFinite(p.v.ms)||p.v.ms<0){flush();if(p.v.state==='fail')failed.push(p);}
  else segment.push(p);
  previous=p;
 }
 flush();return {segments,failed,ceiling,points};
}
if(typeof module!=='undefined')module.exports={recentEvents,historyRange,chartSeries};
if(typeof document!=='undefined'){
const API='https://vps-monitor.daidaidefish.workers.dev/api/network';
let history=[],busy=false,lastLoad=0,lastHistory=0;
const $=id=>document.getElementById(id);
const fmt=(t,short=false)=>t?new Date(t*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',...(short?{}:{second:'2-digit'})}):'尚无记录';
const clock=t=>new Date(t*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,hour:'2-digit',minute:'2-digit'});
const node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
const bytes=n=>Number.isFinite(n)?(n/1048576).toFixed(1)+' MiB':'—';
const ms=n=>Number.isFinite(n)?Math.round(n).toLocaleString('zh-CN'):'—';
const duration=n=>n<60?Math.round(n)+' 秒':n<3600?Math.round(n/60)+' 分钟':(n/3600).toFixed(1)+' 小时';
const fresh=e=>e?.sample&&Date.now()/1000-e.sample.captured_at<=Math.max(360,(e.sample.interval||120)*3);
const names={'vmiss.icmp':'ICMP','vmiss.tcp':'TCP 管理端口','vmiss.hy2':'HY2','vmiss.vless':'VLESS','gateway.icmp':'本地网关','china.icmp':'国内对照','client.http':'日常代理请求','vmiss-hy2.service':'HY2 备用服务','vmiss-vless.service':'VLESS 服务','egress.https':'HTTPS 出口','egress.dns':'DNS 解析'};
const stateText={ok:'正常',fail:'失败',unknown:'缺测',error:'待定位'};
async function api(path){const r=await fetch(API+path,{signal:AbortSignal.timeout(12000)});if(!r.ok)throw Error('数据服务不可达');return r.json();}
function card(label,value,unit,note,tone=''){
 const c=node('article',undefined,'metric-card'),v=node('p',value,'metric-value '+tone);if(unit)v.append(node('small',unit));
 c.append(node('h2',label,'metric-label'),v,node('p',note,'metric-note'));return c;
}
function overview(data,events){
 const w=data.sources.windows,s=data.sources.vmiss,h=w?.sample?.checks?.['vmiss.'+protocol],stats=data.statistics.regular_checks.windows?.['vmiss.'+protocol];
 const total=(stats?.good||0)+(stats?.bad||0),count=events.filter(i=>i.source==='windows'&&i.target==='vmiss.'+protocol&&i.confirmed_at&&i.started_at>=data.server_at-86400).length;
 const service=s?.sample?.checks?.['vmiss-'+protocol+'.service'],live=fresh(w);
 $('overview').replaceChildren(
  card(protocol.toUpperCase()+' 请求耗时',h?.state==='ok'?ms(h.ms):h?stateText[h.state]:'—',h?.state==='ok'?'ms':'',live?'独立探针 · '+clock(w.sample.captured_at)+' 采样':'探针数据过期或缺测',live&&h?.state==='ok'?'':'bad'),
  card(protocol.toUpperCase()+' 常规探测成功率',total?(100*stats.good/total).toFixed(1):'—',total?'%':'',total?stats.good+' 次成功 / '+stats.bad+' 次失败':'尚无常规样本'),
  card(protocol.toUpperCase()+' 确认异常',String(count),'次','近 24 小时新发生 · 已合并持续异常',count?'bad':''),
  card('VMISS 服务',fresh(s)?(service?.state==='ok'?'运行中':service?'异常':'待确认'):'待更新','','采样 '+fmt(s?.sample?.captured_at,true),fresh(s)&&service?.state==='ok'?'good':'bad')
 );
}
function statusRows(id,entry,keys){
 const box=$(id);box.replaceChildren();
 for(const k of keys){const c=entry?.sample?.checks?.[k],r=node('div',undefined,'status-row'),v=node('span',fresh(entry)?stateText[c?.state]||'缺测':'待更新','status-result '+(fresh(entry)&&c?.state==='ok'?'good':'muted'));if(c?.ms!=null)v.append(node('span',ms(c.ms)+' ms','status-extra'));r.append(node('span',names[k]),v);box.append(r);}
}
function details(data){
 const server=data.sources.vmiss,win=data.sources.windows;
 statusRows('server-metrics',server,['vmiss-'+protocol+'.service','egress.https','egress.dns']);statusRows('controls',win,['gateway.icmp','china.icmp','client.http']);
 $('server-freshness').textContent=server?.sample?(fresh(server)?'采样 ':'过期 ')+clock(server.sample.captured_at):'尚无采样';
 $('local-freshness').textContent=win?.sample?(fresh(win)?'采样 ':'过期 ')+clock(win.sample.captured_at):'尚无采样';
 const h=server?.sample?.host||{},r=node('div',undefined,'resource-row');r.append(node('span','CPU '+(h.cpu_percent??'—')+'%'),node('span','可用内存 '+bytes(h.memory?.available)));$('server-metrics').append(r);
 $('accounting').replaceChildren();
 for(const [key,label] of [['windows','电脑'],['vmiss','VMISS']]){const s=data.sources[key]?.sample;if(!s)continue;$('accounting').append(node('p',label+'：历史速率月外推（含估算） '+bytes(s.budget?.monthly_estimate_bytes)+'；上报有效载荷累计实计 '+bytes(s.budget?.measured_upload_payload_bytes)+'；观察 '+duration(s.budget?.observation_seconds||0)+'，调整频率后需重新积累。','small-note'));}
 const traffic=server?.sample?.traffic;$('accounting').append(node('p','VMISS 网卡累计（含 VPN）：接收 '+bytes(traffic?.rx_bytes)+' / 发送 '+bytes(traffic?.tx_bytes)+'。网卡数据不是监控专用流量，月用量估计不是商家账单。','small-note'));
}
function svgNode(tag,attrs,text){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));if(text!==undefined)n.textContent=text;return n;}
function charts(data){
 const now=data.server_at,range=historyRange(history.filter(s=>s.captured_at>=Math.floor((data.window_start||0)/300)*300),now),count=data.sources.windows?.coverage?.regular_samples;
 $('coverage').textContent=range.hasData?'图示 '+fmt(range.start,true)+' — '+clock(range.end)+' · 记录跨度 '+duration(range.end-range.start)+(count!=null?' · '+count+' 次常规采样':'')+'；未满 24 小时，按已有时段展开。':'尚无可用历史；缺测不计作丢包。';
 if(range.end-range.start>=85800)$('coverage').textContent='图示 '+fmt(range.start,true)+' — '+fmt(range.end,true)+' · 最近 24 小时；缺测留空。';
 $('charts').replaceChildren();
 for(const [index,metric] of VMISS_METRICS.entries()){
  const model=chartSeries(history,metric,range.start,range.end),stats=data.statistics.regular_checks.windows?.[metric],row=node('div',undefined,'chart-row');row.style.setProperty('--series',['#4b83ad','#4b8975','#af7748'][index]);
  const info=node('div',undefined,'chart-info'),title=node('h3',undefined,'chart-title');title.append(node('i',undefined,'chart-dot'),node('span',names[metric]));
  info.append(title,node('p',['往返延迟','管理端口建连耗时','完整请求耗时'][index],'chart-description'),node('p','均值 '+ms(stats?.mean_ms)+' ms · 失败 '+(stats?.bad??'—')+' 次','chart-stat'));
  const plot=node('div',undefined,'plot-wrap'),shell=node('div',undefined,'plot-shell'),yaxis=node('div',undefined,'y-axis');
  yaxis.append(node('span',String(model.ceiling)),node('span',String(model.ceiling/2)),node('span','0'));
  const svg=svgNode('svg',{viewBox:'0 0 660 100',preserveAspectRatio:'none',class:'latency-plot',role:'img','aria-label':names[metric]+' '+clock(range.start)+' 至 '+clock(range.end)+'，均值 '+ms(stats?.mean_ms)+' 毫秒'});
  for(const y of [7,47,87])svg.append(svgNode('line',{x1:0,x2:660,y1:y,y2:y,stroke:'#edece7','stroke-width':1,'vector-effect':'non-scaling-stroke'}));
  const x=t=>Math.max(0,Math.min(660,(t-range.start)/Math.max(300,range.end-range.start)*660)),y=v=>87-v/model.ceiling*80;
  for(const segment of model.segments){if(segment.length>1)svg.append(svgNode('polyline',{points:segment.map(p=>x(p.t).toFixed(2)+','+y(p.v.ms).toFixed(2)).join(' '),fill:'none',stroke:'var(--series)','stroke-width':1.8,'vector-effect':'non-scaling-stroke'}));
   for(const p of segment){const point=svgNode('circle',{cx:x(p.t),cy:y(p.v.ms),r:2,fill:'var(--series)'});point.append(svgNode('title',{},clock(p.t)+' · '+ms(p.v.ms)+' ms'));svg.append(point);}}
  for(const p of model.failed){const mark=svgNode('circle',{cx:x(p.t),cy:96,r:2.5,fill:'#bf694e'});mark.append(svgNode('title',{},clock(p.t)+' · '+(p.v.bad||1)+' 次失败'));svg.append(mark);}
  shell.append(yaxis,svg);plot.append(shell);const axis=node('div',undefined,'time-axis');for(const t of [range.start,(range.start+range.end)/2,range.end])axis.append(node('span',clock(t)));plot.append(axis);
  if(!model.points.length)plot.replaceChildren(node('p','此时段尚无样本','chart-empty'));
  row.append(info,plot);$('charts').append(row);
 }
}
function eventRow(i,now){
 const d=node('details',undefined,'event'),s=node('summary'),ongoing=!i.recovered_at&&!i.monitoring_ended_at;
 s.append(node('span',fmt(i.started_at,true),'event-time'),node('span',i.target==='vmiss.hy2'?'独立 HY2 探针异常（旧协议）':i.target==='vmiss.vless'?'独立 VLESS 探针异常':i.target==='vmiss-hy2.service'?'HY2 备用服务异常':'VLESS 服务异常','event-title'),node('span',i.monitoring_ended_at?'旧探针已停止':ongoing?'待确认恢复':'已恢复 · 约 '+duration(i.recovered_at-i.started_at),'event-state '+(ongoing?'bad':'good')));d.append(s);
 const body=node('div',undefined,'event-body');
 const texts=[['观测',i.confirmed_at?'连续三次探测失败，确认于 '+fmt(i.confirmed_at)+'。':'短暂探测异常，未达到连续三次失败。'],['恢复',i.recovered_at?fmt(i.recovered_at)+'，估计持续 '+duration(i.recovered_at-i.started_at)+'。':i.monitoring_ended_at?'探针已停止，未确认恢复；不继续累加故障时间。':'尚未确认；距首次失败 '+duration(now-i.started_at)+'，中间可能有缺测。'],['判断','原因待定位；独立探针异常不能直接等同于当前日常代理断连。详细日志保存在本机与服务器。']];
 for(const [label,text] of texts){const p=node('p');p.append(node('strong',label+' · '),node('span',text));body.append(p);}d.append(body);return d;
}
function incidents(list,now){
 const box=$('incident-list'),confirmed=list.filter(i=>i.confirmed_at),brief=list.filter(i=>!i.confirmed_at);box.replaceChildren();$('incident-count').textContent=confirmed.length+' 次确认 · '+brief.length+' 次短暂';
 if(!confirmed.length)box.append(node('p','此时段没有确认的 VMISS 异常。','empty-state'));
 confirmed.slice(0,5).forEach(i=>box.append(eventRow(i,now)));
 if(confirmed.length>5){const more=node('details',undefined,'more-events');more.append(node('summary','展开其余 '+(confirmed.length-5)+' 次确认异常'));confirmed.slice(5).forEach(i=>more.append(eventRow(i,now)));box.append(more);}
 if(brief.length){const more=node('details',undefined,'more-events');more.append(node('summary',brief.length+' 次短暂异常（未确认断链）'));brief.forEach(i=>more.append(eventRow(i,now)));box.append(more);}
}
async function load(){if(busy||document.hidden)return;busy=true;$('reload').disabled=true;try{
 const [latest,eventData]=await Promise.all([api('/latest'),api('/incidents')]);
 let historyFailed=false;if(Date.now()-lastHistory>=300000){try{const h=await api('/history');history=h.samples;lastHistory=Date.now();}catch{historyFailed=true;}}
 history=history.filter(s=>s.captured_at>latest.server_at-86400);const events=recentEvents(eventData.incidents,latest.server_at);
 protocol=latest.primary_protocol==='vless'?'vless':'hy2';VMISS_METRICS=['vmiss.icmp','vmiss.tcp','vmiss.'+protocol];
 $('cadence').textContent='每 '+(latest.sources.windows?.sample?.interval||300)+' 秒采样 · 北京时间';
 $('probe-note').textContent='电脑到 VMISS 的独立 '+protocol.toUpperCase()+' 探针；当前客户端：'+(latest.sources.windows?.sample?.client_node==='other_or_unknown'?'其他节点或未知':latest.sources.windows?.sample?.client_node||'未知')+'。新协议单独统计，旧 HY2 异常保留原标签。';
 overview(latest,events);details(latest);charts(latest);incidents(events,latest.server_at);
 const stale=!fresh(latest.sources.windows)||!fresh(latest.sources.vmiss);$('health').textContent=(stale?'部分采样已过期 · ':'已同步 · ')+clock(latest.server_at)+' · 摘要最多缓存 2 分钟'+(historyFailed?' · 历史读取失败，保留上次结果':'');lastLoad=Date.now();
 }catch{$('health').textContent='数据服务不可达；保留的结果已过期，请看采样时间。';}finally{busy=false;$('reload').disabled=false;}}
 $('reload').onclick=load;document.addEventListener('visibilitychange',()=>{if(!document.hidden&&Date.now()-lastLoad>60000)load();});setInterval(load,60000);load();
}
