# 东京 VPS 库存、链路与故障记录

- [公开库存](https://daidaideaa.github.io/vps_monitor/) / [Cloudflare 入口](https://vps-monitor.daidaidefish.workers.dev/)
- [私有链路与故障记录](https://vps-link-monitor.daidaidefish.workers.dev/network)：Cloudflare Access 邮箱登录。Access 尚未配置时返回 401，不返回任何链路数据。
- [部署与回滚](docs/observability.md) · [演练报告](docs/drill-2026-09-27.md)

## 库存来源

| 商家 | 套餐 | 频道 |
|---|---|---|
| VMISS | JP.TKY.TRI.Basic | HostMonit |
| GreenCloud | CN Premium Mini（Tokyo） | HostMonit |
| DMIT | TYO.Pro.TINY | 全球 VPS 余量监控 |
| GoMami | JPN.Pulse.Nano | GCPCN |

独立 Telethon 会话订阅新消息及编辑，原始消息时间超过 30 分钟则显示当前待确认。首次历史基线静默建立；按频道、消息 ID、编辑时间和内容去重。补货与有货期间的新优惠码通过机器人及邮件分别重试发送。优惠规则不完整或币种不明确时不计算折后价。

**部署状态（2026-09-27）：** 四款公开接口已发布，Telegram 仍等待用户在本机完成 API ID / API Hash 和独立账号授权，当前库存诚实显示待确认。旧 ZgoCloud、RFCHOST 调度停止，历史保留。

## 链路

Windows、VMISS、日本家宽独立向 Cloudflare Durable Objects 上报。页面每 5 秒读取；正常网络/资源每 5 秒、HY2 每 15 秒、独立新连接每 5 分钟采样。Windows 的探针关闭 TUN 并绑定物理网卡，实际 Clash Party 会话单独记录订阅、选择节点、TUN、PID 与监听状态。

首次失败保存之前 10 分钟及之后至少 5 分钟；异常期间网络每秒、HY2 每 5 秒，最多 5 分钟。连续 3 次失败告警、2 次成功恢复。故障报告区分事实、推断和缺失证据。ICMP 不通、单次超时或一侧日志不能单独证明运营商故障。

链路最新状态、历史、证据和 HTML 全部验证 Access JWT；每个探针令牌只能上报自身数据。公开库存 API 使用字段白名单，不返回链路数据。预览 URL 关闭。

## 仓库结构

- `observatory/`：探针、事件解析、通知、缓存、故障证据和独立演练。
- `link-gateway/`：受 Access 保护的原生页面、私有接口及每来源独立 Durable Object。
- `status-gateway/`：公开 schema v3 库存接口；兼容切换期 v2，已切 v3 后拒绝旧发布器覆盖。
- `wrangler.jsonc`、`scripts/build_public.mjs`：公开静态站点，只发布 index.html 和 assets。
- `tests/`：解析、回放、通知重试、Access、SQLite 补传及页面验证。
- `server/`、`vmiss-stock-monitor/`：保留的旧采集实现；[旧文档](docs/legacy-inventory.md)仅供查阅。

节点安装、Windows 自启动、Komari 配置和部署工具在 `vps_build/observability/`。密钥、会话和原始诊断日志只存放在忽略目录及服务器私有目录。

## 离线测试

```sh
python -m unittest discover -s tests -p 'test_*.py'
node --experimental-loader ./tests/html-loader.mjs --test tests/link-gateway.test.mjs tests/gateway.test.mjs tests/frontend.test.cjs
node scripts/build_public.mjs
```

Node 24 用于 SQLite 集成测试。Linux 实际 HY2 故障演练说明见部署文档；不要为了测试重启日常代理。
