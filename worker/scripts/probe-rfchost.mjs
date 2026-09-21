import { chromium } from 'playwright';
import { TARGETS } from '../src/index.js';
import { analyzePage, hasChallenge } from './rfchost-probe-parser.mjs';

// 只读复用仓库已确认的官方地址，不调用 scheduled、KV 或线上 API。
const target = TARGETS.find(product => product.id === 'rfchost-jp2-co-micro-lite');
const expected = new URL(target.product_url);
let browser, page, title = '', text = '', finalUrl = '';
let httpStatus = null, loaded = false, timedOut = false, challenge = false, forbidden = false;

// 保留地址诊断，避免重定向 URL 中偶然出现的会话参数进入日志。
function publicUrl(value) {
  try {
    const url = new URL(value);
    if (!['https:', 'http:'].includes(url.protocol)) return url.protocol;
    url.username = ''; url.password = ''; url.hash = '';
    for (const key of [...url.searchParams.keys()]) {
      if (!['currency', 'rp'].includes(key)) url.searchParams.set(key, '[redacted]');
    }
    return url.href.slice(0, 1000);
  } catch { return ''; }
}

async function inspectPage() {
  finalUrl = page.url();
  title = (await page.title()).replace(/[\u0000-\u001f\u007f]/g, ' ').slice(0, 200);
  text = await page.locator('body').innerText({ timeout: 5000 });
  if (text.length > 1000000) throw new Error('page-too-large');
  challenge ||= hasChallenge(title, text) || await page.locator(
    '#challenge-form, #cf-challenge-running, .g-recaptcha, .h-captcha, .cf-turnstile, ' +
    'iframe[src*="challenges.cloudflare.com"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"]'
  ).count() > 0;
}

try {
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage();
  page.on('response', response => {
    if (response.request().isNavigationRequest() && response.frame() === page.mainFrame()) {
      httpStatus = response.status();
      forbidden ||= httpStatus === 403;
    }
  });
  // 浏览器正常跟随重定向，不注入 cookies、不重试或处理安全验证。
  await page.goto(target.product_url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await inspectPage();
  if (!challenge && httpStatus === 200 && !forbidden) {
    await page.waitForLoadState('load', { timeout: 15000 });
    await inspectPage();
  }
  loaded = true;
} catch (error) {
  timedOut = error?.name === 'TimeoutError';
  // 保留已采集的诊断，但绝不把超时后残留的页面当作可靠库存。
  if (page) {
    finalUrl = page.url();
    try { await inspectPage(); } catch { /* 不打印浏览器异常或页面原文。 */ }
  }
} finally {
  if (browser) await browser.close().catch(() => {});
}

let urlAllowed = false;
try { urlAllowed = new URL(finalUrl).origin === expected.origin; } catch { /* 未完成导航。 */ }
const result = analyzePage({ text, title, httpStatus, loaded, timedOut, challenge, forbidden, urlAllowed });
console.log(JSON.stringify({ ...result, final_url: publicUrl(finalUrl), page_title: title }, null, 2));
// 红色运行表示无法确认；真实无货仍是一次成功的探测。
if (result.status === 'unknown') process.exitCode = 1;
