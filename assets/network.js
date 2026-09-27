'use strict';
const API='https://38.47.125.205/monitor';
let token=null,challenge=null,cursor=0,history=[],busy=false,lastLoad=0;
const $=id=>document.getElementById(id),fmt=t=>t?new Date(t*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'尚无记录';
const node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
const names={windows:'你的电脑',vmiss:'VMISS 服务端',home:'日本家宽'};
const bytes=n=>Number.isFinite(n)?(n/1048576).toFixed(1)+' MiB':'—';
async function api(path,body){
 const r=await fetch(API+path,{method:body===undefined?'GET':'POST',headers:{...(token?{Authorization:'Bearer '+token}:{}),...(body!==undefined?{'Content-Type':'application/json'}:{})},body:body!==undefined?JSON.stringify(body):undefined,cache:'no-store',signal:AbortSignal.timeout(12000)});
 if(r.status===401){token=null;$('private').hidden=true;$('login').hidden=false;$('logout').hidden=true;throw Error('请重新登录');}
 if(!r.ok)throw Error(r.status===429?'发送过于频繁，请稍后再试':r.status===400?'验证码无效、过期或尝试次数已用完':'数据服务不可达（HTTP '+r.status+'）');
 return r.json();
}
$('request').onclick=async()=>{const b=$('request');b.disabled=true;try{const r=await api('/auth/request',{});challenge=r.challenge;$('login-status').textContent=r.message;$('code').focus();}catch(e){$('login-status').textContent=e.message;}finally{setTimeout(()=>b.disabled=false,60000);}};
$('verify').onsubmit=async e=>{e.preventDefault();if(!challenge){$('login-status').textContent='请先发送验证码。';return;}try{const r=await api('/auth/verify',{challenge,code:$('code').value});token=r.token;$('code').value='';$('login').hidden=true;$('private').hidden=false;$('logout').hidden=false;await load();}catch(err){$('login-status').textContent=err.message;}};
$('logout').onclick=async()=>{try{await api('/auth/logout',{});}finally{token=null;history=[];cursor=0;$('sources').replaceChildren();$('charts').replaceChildren();$('incident-list').replaceChildren();$('reports').replaceChildren();$('private').hidden=true;$('login').hidden=false;$('logout').hidden=true;}};
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
   for(const text of [metric,({ok:'成功',fail:'失败',error:'待定位',unknown:'缺测'})[v.state]||v.state,v.ms==null?'—':v.ms+' ms',stat?stat.good+' / '+stat.bad:'—'])row.append(node('td',text));table.append(row);
  }card.append(table);
  if(id==='windows')card.append(node('p','实际客户端：'+(s.client?.profile||'未知')+' · 选择 '+JSON.stringify(s.client?.groups||{})+' · TUN '+(s.client?.tun?.enable?'开启':'关闭或未知'),'fact-line'));
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
 for(const i of list){const d=node('details',undefined,'event');d.append(node('summary',fmt(i.started_at)+' · '+i.source+'/'+i.target+' · '+(i.confirmed_at?'确认异常':'单次异常，未确认断链')+' · '+(i.recovered_at?'恢复于 '+fmt(i.recovered_at):'未确认恢复')));
  const r=i.report||{};d.append(node('pre','已确认事实\n'+(r.facts||[]).join('\n')+'\n\n推断\n'+(r.inferences||[]).join('\n')+'\n\n缺失证据\n'+(r.missing||[]).join('\n')));
  const b=node('button','下载诊断摘要与现存采样');b.onclick=async()=>{try{const data=await api('/api/evidence?id='+encodeURIComponent(i.id));const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=node('a');a.href=url;a.download='incident-'+i.id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){$('health').textContent=e.message;}};d.append(b);$('incident-list').append(d);}
}
async function load(){if(busy||!token||document.hidden)return;busy=true;try{
 const [latest,events,reports]=await Promise.all([api('/api/latest'),api('/api/incidents?since='+Math.floor(Date.now()/1000-30*86400)),api('/api/reports')]);
 for(let n=0;n<5;n++){const h=await api('/api/history?since='+Math.floor(Date.now()/1000-86400)+'&cursor='+cursor);history.push(...h.samples);cursor=h.cursor;if(!h.has_more)break;}
 history=history.filter(s=>s.captured_at>Date.now()/1000-86400);
 sourceCards(latest);charts();incidents(events.incidents);$('reports').replaceChildren();for(const r of reports.reports){const d=node('details',undefined,'event');d.append(node('summary',r.day+' · '+(r.sent_at?'邮件已提交':'等待发送')),node('pre',r.body));$('reports').append(d);}if(!reports.reports.length)$('reports').textContent='首份日报将在次日 09:00 生成。';
 $('health').textContent='同步于 '+fmt(latest.server_at)+' · 常规 120 秒采样，页面每 60 秒读取';lastLoad=Date.now();
 }catch(e){$('health').textContent=e.message+'；已有结果已过期，请看原采样时间。';}finally{busy=false;}}
$('reload').onclick=load;document.addEventListener('visibilitychange',()=>{if(!document.hidden&&Date.now()-lastLoad>60000)load();});setInterval(load,60000);
