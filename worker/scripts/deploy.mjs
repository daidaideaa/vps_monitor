import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const workerDir = fileURLToPath(new URL('../', import.meta.url));
const outputDir = mkdtempSync(join(tmpdir(), 'vps-monitor-deploy-'));
const outputFile = join(outputDir, 'deployment.jsonl');
try {
  // Wrangler 自动创建缺失的 KV，并将真实 namespace ID 写回配置。
  const result = spawnSync(process.execPath, [join(workerDir, 'node_modules/wrangler/bin/wrangler.js'), 'deploy'], {
    cwd: workerDir, stdio: 'inherit', env: { ...process.env, WRANGLER_OUTPUT_FILE_PATH: outputFile },
  });
  if (result.error || result.status !== 0) throw new Error('Worker 部署失败，前端地址未修改');
  const deployments = readFileSync(outputFile, 'utf8').trim().split('\n').map(line => JSON.parse(line));
  const deployed = deployments.findLast(entry => entry.type === 'deploy' && entry.worker_name === 'vps-monitor');
  const base = deployed?.targets?.find(target => typeof target === 'string' &&
    /^https:\/\/vps-monitor\.[a-z0-9-]+\.workers\.dev\/?$/.test(target));
  if (!base) throw new Error('部署输出没有正式 workers.dev 地址，前端未修改');
  const api = new URL('/status.json', base).href;
  const response = await fetch(api, { signal: AbortSignal.timeout(20000), cache: 'no-store' });
  if (!response.ok || response.headers.get('access-control-allow-origin') !== '*' ||
      response.headers.get('cache-control') !== 'no-store') throw new Error('Worker API 验证失败，前端未修改');
  const state = await response.json();
  if (state.schema_version !== 1 || state.products?.length !== 2 ||
      state.products[0].id !== 'zgocloud-tokyo-intel-starter' || state.products[1].id !== 'rfchost-jp2-co-micro-lite') {
    throw new Error('Worker API 产品不匹配，前端未修改');
  }
  const pagePath = new URL('../../index.html', import.meta.url);
  const page = readFileSync(pagePath, 'utf8');
  if (!/^const WORKER_API=.*;$/m.test(page)) throw new Error('未找到前端 Worker 配置');
  writeFileSync(pagePath, page.replace(/^const WORKER_API=.*;$/m, `const WORKER_API=${JSON.stringify(api)};`));
  const readmePath = new URL('../../README.md', import.meta.url);
  const readme = readFileSync(readmePath, 'utf8');
  writeFileSync(readmePath, readme.replace(/<!-- worker-api:start -->[\s\S]*?<!-- worker-api:end -->/,
    `<!-- worker-api:start -->\nWorker API：[${api}](${api})\n<!-- worker-api:end -->`));
  console.log(`已验证并写入 index.html / README.md：${api}`);
  console.log('等待首次 Cron 后验证 last_checked，再提交 index.html、README.md 和 wrangler.jsonc。');
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
} finally { rmSync(outputDir, { recursive: true, force: true }); }
