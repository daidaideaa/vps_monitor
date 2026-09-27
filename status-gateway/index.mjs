// This gateway never contacts stores or sends mail. Japan publishes a public snapshot.
const headers = {'Content-Type': 'application/json; charset=utf-8', 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'};
const targets = new Map([
  ['vmiss-jp-tky-tri-basic', 'app.vmiss.com'], ['zgocloud-tokyo-intel-starter', 'clients.zgovps.com'],
  ['rfchost-jp2-co-micro-lite', 'my.rfchost.com'],
]);
// Accept an old publisher during deployment, but never expose retired products.
const retired = new Set(['vps-tokyo-cloud-starter', 'vps-tokyo-cloud-essential']);
const fields = ['id', 'provider', 'product_name', 'product_url', 'status', 'last_confirmed', 'last_checked', 'unknown_count', 'stock', 'explanation', 'check_interval_seconds', 'query_location', 'check_interval_min_seconds', 'check_interval_max_seconds', 'last_available_at', 'unavailable_since'];
const currentTargets = new Map([
  ['vmiss-jp-tky-tri-basic','app.vmiss.com'],['greencloud-tokyo-premium-mini','greencloudvps.com'],
  ['dmit-tyo-pro-tiny','www.dmit.io'],['gomami-jpn-pulse-nano','gomami.io'],
]);
function price(p) {
  if(!p || !/^\d{1,8}(\.\d{1,3})?$/.test(p.amount) || !['USD','CAD','币种待确认'].includes(p.currency) ||
    !['月','季','半年','年','2年'].includes(p.cycle)) throw Error('Invalid price');
  return {amount:p.amount,currency:p.currency,cycle:p.cycle};
}
export function versionThree(data) {
  if(!Array.isArray(data.products) || data.products.length!==4 || !Number.isFinite(Date.parse(data.published_at)) ||
     Date.parse(data.published_at)>Date.now()+120000) throw Error('Invalid snapshot');
  const seen=new Set();
  const products=data.products.map(p=>{
    if(!currentTargets.has(p.id)||seen.has(p.id)||!['available','unavailable','unknown'].includes(p.status))throw Error('Invalid product');
    seen.add(p.id);
    const url=new URL(p.product_url);
    if(url.protocol!=='https:'||url.host!==currentTargets.get(p.id)||url.username||url.password)throw Error('Invalid URL');
    if(p.source_url && !/^https:\/\/t\.me\/(hostmonit|vps_spiders|gcpcn)\/[0-9]+$/.test(p.source_url))throw Error('Invalid source');
    if(p.event_at!==null && (!Number.isFinite(p.event_at)||p.event_at>Date.now()/1000+120))throw Error('Invalid observation time');
    const text=(v,max=120)=>typeof v==='string'?v.slice(0,max):null;
    let coupon=null;
    if(p.coupon) {
      const c=p.coupon;
      if(!/^[A-Za-z0-9][A-Za-z0-9_%.-]{1,90}$/.test(c.code))throw Error('Invalid coupon');
      coupon={code:c.code,cycle:text(c.cycle,20),recurring:c.recurring===true,expires_at:Number.isFinite(c.expires_at)?c.expires_at:null,
        terms:text(c.terms,240),validity:c.validity==='expired'?'expired':'source_reported',
        discounted:c.discounted?price(c.discounted):null,observed_at:Number.isFinite(c.observed_at)?c.observed_at:null,
        source_url:/^https:\/\/t\.me\/(hostmonit|vps_spiders|gcpcn)\/[0-9]+$/.test(c.source_url||'')?c.source_url:null};
    }
    return {id:p.id,provider:text(p.provider),product_name:text(p.product_name),product_url:url.href,status:p.status,
      stock:Number.isSafeInteger(p.stock)&&p.stock>=0?p.stock:null,event_at:p.event_at,source_url:p.source_url||null,
      source:text(p.source,32),last_confirmed:['available','unavailable'].includes(p.last_confirmed)?p.last_confirmed:'unknown',
      stale:p.stale===true,coupon,prices:Array.isArray(p.prices)?p.prices.slice(0,8).map(price):[]};
  });
  return {schema_version:3,published_at:data.published_at,products,
    collector:{state:['connected','disconnected','awaiting_authorization'].includes(data.collector?.state)?data.collector.state:'unknown'}};
}
export function publicSnapshot(data) {
  if(data?.schema_version===3)return versionThree(data);
  if (data?.schema_version !== 2 || data.query_location !== 'japan-home-vps' || !Array.isArray(data.products) || data.products.length > targets.size + retired.size || !Number.isFinite(Date.parse(data.published_at)) || Date.parse(data.published_at) > Date.now()+120000) throw Error('Invalid snapshot');
  const active = data.products.filter(p => !retired.has(p?.id));
  if (active.length !== targets.size) throw Error('Missing active product');
  const ids = new Set();
  const products = active.map(p => {
    if (!targets.has(p.id) || ids.has(p.id) || !['available','unavailable','unknown'].includes(p.status)) throw Error('Invalid product');
    const url = new URL(p.product_url);
    if (url.protocol !== 'https:' || url.host !== targets.get(p.id) || url.username || url.password) throw Error('Invalid store URL');
    ids.add(p.id);
    return Object.fromEntries(fields.filter(k => Object.hasOwn(p,k)).map(k => [k,p[k]]));
  });
  return {schema_version:2, query_location:'japan-home-vps', products, published_at:data.published_at};
}
export class StatusSnapshot {
  constructor(state) { this.state = state; }
  async fetch(request) {
    if (request.method === 'PUT') {
      const data = await request.json();
      const old = await this.state.storage.get('latest');
      if(old?.schema_version===3 && data.schema_version!==3)return new Response('Legacy publisher retired',{status:409});
      // A corrected server clock must be able to replace an invalid future snapshot.
      if (old && Date.parse(old.published_at) <= Date.now()+120000 && Date.parse(data.published_at) < Date.parse(old.published_at)) return new Response('Older snapshot', {status:409});
      await this.state.storage.put('latest',data);
      return new Response(null,{status:204});
    }
    const stored = await this.state.storage.get('latest');
    let data;
    try { data = stored && publicSnapshot(stored); } catch { data = null; }
    return new Response(JSON.stringify(data || {error:'Japan has not published a snapshot'}),{status:data?200:503,headers});
  }
}
export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (env.STORAGE_RETIRED === 'true') {
      if(request.method==='GET' && path==='/status.json')return Response.redirect('https://vps-monitor.daidaidefish.workers.dev/api/status',302);
      return new Response('Monitoring storage moved to the owner VPS',{status:410});
    }
    if (request.method === 'GET' && path === '/status.json') return env.STATUS.get(env.STATUS.idFromName('latest')).fetch(request);
    if (request.method !== 'PUT' || path !== '/publish') return new Response('Not found',{status:404});
    if (!env.PUBLISH_TOKEN || request.headers.get('Authorization') !== `Bearer ${env.PUBLISH_TOKEN}`) return new Response('Unauthorized',{status:401});
    if (Number(request.headers.get('Content-Length')) > 32768) return new Response('Too large',{status:413});
    try {
      const body = await request.text();
      if (body.length > 32768) return new Response('Too large',{status:413});
      const data = publicSnapshot(JSON.parse(body));
      return env.STATUS.get(env.STATUS.idFromName('latest')).fetch(new Request('https://snapshot.internal/',{method:'PUT',body:JSON.stringify(data)}));
    } catch { return new Response('Invalid public snapshot',{status:400}); }
  }
};
