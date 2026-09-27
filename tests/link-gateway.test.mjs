import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import worker,{ProbeStore,authorized,validateBatch} from '../link-gateway/index.mjs';
import {correlate} from '../link-gateway/diagnose.mjs';
function store() {
  const db=new DatabaseSync(':memory:');let alarm=null;
  return new ProbeStore({storage:{sql:{exec(q,...args){const s=db.prepare(q);return s.columns().length?s.all(...args):(s.run(...args),[]);}},
    transactionSync(fn){db.exec('BEGIN');try{fn();db.exec('COMMIT');}catch(e){db.exec('ROLLBACK');throw e;}},
    getAlarm:async()=>alarm,setAlarm:async a=>{alarm=a;}}});
}
const now=()=>Date.now()/1000;
function sample(seq,at=now()) {return {source:'windows',boot_id:'test-boot',seq,captured_at:at,monotonic_ms:seq*1000,
  checks:{'vmiss.hy2':{state:'ok',ms:20,new:true},'gateway.icmp':{state:'error'}}};}
const ingest=(s,samples,incidents=[])=>s.fetch(new Request('https://internal/ingest',{method:'POST',body:JSON.stringify({source:'windows',samples,incidents})}));

test('paused monitoring makes no database calls and never reschedules alarms',async()=>{
  const forbidden=()=>{throw Error('storage must remain untouched while paused');};
  const env={MONITORING_PAUSED:'true',PROBES:{get:forbidden,idFromName:forbidden}};
  assert.equal((await worker.fetch(new Request('https://x/ingest',{method:'POST'}),env)).status,503);
  assert.equal((await worker.fetch(new Request('https://x/network/api/evidence'),env)).status,401);
  const object=new ProbeStore({storage:{sql:{exec:forbidden},setAlarm:forbidden}},env);
  assert.equal((await object.fetch(new Request('https://internal/ingest',{method:'POST'}))).status,503);
  await object.alarm();
});
test('all reads, evidence and alternate preview hosts fail closed without Access',async()=>{
  for(const host of ['main.workers.dev','preview.main.workers.dev'])for(const path of ['/','/network','/network/api/latest','/network/api/history','/network/api/evidence','/network/api/incidents'])
    assert.equal((await worker.fetch(new Request('https://'+host+path),{})).status,401);
});
test('source token cannot submit another source and unauthenticated ingress fails',async()=>{
  const e={PROBE_TOKENS:JSON.stringify({windows:'test-secret'})};
  const req=(token,source,data)=>new Request('https://x/ingest',{method:'POST',headers:{Authorization:'Bearer '+token,'X-Probe-Source':source},body:JSON.stringify(data)});
  assert.equal((await worker.fetch(req('wrong','windows',{source:'windows',samples:[]}),e)).status,401);
  assert.equal((await worker.fetch(req('test-secret','windows',{source:'home',samples:[]}),e)).status,400);
  assert.throws(()=>validateBatch({source:'windows',samples:[{...sample(0),captured_at:now()+3600}]},'windows'));
});
test('JWT signature, issuer, audience, email and expiry are verified',async()=>{
  const keys=await crypto.subtle.generateKey({name:'RSASSA-PKCS1-v1_5',modulusLength:2048,publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'},true,['sign','verify']);
  const jwk={...await crypto.subtle.exportKey('jwk',keys.publicKey),kid:'test-1'};
  const env={ACCESS_TEAM_DOMAIN:'test.cloudflareaccess.com',ACCESS_AUD:'aud',ALLOWED_EMAIL:'owner@example.com'};
  const fetcher=async()=>Response.json({keys:[jwk]});
  async function req(changes={},badSignature=false){
    const b=x=>Buffer.from(JSON.stringify(x)).toString('base64url');
    const content=b({alg:'RS256',kid:'test-1'})+'.'+b({iss:'https://test.cloudflareaccess.com',aud:['aud'],email:'owner@example.com',exp:now()+60,...changes});
    const signature=Buffer.from(await crypto.subtle.sign('RSASSA-PKCS1-v1_5',keys.privateKey,new TextEncoder().encode(content))).toString('base64url');
    return new Request('https://test/network',{headers:{'Cf-Access-Jwt-Assertion':content+'.'+(badSignature?'AAAA':signature)}});
  }
  assert.equal(await authorized(await req(),env,fetcher),true);
  for(const changed of [{email:'other@example.com'},{aud:['wrong']},{exp:now()-1},{iss:'https://evil.example'}])
    assert.equal(await authorized(await req(changed),env,fetcher),false);
  assert.equal(await authorized(await req({},true),env,fetcher),false);
});
test('SQLite dedup and replay preserve time; latest does not regress; missing is not loss',async()=>{
  const s=store(),a=sample(2),b=sample(1,a.captured_at-100);
  await ingest(s,[a,b,a]);
  const latest=await (await s.fetch(new Request('https://i/latest'))).json();
  assert.equal(latest.sample.seq,2);
  assert.equal(latest.sample.captured_at,a.captured_at);
  const history=await (await s.fetch(new Request('https://i/history'))).json();
  assert.equal(history.minutes.reduce((n,r)=>n+r.n,0),2);
  assert.equal(history.minutes.some(r=>r.metric==='gateway.icmp'),false);
});
test('incident freezes before and after samples, including offline backfill',async()=>{
  const s=store(),t=now(),i={id:'incident-test',source:'windows',started_at:t-10,end_at:t+290};
  await ingest(s,[sample(0,t-60),sample(1,t)],[i]);await ingest(s,[sample(2,t-30)]);
  const data=await (await s.fetch(new Request('https://i/evidence?id=incident-test'))).json();
  assert.equal(data.samples.length,3);assert.equal(data.incident.id,i.id);
});
test('server evidence request is durable and acknowledged by the source incident',async()=>{
  const s=store(),i={id:'incident-test-vmiss',source:'vmiss',started_at:now()-10,end_at:now()+290};
  await s.fetch(new Request('https://i/collect-evidence',{method:'POST',body:JSON.stringify(i)}));
  assert.equal((await (await ingest(s,[])).json()).requests.length,1);
  assert.equal((await (await ingest(s,[],[i])).json()).requests.length,0);
});
test('cross-source diagnosis requires temporally overlapping independent success',()=>{
  const i={id:'x',target:'vmiss.hy2',report:{facts:[],inferences:[],missing:[],events:[{metric:'vmiss.hy2',state:'fail',at:100}]}};
  const p={source:'home',correlation_id:'x',report:{events:[{metric:'vmiss.hy2',state:'ok',at:110}]}};
  assert.equal(correlate(i,[p]).inferences.length,1);
  p.report.events[0].at=200;assert.equal(correlate(i,[p]).inferences.length,0);
});
