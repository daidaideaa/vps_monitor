'use strict';
const API='https://vps-monitor.daidaidefish.workers.dev/api/network';
let history=[],busy=false,lastLoad=0,lastHistory=0;
const $=id=>document.getElementById(id),fmt=t=>t?new Date(t*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'尚无记录';
const node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
const names={windows:'你的电脑',vmiss:'VMISS 服务端',home:'日本家宽'};
const metricName=k=>({'gateway.icmp':'本地网关','china.icmp':'国内对照','home.icmp':'备用 VPS · ICMP','home.tcp':'备用 VPS · TCP','home.hy2':'备用 VPS · HY2','vmiss.icmp':'主 VPS · ICMP','vmiss.tcp':'主 VPS · TCP','vmiss.hy2':'主 VPS · HY2','client.http':'当前日常代理 · HTTP','egress.https':'服务器 HTTPS 出口','egress.dns':'服务器 DNS','vmiss-hy2.service':'HY2 服务','manual.client':'手动故障标记'})[k]||'其他检查';
const bytes=n=>Number.isFinite(n)?(n/1048576).toFixed(1)+' MiB':'—';
async function api(path){
 const r=await fetch(API+path,{signal:AbortSignal.timeout(12000)});
 if(!r.ok)throw Error('数据服务不可达（HTTP '+r.status+'）');
 return r.json();
}
function sourceCards(data){
 $('sources').replaceChildren();
 for(const [id,entry] of Object.entries(data.sources)){
  const card=node('article',undefined,'panel');card.append(node('h2',names[id]||id));
  if(!entry.sample){card.append(node('p','尚无样本；不计丢包。'));$('sources').append(card);continue;}
  const s=entry.sample;card.append(node('p',(entry.state==='fresh'?'最近采样 ':'数据已过期 ')+fmt(s.captured_at)+' · '+entry.age_seconds+' 秒前','fact-line '+(entry.state==='fresh'?'good':'bad')));
  card.append(node('p','近 24 小时确认异常 '+(data.statistics.confirmed_incidents[id]||0)+' 次 · 上传 '+(s.upload?.state||'未知'),'fact-line'));
  const table=node('table',undefined,'metrics');const head=node('tr');for(const t of ['检查','最近结果','耗时','常规成功/失败'])head.append(node('th',t));table.append(head);
  for(const [metric,v] of Object.entries(s.checks||{})){
   const row=node('tr'),stat=data.statistics.regular_checks[id]?.[metric];
   for(const text of [metricName(metric),({ok:'成功',fail:'失败',error:'待定位',unknown:'缺测'})[v.state]||v.state,v.ms==null?'—':v.ms+' ms',stat?stat.good+' / '+stat.bad:'—'])row.append(node('td',text));table.append(row);
  }card.append(table);
  if(id==='windows')card.append(node('p','独立 HY2 探针与当前日常代理分别观测。','fact-line'));
  const h=s.host||{},mem=h.memory||{};
  card.append(node('p','CPU '+(h.cpu_percent??'—')+'% · 可用内存 '+bytes(mem.MemAvailable??mem.available)+' · 负载 '+(h.load?.[0]?.toFixed(2)??'—'),'fact-line'));
  card.append(node('p','上线以来网卡增量（含 VPN）：接收 '+bytes(s.traffic?.rx_bytes)+' / 发送 '+bytes(s.traffic?.tx_bytes),'fact-line'));
  if(s.budget)card.append(node('p','监控月用量估计 '+bytes(s.budget.monthly_estimate_bytes)+'；其中上报有效载荷实计 '+bytes(s.budget.measured_upload_payload_bytes)+'。探测及保活含估算。','fact-line'));
  $('sources').append(card);
 }
}
function charts(){
 $('charts').replaceChildren();const now=Date.now()/1000,start=now-86400,colors=['#387bce','#13967c','#bc7e32'];
 for(const target of ['vmiss','home']){
  const block=node('div');block.append(node('h3','电脑 → '+names[target]));
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 900 180');svg.setAttribute('class','chart');svg.setAttribute('role','img');svg.setAttribute('aria-label','最近二十四小时延迟曲线');
  const series=['icmp','tcp','hy2'].map(type=>history.filter(s=>s.source==='windows'&&s.checks?.[target+'.'+type]?.new!==false).map(s=>({t:s.captured_at,v:s.checks[target+'.'+type],interval:s.interval||120})).sort((a,b)=>a.t-b.t));
  const max=Math.max(100,...series.flat().filter(p=>p.v?.state==='ok'&&Number.isFinite(p.v.ms)).map(p=>p.v.ms));
  series.forEach((points,k)=>{let segment=[];const flush=()=>{if(segment.length){const line=document.createElementNS(svg.namespaceURI,'polyline');line.setAttribute('points',segment.join(' '));line.setAttribute('fill','none');line.setAttribute('stroke',colors[k]);line.setAttribute('stroke-width','2');svg.append(line);segment=[];}};let last=null;
   for(const p of points){if(last&&p.t-last.t>Math.max(p.interval,last.interval)*2.5)flush();if(p.v?.state!=='ok'||!Number.isFinite(p.v.ms)){flush();last=p;continue;}segment.push((30+(p.t-start)/86400*850).toFixed(1)+','+(150-p.v.ms/max*130).toFixed(1));last=p;}flush();});
  const label=document.createElementNS(svg.namespaceURI,'text');label.setAttribute('x','5');label.setAttribute('y','15');label.setAttribute('fill','#777');label.setAttribute('font-size','11');label.textContent=Math.ceil(max)+' ms';svg.append(label);
  block.append(svg,node('p','蓝：ICMP 往返 · 绿：TCP 建连 · 棕：HY2 请求；左端 24 小时前，右端现在。','legend'));$('charts').append(block);
 }
}
function incidents(list){$('incident-list').replaceChildren();if(!list.length)$('incident-list').append(node('p','暂无故障记录。'));
 for(const i of list){const d=node('details',undefined,'event');d.append(node('summary',fmt(i.started_at)+' · '+names[i.source]+' / '+metricName(i.target)+' · '+(i.confirmed_at?'确认异常':'单次异常，未确认断链')+' · '+(i.recovered_at?'恢复于 '+fmt(i.recovered_at):'未确认恢复')));
  const r=i.report||{};d.append(node('pre','已确认事实\n'+(r.facts||[]).join('\n')+'\n\n推断\n'+(r.inferences||[]).join('\n')+'\n\n缺失证据\n'+(r.missing||[]).join('\n')));
  $('incident-list').append(d);}
}
async function load(){if(busy||document.hidden)return;busy=true;try{
 const [latest,events]=await Promise.all([api('/latest'),api('/incidents')]);
 if(Date.now()-lastHistory>=300000){const h=await api('/history');history=h.samples;lastHistory=Date.now();}
 history=history.filter(s=>s.captured_at>Date.now()/1000-86400);
 sourceCards(latest);charts();incidents(events.incidents);
 $('health').textContent='同步于 '+fmt(latest.server_at)+' · 常规 120 秒采样 · 摘要最多缓存 120 秒';lastLoad=Date.now();
 }catch(e){$('health').textContent=e.message+'；已有结果已过期，请看原采样时间。';}finally{busy=false;}}
$('reload').onclick=load;document.addEventListener('visibilitychange',()=>{if(!document.hidden&&Date.now()-lastLoad>60000)load();});setInterval(load,60000);load();
