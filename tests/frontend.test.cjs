const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');

function element() {
  return { children: [], dataset: {}, textContent: '', lastChild: { textContent: '' },
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    addEventListener() {} };
}
function page(fetcher) {
  const script = readFileSync(join(__dirname, '../index.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
  const nodes = { cards: element(), reload: element(), refresh: element(), overview: element() };
  const context = vm.createContext({ document: { getElementById: id => nodes[id], createElement: element },
    fetch: fetcher, AbortSignal, URL, Intl, setInterval() {} });
  vm.runInContext(script.replace("reload.addEventListener('click',load);load();", "reload.addEventListener('click',load);"), context);
  return { nodes, run: code => vm.runInContext(code, context),
    states: () => { const visit = node => [...(node.dataset.productId ? [node.dataset.state] : []), ...node.children.flatMap(visit)]; return nodes.cards.children.flatMap(visit); },
    text: () => { const visit = node => [node.textContent, ...node.children.flatMap(visit)]; return nodes.cards.children.flatMap(visit).join(' '); } };
}
const checked = () => new Date().toISOString();
const snapshot = () => ({ schema_version: 2, products: [
  { id: 'rfchost-jp2-co-micro-lite', provider: 'RFCHOST', status: 'unavailable', last_confirmed: 'unavailable', last_checked: checked(), check_interval_seconds: 180, stock: 0 },
  { id: 'vmiss-jp-tky-tri-basic', provider: 'VMISS', status: 'available', last_confirmed: 'available', last_checked: checked(), check_interval_seconds: 180 },
  { id: 'zgocloud-tokyo-intel-starter', provider: 'ZgoCloud', status: 'unavailable', last_confirmed: 'unavailable', last_checked: checked(), check_interval_seconds: 180, stock: 0 },
  { id: 'vps-tokyo-cloud-essential', provider: 'V.PS', status: 'unavailable', last_confirmed: 'unavailable', last_checked: checked(), check_interval_seconds: 300 },
  { id: 'vps-tokyo-cloud-starter', provider: 'V.PS', status: 'available', last_confirmed: 'available', last_checked: checked(), check_interval_seconds: 300 },
] });

test('一次刷新只读 VPS API，三张卡片展示三个独立套餐', async () => {
  const urls = [];
  const p = page(async url => { urls.push(url); return Response.json(snapshot()); });
  await p.run('load()');
  assert.deepEqual(urls, ['https://jp-vps-status.jp-home-subscription.workers.dev/status.json']);
  assert.deepEqual(p.states(), ['available','unavailable','unavailable']);
  assert.equal(p.nodes.cards.children.length, 3);
  assert.equal(p.run("latest.map(p=>p.provider).join(',')"), 'VMISS,ZgoCloud,RFCHOST');
});

test('旧快照中的 V.PS 不再显示或计入统计', async () => {
  const p = page(async () => Response.json(snapshot())); await p.run('load()');
  assert.equal(p.nodes.cards.children.length, 3);
  assert.equal(p.text().includes('V.PS'), false);
  const visit = node => [node.textContent, ...node.children.flatMap(visit)];
  assert.equal(visit(p.nodes.overview).join(''), '1 个套餐有货1 有货 / 2 无货 / 0 待确认');
});

test('页面同步时间独立于商家实际检查时间，失败不显示同步成功', async () => {
  let failed = false;
  const data = snapshot();
  data.products[1].last_checked = new Date(Date.now()-120000).toISOString();
  const p = page(async () => { if(failed)throw new Error('network'); return Response.json(data); });
  await p.run('load()');
  assert.match(p.nodes.refresh.textContent, /^页面同步于 \d{2}:\d{2}:\d{2}$/);
  assert.equal(p.run('latest[0].last_checked'), data.products[1].last_checked);
  failed = true; await p.run('load()');
  assert.equal(p.nodes.refresh.textContent, '同步失败，稍后重试');
});

test('VPS 断连保留历史但三个套餐均为 offline，恢复后解除离线', async () => {
  let failed = false;
  const p = page(async () => { if (failed) throw new Error('network'); return Response.json(snapshot()); });
  await p.run('load()'); failed = true; await p.run('load()');
  assert.deepEqual(p.states(), ['offline','offline','offline']);
  assert.equal(p.run('latest[0].last_confirmed'), 'available');
  failed = false; await p.run('load()');
  assert.deepEqual(p.states(), ['available','unavailable','unavailable']);
});

test('单商家 unknown、缺失或过期不污染其它商家的结果', async () => {
  const data = snapshot();
  data.products[0] = {...data.products[0], status:'unknown', last_confirmed:'available', stock:3, unknown_count:1};
  const p = page(async () => Response.json(data));
  await p.run('load()');
  assert.deepEqual(p.states(), ['available','unavailable','unknown']);
  assert.equal(p.run('latest[2].stock'), null);
  data.products.splice(2,1); await p.run('load()');
  assert.deepEqual(p.states(), ['available','offline','unknown']);
  data.products[0].last_checked = '2020-01-01T00:00:00Z'; await p.run('load()');
  assert.deepEqual(p.states(), ['available','offline','stale']);
});

test('旧单产品 API 只展示 VMISS，不借用其它来源补造其它结果', async () => {
  const p = page(async () => Response.json({schema_version:1, ...snapshot().products[1]}));
  await p.run('load()');
  assert.deepEqual(p.states(), ['available','offline','offline']);
});

test('重复 id 不会选择任意结果作为当前库存', async () => {
  const data = snapshot(); data.products.push({...data.products[0], status:'available', stock:2});
  const p = page(async () => Response.json(data)); await p.run('load()');
  assert.deepEqual(p.states(), ['available','unavailable','offline']);
});

test('540 秒边界与不同 interval 的 stale 语义', () => {
  const p = page(); p.run('Date.now=()=>1000000');
  assert.equal(p.run("productState({status:'available',last_checked:new Date(460000).toISOString(),check_interval_seconds:180}).state"), 'available');
  assert.equal(p.run("productState({status:'available',last_checked:new Date(459999).toISOString(),check_interval_seconds:180}).state"), 'stale');
  assert.equal(p.run("productState({status:'available',last_checked:new Date(819000).toISOString(),check_interval_seconds:60}).state"), 'stale');
});

test('首次检查前 unknown，历史显示北京时间，unknown 或过期保留无货起点并标为待确认', () => {
  const p = page();
  p.run("latest[1]={...latest[1],pending:false,unknown_count:0,explanation:'等待首次定时检查'};draw()");
  assert.equal(p.states()[1], 'unknown');
  p.run("Date.now=()=>Date.parse('2026-09-23T04:05:00Z')");
  const item = {unavailable_since:'2026-09-21T02:00:00Z',last_checked:'2026-09-23T04:03:00Z',last_available_at:'2026-09-21T01:57:00Z'};
  const history = state => p.run(`inventoryHistory(${JSON.stringify(item)}, '${state}')`);
  assert.equal(history('unavailable')[0][1], '2 天 2 小时 5 分钟');
  assert.match(history('unavailable')[1][1], /2026.*09.*21.*09:57/);
  assert.equal(history('unknown')[0][1], '2 天 2 小时 5 分钟（当前待确认）');
  assert.equal(history('stale')[0][1], '2 天 2 小时 5 分钟（当前待确认）');
  assert.equal(history('available')[0][1], '当前有货');
  assert.equal(p.run("inventoryHistory({}, 'unavailable')[0][1]"), '尚无记录');
});

test('仅保留三个目标，拒绝退休商家链接，默认十分钟并随机抖动', () => {
  const p = page();
  assert.equal(p.run('latest.length'), 3);
  assert.equal(p.run('latest.every(item => item.check_interval_seconds === 600)'), true);
  assert.equal(p.run("safeLink('https://vps.hosting/')"), '#');
  assert.equal(p.run("productState({}).interval"), 600);
});
