import { parseZgoCloud, parseRfchost } from './parsers.js';

export const STATE_KEY = 'tokyo-vps-status';
export const TARGETS = [
  {
    id: 'zgocloud-tokyo-intel-starter', provider: 'ZgoCloud',
    product_name: 'Tokyo Intel VPS · Starter',
    product_url: 'https://clients.zgovps.com/index.php?/cart/tokyo-intel-vps/',
  },
  {
    id: 'rfchost-jp2-co-micro-lite', provider: 'RFCHOST',
    product_name: 'JP2-CO-Micro-Lite',
    product_url: 'https://my.rfchost.com/index.php?currency=8&rp=/store/jp-2-china-optimization-network-lite',
  },
];
const HEADERS = {
  'Content-Type': 'application/json; charset=utf-8',
  'Cache-Control': 'no-store',
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
  'X-Content-Type-Options': 'nosniff',
};
const STATES = new Set(['available', 'unavailable', 'unknown']);
const MAX_HTML_BYTES = 1024 * 1024;

function initialProduct(target, explanation = '等待首次定时检查') {
  return { ...target, status: 'unknown', last_confirmed: 'unknown', last_checked: null,
    unknown_count: 0, stock: null, explanation, check_interval_seconds: 180 };
}

function publicSnapshot(stored, explanation) {
  // 白名单重建响应，KV 中额外的调试信息也不会被直接公开。
  return {
    schema_version: 1,
    products: TARGETS.map(target => {
      const old = Array.isArray(stored?.products) ? stored.products.find(p => p?.id === target.id) : null;
      if (!old) return initialProduct(target, explanation);
      const count = Number.isSafeInteger(old.unknown_count) && old.unknown_count >= 0 ? old.unknown_count : 0;
      const status = STATES.has(old.status) && !count ? old.status : 'unknown';
      return { ...initialProduct(target), status,
        last_confirmed: STATES.has(old.last_confirmed) ? old.last_confirmed : 'unknown',
        last_checked: typeof old.last_checked === 'string' && Number.isFinite(Date.parse(old.last_checked)) ? old.last_checked : null,
        unknown_count: count,
        stock: status === 'unavailable' ? 0 : status === 'available' && Number.isSafeInteger(old.stock) && old.stock > 0 ? old.stock : null,
        explanation: status === 'available' ? '已确认目标套餐有库存' : status === 'unavailable' ? '已确认目标套餐无库存' : '本轮未能可靠确认库存，请等待下一次定时检查',
      };
    }),
    published_at: typeof stored?.published_at === 'string' && Number.isFinite(Date.parse(stored.published_at)) ? stored.published_at : null,
  };
}

async function checkTarget(target, fetcher) {
  const controller = new AbortController();
  // 超时覆盖响应体读取；不做浏览器 fallback，也不在同轮重试。
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetcher(target.product_url, {
      signal: controller.signal, redirect: 'error', cache: 'no-store',
      headers: { Accept: 'text/html,application/xhtml+xml', 'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': 'VPSStockMonitor/1.0 (+https://github.com/daidaideaa/vps_monitor)',
        'Cache-Control': 'no-cache' },
    });
    if (response.status !== 200 || !/^text\/html\b|^application\/xhtml\+xml\b/i.test(response.headers.get('content-type') || '') ||
        response.headers.get('cf-mitigated') === 'challenge' || response.headers.has('content-range')) {
      await response.body?.cancel();
      return { status: 'unknown', stock: null };
    }
    const reader = response.body?.getReader();
    if (!reader) return { status: 'unknown', stock: null };
    const decoder = new TextDecoder();
    let html = '', size = 0;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_HTML_BYTES) { await reader.cancel(); return { status: 'unknown', stock: null }; }
      html += decoder.decode(value, { stream: true });
    }
    html += decoder.decode();
    return target.provider === 'ZgoCloud' ? parseZgoCloud(html) : parseRfchost(html);
  } finally { clearTimeout(timer); }
}

export async function runScheduled(env, fetcher = fetch, now = () => new Date().toISOString()) {
  // KV 读取失败时中止，避免覆盖 last_confirmed；旧记录会自然变成 stale。
  const previous = publicSnapshot(await env.VPS_MONITOR_KV.get(STATE_KEY, 'json'));
  const results = await Promise.allSettled(TARGETS.map(target => checkTarget(target, fetcher)));
  const published_at = now();
  const products = TARGETS.map((target, index) => {
    const old = previous.products[index];
    const result = results[index];
    const current = result.status === 'fulfilled' ? result.value : { status: 'unknown', stock: null };
    const unknown = current.status === 'unknown';
    return { ...target, ...current, last_confirmed: unknown ? old.last_confirmed : current.status,
      last_checked: published_at, unknown_count: unknown ? Math.min(old.unknown_count + 1, Number.MAX_SAFE_INTEGER) : 0,
      explanation: unknown ? '本轮请求异常、网站验证或套餐结构变化，无法可靠确认库存' :
        current.status === 'available' ? '已确认目标套餐有库存' : '已确认目标套餐无库存',
      check_interval_seconds: 180 };
  });
  const state = { schema_version: 1, products, published_at };
  // 两家合并后每轮只写一次：约 480 writes/day。
  await env.VPS_MONITOR_KV.put(STATE_KEY, JSON.stringify(state));
  console.log(JSON.stringify({ event: 'stock_check', published_at,
    products: products.map(({ provider, status, unknown_count }) => ({ provider, status, unknown_count })) }));
  return state;
}

export default {
  async scheduled(_event, env, _ctx) {
    try { await runScheduled(env); }
    catch { throw new Error('定时检查未完成，未发布新状态'); }
  },
  async fetch(request, env) {
    if (new URL(request.url).pathname !== '/status.json') {
      return new Response(JSON.stringify({ error: 'Not found' }), { status: 404, headers: HEADERS });
    }
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: HEADERS });
    if (request.method !== 'GET') {
      return new Response(JSON.stringify({ error: 'Method not allowed' }), { status: 405, headers: { ...HEADERS, Allow: 'GET, OPTIONS' } });
    }
    try {
      const stored = await env.VPS_MONITOR_KV.get(STATE_KEY, 'json');
      return new Response(JSON.stringify(publicSnapshot(stored)), { headers: HEADERS });
    } catch {
      return new Response(JSON.stringify(publicSnapshot(null, '暂时无法读取已保存状态')), { status: 503, headers: HEADERS });
    }
  },
};
