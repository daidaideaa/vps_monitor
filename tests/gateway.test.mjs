import test from 'node:test';
import assert from 'node:assert/strict';
import gateway, {publicSnapshot, StatusSnapshot} from '../status-gateway/index.mjs';

const entries = [
  ['vmiss-jp-tky-tri-basic','app.vmiss.com'], ['zgocloud-tokyo-intel-starter','clients.zgovps.com'],
  ['rfchost-jp2-co-micro-lite','my.rfchost.com'], ['vps-tokyo-cloud-starter','vps.hosting'], ['vps-tokyo-cloud-essential','vps.hosting'],
];
const checkedAt = new Date(Date.now()-60000).toISOString();
const snapshot = () => ({schema_version:2, query_location:'japan-home-vps', published_at:checkedAt,
  SMTP_PASSWORD:'must-not-publish', products:entries.map(([id,host])=>({id,product_url:`https://${host}/`,
    status:'unknown',query_location:'japan-home-vps',cookie:'must-not-publish'}))});
function env() {
  const storage = new Map();
  const instance = new StatusSnapshot({storage:{get:key=>storage.get(key),put:(key,value)=>storage.set(key,value)}});
  return {PUBLISH_TOKEN:'test-only',STATUS:{idFromName:name=>name,get:()=>instance}};
}
const request = (body, token='test-only') => new Request('https://gateway.invalid/publish', {
  method:'PUT',headers:{Authorization:`Bearer ${token}`},body:JSON.stringify(body)});

test('only fixed products and public fields can be published',()=>{
  assert.ok(!JSON.stringify(publicSnapshot(snapshot())).includes('must-not-publish'));
  const invalid=snapshot();invalid.products[1]=invalid.products[0];assert.throws(()=>publicSnapshot(invalid));
  const badURL=snapshot();badURL.products[0].product_url='https://app.vmiss.com.evil.invalid/';assert.throws(()=>publicSnapshot(badURL));
  const future=snapshot();future.published_at=new Date(Date.now()+3600000).toISOString();assert.throws(()=>publicSnapshot(future));
});
test('clock correction can replace a previously stored future snapshot',async()=>{
  const future=snapshot();future.published_at=new Date(Date.now()+3600000).toISOString();
  const storage=new Map([['latest',future]]);
  const instance=new StatusSnapshot({storage:{get:key=>storage.get(key),put:(key,value)=>storage.set(key,value)}});
  const response=await instance.fetch(request(snapshot()));
  assert.equal(response.status,204);
  assert.equal(storage.get('latest').published_at,snapshot().published_at);
});
test('public read is separate from authenticated publishing, with no merchant requests',async()=>{
  const e=env();
  assert.equal((await gateway.fetch(new Request('https://gateway.invalid/status.json'),e)).status,503);
  assert.equal((await gateway.fetch(request(snapshot(),'wrong'),e)).status,401);
  assert.equal((await gateway.fetch(request(snapshot()),e)).status,204);
  const response=await gateway.fetch(new Request('https://gateway.invalid/status.json'),e);
  assert.equal(response.status,200);assert.equal(response.headers.get('Access-Control-Allow-Origin'),'*');
  assert.equal((await response.json()).products.length,5);
  const older=snapshot();older.published_at=new Date(Date.parse(checkedAt)-60000).toISOString();
  assert.equal((await gateway.fetch(request(older),e)).status,409);
});
