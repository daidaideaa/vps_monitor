import test from 'node:test';
import assert from 'node:assert/strict';
import {publicRead} from '../public-edge.mjs';
test('fixed public endpoints cache reads and never forward private paths or origin failures',async()=>{
 const map=new Map(),cache={match:async r=>map.get(r.url)?.clone(),put:async(r,v)=>map.set(r.url,v)};
 const tasks=[],ctx={waitUntil:p=>tasks.push(p)},env={MONITOR_ORIGIN:'https://private-origin.invalid/monitor'};let calls=0;
 const fetcher=async url=>{calls++;assert.equal(url,env.MONITOR_ORIGIN+'/public/latest');return Response.json({sources:{}});};
 for(let i=0;i<2;i++){
  const r=await publicRead(new Request('https://edge.invalid/api/network/latest'),env,ctx,cache,fetcher);
  assert.equal(r.status,200);await Promise.all(tasks);
 }
 assert.equal(calls,1);
 for(const path of ['/api/network/evidence','/api/network/latest?source=private'])
  assert.notEqual((await publicRead(new Request('https://edge.invalid'+path),env,ctx,cache,fetcher)).status,200);
 const fail=await publicRead(new Request('https://edge.invalid/api/network/history'),env,ctx,cache,async()=>new Response('SECRET_IP',{status:500}));
 assert.equal(fail.status,503);assert.ok(!(await fail.text()).includes('SECRET'));
});
