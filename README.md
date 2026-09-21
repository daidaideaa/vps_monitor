# 东京 VPS 库存观察

页面：[GitHub Pages](https://daidaideaa.github.io/vps_monitor/)。固定按 VMISS、ZgoCloud、RFCHOST 排列，保持现有三卡样式。

## 最终架构

| 目标套餐 | 检查位置 | 实际检查频率 |
| --- | --- | --- |
| VMISS `JP.TKY.TRI.Basic` | 香港 VPS，现有 Playwright 监控 | 约 180 秒 |
| ZgoCloud `Tokyo Intel VPS · Starter` | Cloudflare Module Worker，普通 HTTP | `*/3 * * * *` |
| RFCHOST `JP2-CO-Micro-Lite` | 同一个 Worker，普通 HTTP | `*/3 * * * *` |

GitHub Pages 每 30 秒独立读取两个公开 API，合并展示。VMISS API 中只取 VMISS（兼容单产品 schema v1 和旧三产品 schema v2），另外两家只从 Worker 获取。某个 API 断连只影响对应的卡片。页面刷新不会触发商家抓取。

- VMISS API：[status.json](https://vmiss-status.96-126-179-210.sslip.io/status.json)。VMISS 实际监控、Playwright 与原有邮件逻辑不变。
- Worker 名称：`vps-monitor`。
- KV binding：`VPS_MONITOR_KV`；主要 key：`tokyo-vps-status`。
- Cron：`*/3 * * * *`（UTC，约每 3 分钟）。

<!-- worker-api:start -->
Worker API：[status.json](https://vps-monitor.daidaidefish.workers.dev/status.json)。2026-09-21 已验证正式接口返回 HTTP 200、两家产品的 JSON，以及 CORS / no-store / nosniff 响应头；已确认 Cron 在北京时间 12:31:12 产生检查记录并完成 KV 写入；两家本轮均为 unknown，不能据此判断有货或无货。
<!-- worker-api:end -->

`index.html` 已固定使用上述正式 API。GitHub Pages 从 main 发布，无需构建；Worker 的代码和定时配置位于 `worker/`。

## 文件职责

```text
index.html                         双数据源三卡页面，无构建依赖
server/export_status.py            只导出 VMISS 公开状态（schema v1）
server/vmiss-public-status.*        原 VMISS 状态导出服务，每 30 秒执行
server/Caddyfile                    原 VMISS HTTPS 公开接口
worker/src/index.js                scheduled 检查、合并 KV、只读 API
worker/src/parsers.js               两家套餐范围及库存解析
worker/wrangler.jsonc              Worker、KV 与 Cron 配置
worker/scripts/deploy.mjs          部署、验证 API、回填真实地址
worker/test/                       解析、状态生命周期和 API 测试
tests/                             VMISS 导出及页面故障隔离测试
```

Worker 没有运行时第三方依赖，不使用浏览器、D1、Durable Objects、R2 或第三方数据库。普通 HTTP 出现 403、验证码、超时、重定向、非 HTML/部分响应、套餐边界缺失、标记冲突时保守返回 `unknown`。抓取使用 `redirect: 'manual'` 并检查状态码；实际 Workers 运行时不支持 `redirect: 'error'`，不能沿用 Node 的该选项。若商家拒绝 Cloudflare 节点访问，监控会持续显示无法确认，需要后续评估商家允许的访问方式，不能靠历史有货状态掩盖问题。

ZgoCloud 仅识别 `Starter` 到 `Standard` 之间的 `Continue / Out of stock!`；RFCHOST 仅识别 `JP2-CO-Micro-Lite` 到 `JP2-CO-Mini-Lite` 之间唯一的 `N Available`。购买地址直接沿用迁移前仓库的官方 URL。

## 本地测试与调试

Node.js 22 或更新版本；Python 测试需要原 VMISS 导出器使用的 `python-dotenv`。

```bash
cd worker
npm ci
npm test
npx wrangler deploy --dry-run
npx wrangler dev --test-scheduled
```

`npm test` 包含 Node 单元测试和真实 workerd 运行时回归测试（无需外网）。后者覆盖请求参数兼容性、HTML 抓取和重定向拒绝，避免 Node mock 接受了线上运行时不支持的参数。可用 `npm run test:runtime` 单独执行。

`npm run dev` 等同于最后一条命令。另开终端，仅向**本地开发服务**模拟一次 scheduled 事件：

```bash
curl 'http://localhost:8787/__scheduled?cron=*/3%20*%20*%20*%20*'
curl -i http://localhost:8787/status.json
```

本地 KV 与线上 KV 隔离。线上不提供手动触发检查的 HTTP 路径；线上 `GET /__scheduled` 返回 404。

仓库根目录执行 VMISS 导出测试：

```bash
python -m pip install python-dotenv
python -m unittest discover -s tests -v
```

## Cloudflare 网页部署

进入已创建的 `vps-monitor`，在 Settings / Builds 中连接 `daidaideaa/vps_monitor`，根目录 `worker`，构建命令留空，部署命令 `npx wrangler deploy`。合并本次迁移后，将 Production branch 从 `codex/cloudflare-stock-monitor` 改为 `main`，之后主分支更新即可自动部署。修改分支后应触发一次新的生产构建；旧的预览构建重试不能代替生产部署。

`Version command: npx wrangler versions upload` 可保留为非生产分支的预览命令，它不会将新版本正式上线。确认生产部署成功后，检查 `/status.json` 是否返回包含两个产品的 JSON；空 KV 也必须返回 JSON，而不是空文件。

网页部署不会将生成的 KV ID 自动提交回 GitHub。当前配置采用 Wrangler 自动配置资源：同一 Worker 的同名 `VPS_MONITOR_KV` binding 在后续部署时复用已有 namespace，无需为了重部署手动新建 KV。如需显式固定 ID，可从控制台 Bindings 读取 namespace ID 后写入 `worker/wrangler.jsonc`；不要更改 Worker 名称或删除已有 binding，以免丢失历史状态。

若接口返回空文件，请检查本次构建是否运行 `wrangler deploy`、生产版本是否承接流量，以及 Root directory 是否为 `worker`。

## Cloudflare 命令行部署

```bash
cd worker
npm ci
npx wrangler whoami
# 仅在未登录时，通过浏览器完成 Cloudflare 官方登录
npx wrangler login
npm run deploy
```

配置中未填写虚构的 namespace ID。首次 `wrangler deploy` 自动创建缺失的 KV，并把实际 ID 写回 `wrangler.jsonc`；后续部署复用该 namespace。多账号用户应先通过 `CLOUDFLARE_ACCOUNT_ID` 选择正确账号，不要把凭据提交到仓库。

`npm run deploy` 使用 Wrangler 的部署结果取得真实正式 workers.dev 地址；验证 API 的 schema、两家产品及 CORS 后，自动更新 `index.html` 的 `WORKER_API` 和本 README。部署或 API 验证失败不会回填地址。也可直接运行 `npx wrangler deploy`，但它不会执行页面地址回填，建议使用 `npm run deploy`。

部署后提交 `worker/wrangler.jsonc`、`index.html`、`README.md`。Wrangler 登录信息、token、账号凭据均不写入源码或公开响应。

首次读取空 KV 时返回两家 `unknown`，`last_checked=null`，说明“等待首次定时检查”。等待 Cron 自然执行后检查 `last_checked` 和 `published_at`。Cron 配置传播可能需要数分钟，最长约 15 分钟；不会通过网页请求临时补抓。

## 手动检查线上 API 和日志

部署成功后，在仓库根目录直接使用已回填的 URL：

```bash
node --input-type=module -e 'import {readFileSync} from "node:fs"; const url=JSON.parse(readFileSync("index.html","utf8").match(/^const WORKER_API=(.*);$/m)[1]); if(!url) throw new Error("Worker 尚未部署"); const r=await fetch(url); console.log(r.status,Object.fromEntries(r.headers)); console.log(await r.json());'
```

在 `worker/` 查看实时日志：

```bash
npx wrangler tail --format pretty
```

每轮成功写入后日志只记录时间、商家、当前状态和连续异常次数。KV 读写失败会报通用错误，不输出商户 HTML、cookies 或原始异常内容。KV 读取失败时中止该轮以保留历史确认状态；没有发布新时间戳，页面会在超时后显示 stale。

`GET /status.json` 为只读接口；响应含 `Content-Type: application/json; charset=utf-8`、`Cache-Control: no-store`、`Access-Control-Allow-Origin: *`、`X-Content-Type-Options: nosniff`。`OPTIONS /status.json` 返回 204，其他方法 405，其他路径 404。KV 读取故障返回 503 和不含敏感信息的未知状态。

## 状态判断

- `available`：本轮可靠确认有货；ZgoCloud `stock=null`，RFCHOST 为正整数。
- `unavailable`：本轮可靠确认无货，`stock=0`。
- `unknown`：本轮无法确认，`stock=null`；保留 `last_confirmed`，`unknown_count` 连续累加，恢复可靠检查后清零。历史有货只出现在“最近确认库存”中。
- `stale`：记录超过 `3 × check_interval_seconds`，180 秒间隔即 **大于 540 秒**；缺少/异常时间戳也视为过期。此时不展示当前有货或旧库存数字。
- `offline`：某个 API 无法访问或返回异常，该数据源所属卡片显示“连接异常”。另一个 API 的卡片正常显示。

Worker 快照的 schema 为 v1，包含 `products` 与 `published_at`；每个产品保存 `id/provider/product_name/product_url/status/last_confirmed/last_checked/unknown_count/stock/explanation/check_interval_seconds`。KV 可能存在短暂传播延迟；UI 根据检查时间判断有效性。

## 库存时间记录

三张卡显示“连续无货 · 观测时长”和“上次确认有货 · 北京时间”。API 新增 `unavailable_since`、`last_available_at`，缺少历史时为 null，页面显示“尚无记录”。

- `last_available_at`：最近一次可靠确认有货的检查时间，后续无货或 unknown 不覆盖它。
- `unavailable_since`：本轮连续无货观测的起点；首次无货从该次检查开始，有货、unknown 或超过三个检查间隔的断档会中断累计。
- 页面只在当前记录为有效的 unavailable 时显示时长；连接异常、unknown 或 stale 时不继续展示为连续无货。记录不代表两次轮询之间从未短暂补货，也无法还原启用本功能之前的真实售罄时间。

Worker 将时间字段保存在原有合并 KV 快照中，仍每轮只写一次。VMISS 导出器读取上次公开快照保存历史，重复导出同一检查不会重置计时，不修改 Playwright 状态文件。**VMISS 需要将新版 `server/export_status.py` 同步到香港 VPS 后才会输出这些字段**；旧 API 仍正常显示库存，历史栏显示“尚无记录”。

## 香港 VPS 迁移收尾

**先确认 Worker 两家均已产生 scheduled 记录、Pages 已回填真实地址，再停用旧两家任务。** 删除仓库文件不会自动停止服务器上已经运行的 systemd 服务。

在香港 VPS 执行以下命令，仅停用旧的 ZgoCloud/RFCHOST 服务：

```bash
sudo systemctl disable --now http-stock-monitor.timer
sudo systemctl stop http-stock-monitor.service
sudo rm -f /etc/systemd/system/http-stock-monitor.service /etc/systemd/system/http-stock-monitor.timer
sudo rm -f /opt/vmiss-public-status/http_stock_monitor.py /opt/vmiss-public-status/http-stock-monitor.service /opt/vmiss-public-status/http-stock-monitor.timer
sudo systemctl daemon-reload
```

将本仓库新的 `server/export_status.py` 同步至 `/opt/vmiss-public-status/export_status.py`，然后：

```bash
sudo systemctl start vmiss-public-status.service
curl https://vmiss-status.96-126-179-210.sslip.io/status.json
```

不要停止 `/opt/vmiss-stock-monitor` 对应的现有监控服务。原 Caddy 和 `vmiss-public-status.timer` 保留。旧 `/var/lib/vps-stock-monitor/http-status.json` 已不再读取，可自行归档；新导出器不再合并或补造另外两家的记录。

## 免费额度

每天约 480 次 Cron，每轮两次商家 HTTP 请求、一次 KV 读取、**一次合并 KV 写入**：约 480 writes/day。不会为每个商家分别写 KV，也不会为每次页面刷新写 KV。

KV Free 当前为每日 1,000 writes、100,000 reads（账号共享）；一个持续打开的页面每天约 2,880 次 Worker API 读取。低流量自用通常在免费额度内，访问量和同账号其它服务也会占用额度。本项目不自动升级套餐、不启用额外付费产品。

官方说明：[KV 限额](https://developers.cloudflare.com/kv/platform/limits/)、[KV 自动创建配置](https://developers.cloudflare.com/workers/wrangler/configuration/#automatic-provisioning)、[Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)。
