const {test}=require('node:test');
const assert=require('node:assert/strict');
const {historyRange,chartSeries,recentEvents,chartStats}=require('../assets/network.js');
const sample=(t,state='ok',metric='vmiss.hy2')=>({source:'windows',captured_at:t,interval:300,checks:{[metric]:{state,ms:state==='ok'?200:null}}});
test('chart trims unobserved leading hours, preserves actual timestamps',()=>{
 const range=historyRange([sample(100000),sample(100300),sample(1),sample(100900,'ok','home.hy2')],101000);
 assert.deepEqual(range,{start:100000,end:101000,hasData:true});
});
test('failed and missing buckets split lines; isolated successes remain visible',()=>{
 const chart=chartSeries([sample(1000),sample(1300,'fail'),sample(1600),sample(2500),sample(2800)],'vmiss.hy2',1000,3000);
 assert.deepEqual(chart.segments.map(s=>s.map(p=>p.t)),[[1000],[1600],[2500,2800]]);
 assert.equal(chart.failed.length,1);assert.equal(chart.ceiling,200);
});
test('series never mixes other targets or ICMP with HY2',()=>{
 const chart=chartSeries([sample(1000),sample(1300,'ok','vmiss.icmp'),sample(1600,'fail','home.hy2')],'vmiss.hy2',1000,2000);
 assert.equal(chart.points.length,1);assert.equal(chart.failed.length,0);
});
test('incident display excludes home, current client, old recovered and future events',()=>{
 const events=['home.hy2','client.http','vmiss.hy2'].map(target=>({source:'windows',target,started_at:95000,confirmed_at:95010}));
 events.push({source:'windows',target:'vmiss.hy2',started_at:1,recovered_at:2},{source:'windows',target:'vmiss.hy2',started_at:200000});
 assert.equal(recentEvents(events,100000).length,1);
});
test('chart labels use plotted buckets rather than newer independently cached latest statistics',()=>{
 const points=[{v:{state:'ok',ms:100,good:2,bad:0}},{v:{state:'ok',ms:400,good:1,bad:0}},{v:{state:'fail',ms:null,good:1,bad:2}}];
 assert.deepEqual(chartStats(points),{mean_ms:200,bad:2});
 assert.deepEqual(chartStats([]),{mean_ms:null,bad:0});
});
