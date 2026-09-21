import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { runScheduled, STATE_KEY, TARGETS } from '../src/index.js';

const stamp = '2026-09-21T12:00:00.000Z';
const html = body => new Response(body, { headers: { 'Content-Type': 'text/html' } });
const zgo = '<h1>Tokyo Intel VPS</h1><div>Starter</div>Continue<div>Standard</div>';
const rfc = '<h3>JP2-CO-Micro-Lite</h3>0 Available<h3>JP2-CO-Mini-Lite</h3>2 Available';
function kv(initial = null) {
  let saved = initial;
  const calls = { reads: 0, writes: 0 };
  return { calls, async get(key) { assert.equal(key, STATE_KEY); calls.reads++; return saved; },
    async put(key, data) { assert.equal(key, STATE_KEY); calls.writes++; saved = JSON.parse(data); } };
}

test('同轮并行检查两家且只进行一次合并写入', async () => {
  const storage = kv(); let started = 0, release;
  const bothStarted = new Promise(resolve => { release = resolve; });
  const state = await runScheduled({ VPS_MONITOR_KV: storage }, async url => {
    if (++started === 2) release();
    await bothStarted;
    return html(url.includes('zgovps') ? zgo : rfc);
  }, () => stamp);
  assert.equal(storage.calls.reads, 1); assert.equal(storage.calls.writes, 1);
  assert.deepEqual(state.products.map(p => p.status), ['available','unavailable']);
  assert.equal(state.products[1].stock, 0);
  assert.ok(state.products.every(p => p.last_checked === stamp && p.check_interval_seconds === 180));
});

test('单商家网络失败保留 last_confirmed，但当前为 unknown；恢复后清零计数', async () => {
  const storage = kv({ products: [{ ...TARGETS[0], status: 'available', last_confirmed: 'available', unknown_count: 0 }] });
  const env = { VPS_MONITOR_KV: storage };
  const first = await runScheduled(env, async url => {
    if (url.includes('zgovps')) throw new Error('private-cookie-secret');
    return html(rfc);
  }, () => stamp);
  assert.equal(first.products[0].status, 'unknown');
  assert.equal(first.products[0].last_confirmed, 'available');
  assert.equal(first.products[0].stock, null);
  assert.equal(first.products[0].unknown_count, 1);
  assert.equal(first.products[1].status, 'unavailable');
  assert.ok(!JSON.stringify(first).includes('private-cookie-secret'));
  const second = await runScheduled(env, async () => new Response('blocked', { status: 403 }), () => stamp);
  assert.equal(second.products[0].unknown_count, 2);
  const third = await runScheduled(env, async url => html(url.includes('zgovps') ? zgo : rfc), () => stamp);
  assert.equal(third.products[0].unknown_count, 0);
  assert.equal(third.products[0].status, 'available');
});

test('403、部分响应、非 HTML、验证响应和过大页面一律 unknown', async () => {
  for (const make of [() => new Response(zgo, { status: 403 }), () => new Response(zgo, { status: 206 }),
    () => new Response(zgo, { headers: { 'content-type': 'application/json' } }),
    () => new Response(zgo, { headers: { 'content-type': 'text/html', 'cf-mitigated': 'challenge' } }),
    () => html(zgo + ' '.repeat(1024 * 1024))]) {
    const state = await runScheduled({ VPS_MONITOR_KV: kv() }, async () => make(), () => stamp);
    assert.ok(state.products.every(p => p.status === 'unknown' && p.stock === null));
  }
});

test('公开 API 空 KV 连续读取也不抓商家、不写 KV；CORS 和只读路由', async t => {
  t.mock.method(globalThis, 'fetch', () => { throw new Error('不应访问商户'); });
  const storage = kv(); const env = { VPS_MONITOR_KV: storage };
  const request = method => new Request('https://unit.test/status.json', { method });
  for (let i = 0; i < 2; i++) {
    const response = await worker.fetch(request('GET'), env);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('access-control-allow-origin'), '*');
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
    const data = await response.json();
    assert.equal(data.products.length, 2);
    assert.ok(data.products.every(p => p.status === 'unknown' && p.last_checked === null && p.explanation === '等待首次定时检查'));
  }
  assert.equal(storage.calls.writes, 0);
  assert.equal((await worker.fetch(request('OPTIONS'), env)).status, 204);
  assert.equal((await worker.fetch(request('POST'), env)).status, 405);
  assert.equal((await worker.fetch(new Request('https://unit.test/check'), env)).status, 404);
  assert.equal(storage.calls.reads, 2);
});

test('KV 故障不覆盖旧状态，公开 API 不泄露堆栈或敏感字段', async () => {
  let writes = 0, fetches = 0;
  const env = { VPS_MONITOR_KV: { async get() { throw new Error('secret'); }, async put() { writes++; } } };
  await assert.rejects(runScheduled(env, async () => { fetches++; }));
  assert.equal(writes, 0); assert.equal(fetches, 0);
  const response = await worker.fetch(new Request('https://unit.test/status.json'), env);
  assert.equal(response.status, 503); assert.ok(!(await response.text()).includes('secret'));
  const stored = kv({ secret: 'secret', products: [{ ...TARGETS[0], status: 'unknown', last_confirmed: 'available',
    stock: 2, unknown_count: 1, explanation: 'secret', cookies: 'secret', html: 'secret' }] });
  const publicData = await (await worker.fetch(new Request('https://unit.test/status.json'), { VPS_MONITOR_KV: stored })).json();
  assert.equal(publicData.products[0].last_confirmed, 'available');
  assert.equal(publicData.products[0].stock, null);
  assert.ok(!JSON.stringify(publicData).includes('secret'));
});

test('公开 unknown 原因使用固定分类，区分 HTTP 拒绝与解析失败', async () => {
  const storage = kv();
  const env = { VPS_MONITOR_KV: storage };
  await runScheduled(env, async url => url.includes('zgovps') ?
    new Response('private challenge page', { status: 403 }) : html('<div>页面结构变化</div>'), () => stamp);
  const data = await (await worker.fetch(new Request('https://unit.test/status.json'), env)).json();
  assert.equal(data.products[0].explanation, '商家 HTTP 403，拒绝访问');
  assert.match(data.products[1].explanation, /未能可靠识别/);
  assert.ok(data.products.every(p => p.status === 'unknown' && p.stock === null));
  assert.ok(!JSON.stringify(data).includes('private challenge page'));
});
