// This gateway never contacts stores or sends mail. Japan publishes a public snapshot.
const headers = {'Content-Type': 'application/json; charset=utf-8', 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'};
const targets = new Map([
  ['vmiss-jp-tky-tri-basic', 'app.vmiss.com'], ['zgocloud-tokyo-intel-starter', 'clients.zgovps.com'],
  ['rfchost-jp2-co-micro-lite', 'my.rfchost.com'], ['vps-tokyo-cloud-starter', 'vps.hosting'], ['vps-tokyo-cloud-essential', 'vps.hosting'],
]);
const fields = ['id', 'provider', 'product_name', 'product_url', 'status', 'last_confirmed', 'last_checked', 'unknown_count', 'stock', 'explanation', 'check_interval_seconds', 'query_location', 'check_interval_min_seconds', 'check_interval_max_seconds', 'last_available_at', 'unavailable_since'];
export function publicSnapshot(data) {
  if (data?.schema_version !== 2 || data.query_location !== 'japan-home-vps' || !Array.isArray(data.products) || data.products.length !== 5 || !Number.isFinite(Date.parse(data.published_at)) || Date.parse(data.published_at) > Date.now()+120000) throw Error('Invalid snapshot');
  const ids = new Set();
  const products = data.products.map(p => {
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
      // A corrected server clock must be able to replace an invalid future snapshot.
      if (old && Date.parse(old.published_at) <= Date.now()+120000 && Date.parse(data.published_at) < Date.parse(old.published_at)) return new Response('Older snapshot', {status:409});
      await this.state.storage.put('latest',data);
      return new Response(null,{status:204});
    }
    const data = await this.state.storage.get('latest');
    return new Response(JSON.stringify(data || {error:'Japan has not published a snapshot'}),{status:data?200:503,headers});
  }
}
export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
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
