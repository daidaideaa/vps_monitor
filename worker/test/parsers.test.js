import test from 'node:test';
import assert from 'node:assert/strict';
import { parseZgoCloud, parseRfchost } from '../src/parsers.js';

const unknown = { status: 'unknown', stock: null };
const zgo = control => `<h1>Tokyo Intel VPS</h1><div>Starter</div>${control}<div>Standard</div><button>Continue</button>`;
const rfc = count => `<h3>JP2-CO-Micro-Lite</h3><div>${count}</div><h3>JP2-CO-Mini-Lite</h3>9 Available`;

test('Starter 库存只来自自身卡片，兼容实体和嵌套标签', () => {
  assert.deepEqual(parseZgoCloud(zgo('<button>Out of stock!</button>')), { status: 'unavailable', stock: 0 });
  assert.deepEqual(parseZgoCloud(zgo('<button>Continue</button>') + '<p>Out of stock!</p>'), { status: 'available', stock: null });
  assert.deepEqual(parseZgoCloud(zgo('<button>Out&nbsp;of <span>stock!</span></button>')), { status: 'unavailable', stock: 0 });
});

test('ZgoCloud 缺标记、边界丢失、重复套餐和冲突控制均 unknown', () => {
  for (const html of [zgo('').replace('Tokyo Intel VPS', 'Osaka VPS'), zgo('').replace('Starter','Other'),
    zgo(''), zgo('Continue').replace('Standard', 'Pro'), zgo('Continue Out of stock!'),
    zgo('Continue Starter'), '<h1>Tokyo Intel VPS</h1>Starter Continue']) {
    assert.deepEqual(parseZgoCloud(html), unknown);
  }
});

test('RFCHOST 只接受 Micro 到 Mini 之间的唯一非负整数', () => {
  assert.deepEqual(parseRfchost(rfc('0 Available')), { status: 'unavailable', stock: 0 });
  assert.deepEqual(parseRfchost(rfc('2 Available')), { status: 'available', stock: 2 });
  assert.deepEqual(parseRfchost(rfc('&#50;&nbsp;Available')), { status: 'available', stock: 2 });
  for (const html of [rfc(''), rfc('Out of Stock'), rfc('-2 Available'), rfc('1.5 Available'), rfc('1,000 Available'),
    rfc('2 Available 0 Available'), rfc('2 Available Out of stock'), rfc('999999999999999999999 Available'),
    rfc('2 Available').replace('JP2-CO-Mini-Lite','JP2-CO-Standard-Lite'),
    rfc('').replace('JP2-CO-Micro-Lite','JP2-CO-Max-Lite'), rfc('JP2-CO-Advanced 2 Available')]) {
    assert.deepEqual(parseRfchost(html), unknown);
  }
});

test('脚本、注释及验证页不能作为库存证据', () => {
  assert.deepEqual(parseZgoCloud(zgo('<script>Continue</script><!-- Continue -->')), unknown);
  for (const [parse, html] of [[parseZgoCloud, zgo('Continue')], [parseRfchost, rfc('2 Available')]]) {
    assert.deepEqual(parse('<h1>Just a moment</h1>' + html), unknown);
    assert.deepEqual(parse('<script src="/cdn-cgi/challenge-platform/test"></script>' + html), unknown);
    assert.deepEqual(parse('<p>Verify you are human</p>' + html), unknown);
  }
});
