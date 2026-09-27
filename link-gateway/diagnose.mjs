export function correlate(incident, peers) {
  const report=structuredClone(incident.report||{facts:[],inferences:[],missing:[]});
  const target=incident.target?.split('.')[0];
  if(!['vmiss','home'].includes(target))return report;
  const failed=(report.events||[]).filter(e=>e.metric===target+'.hy2'&&e.state==='fail');
  const other=peers.filter(p=>p.correlation_id===incident.id);
  for(const peer of other) {
    if((peer.report?.events||[]).some(e=>e.metric===target+'.hy2'&&e.state==='ok'&&failed.some(f=>Math.abs(f.at-e.at)<=30))) {
      report.facts.push(peer.source+' 在本机失败前后 30 秒内成功完成了同目标 HY2 请求。');
      report.inferences.push('优先检查本机或本机到 '+target+' 的路径；其他观测点同期可用。仍需两端 UDP 证据确定方向。');
    }
  }
  if(other.length)report.facts.push('已关联 '+other.map(p=>p.source).join('、')+' 的服务端/外部观测证据。');
  return {...report,facts:[...new Set(report.facts)],inferences:[...new Set(report.inferences)]};
}
