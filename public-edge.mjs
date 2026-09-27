// Only fixed, sanitized read endpoints. No Durable Objects, KV, cron or paid bindings.
const routes = new Map([
  ['/api/status', ['/status.json', 60]],
  ['/api/network/latest', ['/public/latest', 120]],
  ['/api/network/history', ['/public/history', 300]],
  ['/api/network/incidents', ['/public/incidents', 120]],
]);
const headers = {'Content-Type':'application/json; charset=utf-8','Access-Control-Allow-Origin':'*',
  'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'};
export async function publicRead(request, env, ctx, cache = caches.default, fetcher = fetch) {
  const url = new URL(request.url), route = routes.get(url.pathname);
  if (!route) return Response.json({error:'not_found'},{status:404,headers});
  if (request.method!=='GET' || url.search) return Response.json({error:'invalid_request'},{status:400,headers});
  const key = new Request(url.origin+url.pathname);
  const found = await cache.match(key);
  if (found) return found;
  try {
    if (!env.MONITOR_ORIGIN) throw Error('not_configured');
    const upstream = await fetcher(env.MONITOR_ORIGIN+route[0], {redirect:'error',signal:AbortSignal.timeout(8000),
      headers:{'Accept':'application/json'}});
    if (!upstream.ok) throw Error('upstream_status_'+upstream.status);
    const body = await upstream.text();
    if (body.length>200000) throw Error('response_limit');
    const response = new Response(body,{headers:{...headers,'Cache-Control':'public, max-age='+route[1]}});
    ctx.waitUntil(cache.put(key,response.clone()));
    return response;
  } catch (error) {
    console.warn('Public origin read failed', /^upstream_status_\d+$/.test(error.message)?error.message:error.name);
    // Never forward origin headers, addresses, redirects or error bodies.
    const response = Response.json({error:'data_service_unavailable'},{status:503,headers:{...headers,'Cache-Control':'public, max-age=30'}});
    return response;
  }
}
export default {fetch(request,env,ctx) {
  return new URL(request.url).pathname.startsWith('/api/') ? publicRead(request,env,ctx) : env.ASSETS.fetch(request);
}};
