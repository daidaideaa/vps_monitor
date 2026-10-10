# 东京 VPS：库存、链路监控与单次年付订单

> **代码说明更新：2026-10-10。** 本仓库记录的是部署设计与离线验证，不等于 VMISS 当前在线、库存真实有货、邮件已送达或订单已完成。线上状态应以带采样时间的只读 API / 授权 SSH / VMISS 官网账单为准。

- [库存与优惠页面](https://daidaideaa.github.io/vps_monitor/)
- [VMISS 网络与资源监控](https://daidaideaa.github.io/vps_monitor/network.html)
- [当前部署与回滚](docs/observability.md) · [VLESS 与流量预算](docs/vless-and-budget.md) · [VMISS 年付订单](docs/vmiss-reservation.md)
- [关联私有部署仓库](https://github.com/daidaideaa/vps_build)（服务器 SSH / VLESS / HY2 / 订阅生成）

## 当前低消耗架构

库存 Telegram 监听、公开链路 API、SQLite 和邮件部署在 **VMISS**；Windows 独立探针仅监控 VMISS 和本地网关/国内/日常客户端对照。**VMISS 与 Windows 常规采样、批量上报配置为 300 秒**，失败后至多每隔 5 秒复核两次；额外独立新会话测试已关闭。Windows 关闭时其采样缺测，不视为丢包；VMISS 服务器端采集、库存监听、日报不依赖 Windows 常开。

2026-09-27 日常 mihomo 主链路迁至 **VLESS TCP TLS Vision（`VMISS-JP-VLESS`）**，HY2 备用；历史 HY2 与新 VLESS 独立统计。日本家宽（JP-Home）监控、Komari、旧高频 collector 以及 Cloudflare Durable Objects 写入已暂停，不能因排障而直接恢复旧部署。公共链路页根据可用数据展示 ICMP、TCP、VLESS（或尚未迁移时的 HY2）延迟、异常、VMISS CPU/内存、流量预算及数据新鲜度。

Cloudflare Worker / Pages 只提供静态页面和 **60～300 秒缓存的白名单只读摘要**，不暴露原始 IP、认证、订阅令牌、自由文本日志、私人账单或 Telegram 会话；原验证码/详细诊断 API 已停用。公开 API：`https://vps-monitor.daidaidefish.workers.dev/api/status`、`/api/network/latest`、`/api/network/history`、`/api/network/incidents`。详细证据留在服务器或本机。每天北京时间 09:00 邮件日报；网络断连不逐次发信，库存新事件邮件不等待日报。

## 库存与优惠：精确 Telegram 来源

以下是 [`observatory/targets.py`](observatory/targets.py) **当前允许的来源**，并非对商家官网实时库存的保证。

| 商家 | 精确套餐 | 允许的 Telegram 来源 |
| --- | --- | --- |
| VMISS | `JP.TKY.TRI.Basic` | `@hostmonit`、`@vmisstz`、`@vmiss_com`、`@vmisscom` |
| GreenCloud | CN Premium Mini (Tokyo)（PID 2213） | `@hostmonit` |
| DMIT | `TYO.Pro.TINY` | `@vps_spiders`、`@dmitnews` |
| GoMami | `JPN.Pulse.Nano` | `@gcpcn`、`@gomaminetworks` |

`@vmisscom`、`@gomaminetworks` 属于客户群，仅采信管理员或群自身发布的非转发消息；`@vmisstz` 于 2026-09-29 增加，不能仅凭名称认定为已核实的官方频道。解析必须同时满足来源白名单、精确套餐/链接等限制；新消息和编辑可处理，历史基线只记录而不通知。频道消息超过 30 分钟仅保留上次报告结论，当前库存转为待确认；`unknown` 不冒充售罄。实时会话是否成功连接，必须读取 collector 心跳。

## 2026-10-09 增加：VMISS 单次年付锁单

独立 `vps-vmiss-reservation` 服务仅针对 **一台 `JP.TKY.TRI.Basic`、Debian 12、年付**。初次启动可核对官网一次，之后只响应有效的新补货信号核对官网；不增加定时网页轮询。优惠码在官网结算验证，先使用账户余额，剩余金额留给用户手动支付宝支付；抵扣前总额超过 **144 CAD**、页面/套餐异常或已有目标账单/有效服务会停止。提交前持久记录防重复；提交不明只核对账单，不盲目重试。详见 [订单规则、隐私与只读状态查询](docs/vmiss-reservation.md)。

2026-10-09 历史检查结果为目标套餐缺货，同系列测试仅完成预览，**不是今天的库存或订单状态**；不要将该自动锁单服务视为会自动完成支付宝支付。

## 当前代码与历史模块

- `observatory/`：VMISS / Windows 低频采集、白名单 API、库存来源/解析、通知、VLESS 分开统计与订单逻辑。
- `assets/network.js`、`network.html`：公开 VMISS 状态页，缺测与失败分别展示。
- [`vmiss-stock-monitor/`](vmiss-stock-monitor/README.md)、[`docs/legacy-inventory.md`](docs/legacy-inventory.md)：**历史独立 Playwright/JP-Home 库存方案**，仅留测试和迁移记录，不是当前生产部署入口。
- `status-gateway/`、`link-gateway/`：旧网关兼容/归档；不要恢复旧 Cloudflare DO 写入。

## 验证与发布

```sh
python -m unittest discover -s tests -p 'test_*.py'
node --experimental-loader ./tests/html-loader.mjs --test tests/link-gateway.test.mjs tests/gateway.test.mjs tests/frontend.test.cjs tests/public-edge.test.mjs tests/network.test.cjs
node scripts/build_public.mjs
```

CI 还在 Linux 上验证旧 Playwright 离线 fixture 等兼容测试。**CI 和 Pages 绿色只证明代码/构建成功，不是线上 VPS、邮箱或 Telegram 的实时验收。** 不将 `.env`、`.local/`、Telegram session、库存数据库、代理配置、私人账单或令牌上传 Git。
