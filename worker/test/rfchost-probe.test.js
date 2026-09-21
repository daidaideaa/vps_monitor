import test from 'node:test';
import assert from 'node:assert/strict';
import { analyzePage } from '../scripts/rfchost-probe-parser.mjs';

const card = stock => `JP2-CO-Micro-Lite ${stock} Available JP2-CO-Mini-Lite 99 Available JP2-CO-Standard-Lite 50 Available`;
const inspect = (text, overrides = {}) => analyzePage({ text, httpStatus: 200, loaded: true, ...overrides });

test('0 Available → unavailable；2 Available → available', () => {
  for (const stock of [0, 2]) {
    const result = inspect(card(stock));
    assert.equal(result.stock, stock);
    assert.equal(result.status, stock === 0 ? 'unavailable' : 'available');
    assert.equal(result.available_match_count, 1);
  }
});

test('相邻套餐的库存不能污染 Micro', () => {
  assert.equal(inspect(card(0)).status, 'unavailable');
  const missing = inspect('JP2-CO-Micro-Lite 无库存数字 JP2-CO-Mini-Lite 2 Available');
  assert.equal(missing.status, 'unknown');
  assert.equal(missing.available_match_count, 0);
});

test('多个 Available 或目标套餐缺失为 unknown', () => {
  for (const text of [
    'JP2-CO-Micro-Lite 0 Available 2 Available JP2-CO-Mini-Lite',
    'JP2-CO-Micro-Lite 2 Available Available JP2-CO-Mini-Lite',
    'JP2-CO-Mini-Lite 2 Available',
    'JP2-CO-Micro-Lite-Other 2 Available JP2-CO-Mini-Lite',
  ]) assert.equal(inspect(text).status, 'unknown');
});

test('缺失、重复、逆序或混入其他套餐的边界为 unknown', () => {
  for (const text of [
    'JP2-CO-Micro-Lite 2 Available',
    'JP2-CO-Mini-Lite 2 Available JP2-CO-Micro-Lite',
    'JP2-CO-Micro-Lite JP2-CO-Micro-Lite 2 Available JP2-CO-Mini-Lite',
    'JP2-CO-Micro-Lite 2 Available JP2-CO-Mini-Lite JP2-CO-Mini-Lite',
    'JP2-CO-Micro-Lite JP2-CO-Standard-Lite 2 Available JP2-CO-Mini-Lite',
  ]) assert.equal(inspect(text).status, 'unknown');
});

test('403、验证页、超时、加载失败或站外落地不能判为有货', () => {
  for (const overrides of [{ httpStatus: 403 }, { forbidden: true }, { httpStatus: 500 },
    { httpStatus: null }, { title: 'Just a moment...' }, { challenge: true },
    { timedOut: true }, { loaded: false }, { urlAllowed: false }]) {
    const result = inspect(card(2), overrides);
    assert.equal(result.status, 'unknown');
    assert.equal(result.stock, null);
  }
  assert.equal(inspect('CAPTCHA ' + card(2)).page_load, 'challenge');
});

test('非法库存数字或矛盾标记为 unknown', () => {
  for (const value of ['-1', '2.5', '1,000', '9007199254740992']) {
    assert.equal(inspect(card(value)).status, 'unknown');
  }
  assert.equal(inspect(card(2).replace('2 Available', 'Out of stock! 2 Available')).status, 'unknown');
});
