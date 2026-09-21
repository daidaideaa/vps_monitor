// 独立探测解析器，不参与线上 Worker 的库存判断。
const MICRO = /(?<![\w-])JP2-CO-Micro-Lite(?![\w-])/g;
const MINI = /(?<![\w-])JP2-CO-Mini-Lite(?![\w-])/g;
const CHALLENGE = /captcha|just a moment|checking your browser|verify (?:that )?you are human|security verification|人机验证|验证码|安全验证/i;

export function hasChallenge(title = '', text = '') {
  return CHALLENGE.test(title + '\n' + text);
}

export function analyzePage({ text = '', title = '', httpStatus = null, loaded = false,
  timedOut = false, challenge = false, forbidden = false, urlAllowed = true } = {}) {
  const plain = text.replace(/\s+/g, ' ').trim();
  const starts = [...plain.matchAll(MICRO)], ends = [...plain.matchAll(MINI)];
  const boundary = starts.length === 1 && ends.length === 1 && starts[0].index < ends[0].index;
  const region = boundary ? plain.slice(starts[0].index + starts[0][0].length, ends[0].index) : '';
  const neighboringPlan = /JP2-CO-(?:Standard|Advanced|Max)(?:-Lite)?\b/.test(region);
  const counts = [...region.matchAll(/(?:^|\s)(\d+)\s+Available\b/gi)];
  const words = [...region.matchAll(/\bAvailable\b/gi)];
  const challenged = challenge || hasChallenge(title, plain);
  const httpOk = httpStatus === 200 && !forbidden;
  const unique = boundary && !neighboringPlan && counts.length === 1 && words.length === 1;
  const stock = unique ? Number(counts[0][1]) : null;
  const reliable = loaded && !timedOut && httpOk && !challenged && urlAllowed &&
    unique && Number.isSafeInteger(stock) && !(stock > 0 && /out\s+of\s+stock/i.test(region));
  return {
    http_status: httpStatus,
    http_ok: httpOk,
    page_load: timedOut ? 'timeout' : challenged ? 'challenge' : !loaded ? 'failed' :
      !httpOk ? 'http_error' : !urlAllowed ? 'unexpected_url' : 'success',
    found_micro: starts.length > 0,
    found_mini: ends.length > 0,
    boundary_valid: boundary && !neighboringPlan,
    available_match_count: counts.length,
    unique_available: unique,
    stock: reliable ? stock : null,
    status: reliable ? (stock === 0 ? 'unavailable' : 'available') : 'unknown',
  };
}
