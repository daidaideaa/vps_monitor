const unknown = () => ({ status: 'unknown', stock: null });

// 只读可见文本；脚本、样式和注释中的旧库存不能作为本轮证据。
function visibleText(html) {
  return html
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/<(script|style|noscript|template)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, ' ')
    .replace(/<[^>]*>/g, ' ')
    .replace(/&#(x[\da-f]+|\d+);/gi, (_, code) => {
      const value = code[0].toLowerCase() === 'x' ? parseInt(code.slice(1), 16) : Number(code);
      return value > 0 && value <= 0x10ffff ? String.fromCodePoint(value) : ' ';
    })
    .replace(/&(nbsp|amp|lt|gt|quot|apos);/gi, (_, name) =>
      ({ nbsp: ' ', amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" })[name.toLowerCase()])
    .replace(/\s+/g, ' ').trim();
}

function pageText(html) {
  if (typeof html !== 'string' || !/<(?:html|body|div|h[1-6]|section|form)\b/i.test(html)) return null;
  // 验证页不能借页面中残留的套餐文字误报库存。
  if (/cf-chl-|\/cdn-cgi\/challenge-platform|g-recaptcha|h-captcha|cf-turnstile/i.test(html)) return null;
  const text = visibleText(html);
  if (/just a moment|checking your browser|verify (?:that )?you are human|access denied|captcha|验证码|人机验证/i.test(text)) return null;
  return text;
}

function segment(text, first, next) {
  const starts = [...text.matchAll(first)];
  const ends = [...text.matchAll(next)];
  // 套餐边界消失或重复时宁可 unknown，不让相邻套餐污染结果。
  if (starts.length !== 1 || ends.length !== 1 || ends[0].index <= starts[0].index) return null;
  return text.slice(starts[0].index + starts[0][0].length, ends[0].index);
}

export function parseZgoCloud(html) {
  const text = pageText(html);
  if (!text || !/\bTokyo Intel VPS\b/.test(text)) return unknown();
  const card = segment(text, /\bStarter\b/g, /\bStandard\b/g);
  if (card === null || /\b(?:Pro|Premium)\b/.test(card)) return unknown();
  const out = /\bOut\s+of\s+stock\b!?/i.test(card);
  const available = /\bContinue\b/.test(card);
  if (out === available) return unknown();
  return out ? { status: 'unavailable', stock: 0 } : { status: 'available', stock: null };
}

export function parseRfchost(html) {
  const text = pageText(html);
  if (!text) return unknown();
  const card = segment(text, /\bJP2-CO-Micro-Lite\b/g, /\bJP2-CO-Mini-Lite\b/g);
  if (card === null || /JP2-CO-(?:Standard|Advanced|Max)/i.test(card)) return unknown();
  const counts = [...card.matchAll(/(?:^|\s)(\d+)\s+Available\b/gi)];
  if (counts.length !== 1) return unknown();
  const stock = Number(counts[0][1]);
  if (!Number.isSafeInteger(stock) || (stock > 0 && /Out\s+of\s+Stock/i.test(card))) return unknown();
  return { status: stock > 0 ? 'available' : 'unavailable', stock };
}
