import { runScheduled } from '../src/index.js';

// 使用真正的 Workers fetch / Request，避免 Node mock 掩盖运行时参数不兼容。
export default {
  async test() {
    let state, writes = 0;
    const env = { VPS_MONITOR_KV: {
      async get() { return state || null; },
      async put(_key, value) { state = JSON.parse(value); writes++; },
    } };
    await runScheduled(env);
    if (writes !== 1 || state.products.some(p => p.status !== 'unavailable' || p.stock !== 0)) {
      throw new Error('Workers 运行时未能抓取并解析普通 HTML');
    }
    await runScheduled(env, (url, options) => fetch(url + '&runtime-redirect=1', options));
    if (writes !== 2 || state.products.some(p => p.status !== 'unknown' || p.last_confirmed !== 'unavailable')) {
      throw new Error('重定向必须为 unknown，并保留最近确认状态');
    }
  },
};
