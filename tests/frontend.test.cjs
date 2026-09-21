const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');

// 执行页面实际脚本，以 DOM 轻量替身检验用户可见卡片和异步故障隔离。
function element() {
  return { children: [], dataset: {}, textContent: '', lastChild: { textContent: '' },
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    addEventListener() {} };
}
function page(fetcher) {
  const script = readFileSync(join(__dirname, '../index.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
  const nodes = { cards: element(), reload: element(), refresh: element() };
  const context = vm.createContext({ document: { getElementById: id => nodes[id], createElement: element },
    fetch: fetcher, AbortSignal, URL, Intl, setInterval() {} });
  vm.runInContext(script.replace(/^const WORKER_API=.*;$/m, "const WORKER_API='https://worker.test/status.json';").replace("reload.addEventListener('click',load);load();", "reload.addEventListener('click',load);"), context);
  return { nodes, run: code => vm.runInContext(code, context),
    states: () => nodes.cards.children.map(card => card.dataset.state),
    text: () => { const visit = node => [node.textContent, ...node.children.flatMap(visit)]; return nodes.cards.children.flatMap(visit).join(' '); } };
}
const checked = () => new Date().toISOString();
const vmiss = () => ({ schema_version: 1, status: 'available', last_confirmed: 'available', last_checked: checked(), check_interval_seconds: 180 });
const worker = () => ({ schema_version: 1, products: [
  { id: 'rfchost-jp2-co-micro-lite', provider: 'RFCHOST', status: 'unavailable', last_confirmed: 'unavailable', last_checked: checked(), check_interval_seconds: 180, stock: 0 },
  { id: 'zgocloud-tokyo-intel-starter', provider: 'ZgoCloud', status: 'unavailable', last_confirmed: 'unavailable', last_checked: checked(), check_interval_seconds: 180, stock: 0 },
] });

test('固定顺序、兼容旧 v1 和 v2，并排除旧香港 API 中的其它两家', async () => {
  for (const schema of [1,2]) {
    const p = page(async url => Response.json(url.includes('vmiss-status') ? schema === 1 ? vmiss() :
      { schema_version: 2, products: [{ ...vmiss(), provider: 'VMISS' }, ...worker().products.map(x => ({ ...x, status: 'available' }))] } :
      { ...worker(), products: [...worker().products, { ...vmiss(), provider: 'VMISS' }] }));
    await p.run('load()');
    assert.deepEqual(p.states(), ['available','unavailable','unavailable']);
    assert.equal(p.run("latest.map(p=>p.provider).join(',')"), 'VMISS,ZgoCloud,RFCHOST');
  }
});

test('VMISS 接口失败只影响 VMISS；Worker 接口失败只影响两家', async () => {
  for (const source of ['vmiss-status','worker.test']) {
    const p = page(async url => {
      if (url.includes(source)) throw new Error('network');
      return Response.json(url.includes('vmiss-status') ? vmiss() : worker());
    });
    await p.run('load()');
    assert.deepEqual(p.states(), source === 'vmiss-status' ? ['offline','unavailable','unavailable'] : ['available','offline','offline']);
    assert.equal(p.nodes.cards.children.length, 3);
  }
});

test('慢接口不阻止另一数据源先显示；失败后保留历史、恢复后解除离线', async () => {
  let release;
  const waiting = new Promise(resolve => { release = resolve; });
  const p = page(async url => { if (url.includes('vmiss-status')) return waiting; return Response.json(worker()); });
  const loading = p.run('load()');
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(p.states(), ['unknown','unavailable','unavailable']);
  release(Response.json(vmiss())); await loading;
  p.run("mergeSource('vmiss', [], true); draw()");
  assert.equal(p.states()[0], 'offline'); assert.equal(p.run('latest[0].last_confirmed'), 'available');
  p.run(`mergeSource('vmiss', normalize(${JSON.stringify(vmiss())}, 'vmiss')); draw()`);
  assert.equal(p.states()[0], 'available');
});

test('unknown 不显示历史库存数字；540 秒边界与不同 interval 的 stale 语义', () => {
  const p = page();
  p.run("latest[0]={...latest[0],pending:false,status:'unknown',last_confirmed:'available',last_checked:new Date().toISOString(),stock:2};draw()");
  assert.equal(p.states()[0], 'unknown');
  assert.ok(p.text().includes('暂时无法确认')); assert.ok(p.text().includes('最近确认库存'));
  assert.ok(!p.text().includes('商家库存数字'));
  p.run('Date.now=()=>1000000');
  assert.equal(p.run("productState({status:'available',last_checked:new Date(460000).toISOString(),check_interval_seconds:180}).state"), 'available');
  assert.equal(p.run("productState({status:'available',last_checked:new Date(459999).toISOString(),check_interval_seconds:180}).state"), 'stale');
  assert.equal(p.run("productState({status:'available',last_checked:new Date(819000).toISOString(),check_interval_seconds:60}).state"), 'stale');
});

test('Worker 单产品缺失不会污染另一产品，未知状态不可沿用为有货', () => {
  const p = page();
  const data = worker(); data.products[0].status = 'broken'; data.products[0].last_confirmed = 'available';
  p.run(`mergeSource('worker', ${JSON.stringify(data.products)});draw()`);
  assert.deepEqual(p.states(), ['unknown','unavailable','unknown']);
  p.run(`mergeSource('worker', ${JSON.stringify([data.products[1]])});draw()`);
  assert.deepEqual(p.states(), ['unknown','unavailable','offline']);
});

test('首次定时检查前显示 unknown 等待，不误显示过期', () => {
  const p = page();
  p.run("latest[1]={...latest[1],pending:false,unknown_count:0,explanation:'等待首次定时检查'};draw()");
  assert.equal(p.states()[1], 'unknown');
  assert.ok(p.text().includes('等待首次定时检查'));
});

test('库存历史显示天时分及北京时间，未知或过期不继续累计无货', () => {
  const p = page();
  p.run("Date.now=()=>Date.parse('2026-09-23T04:05:00Z')");
  const item = {unavailable_since:'2026-09-21T02:00:00Z',last_checked:'2026-09-23T04:03:00Z',last_available_at:'2026-09-21T01:57:00Z'};
  const history = state => p.run(`inventoryHistory(${JSON.stringify(item)}, '${state}')`);
  assert.equal(history('unavailable')[0][1], '2 天 2 小时 5 分钟');
  assert.match(history('unavailable')[1][1], /2026.*09.*21.*09:57/);
  assert.equal(history('unknown')[0][1], '暂时无法确认');
  assert.equal(history('stale')[0][1], '检查数据已过期');
  assert.equal(history('available')[0][1], '当前有货');
  assert.equal(p.run("inventoryHistory({}, 'unavailable')[0][1]"), '尚无记录');
});
