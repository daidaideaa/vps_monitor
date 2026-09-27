# 东京 VPS 库存、链路与故障记录

- [公开库存](https://daidaideaa.github.io/vps_monitor/) / [公开链路](https://daidaideaa.github.io/vps_monitor/network.html)
- [部署与回滚](docs/observability.md)

## 低消耗架构

库存监听、公开链路 API、SQLite 和邮件集中部署在 VMISS。Windows 每 120 秒探测两台 VPS；异常后仅快速复核两次，客户端日志增量读取。Komari 和第二台 VPS 上的监控服务暂停。Cloudflare 提供静态入口和 60～300 秒的只读缓存，但不再承担采样/库存存储，不启用付费功能。

链路概览免登录，服务端按白名单输出公开指标，不包含 IP、凭据、订阅、原始日志或邮件正文。页面显示延迟、确认异常次数、资源、流量估计与故障摘要；隐藏后暂停刷新。断连不逐次通知，每天 09:00 发邮件日报，缺测不算丢包。

## 库存与优惠

| 商家 | 套餐 | Telegram 来源 |
|---|---|---|
| VMISS | JP.TKY.TRI.Basic | HostMonit、VMISS 公告及群管理员 |
| GreenCloud | CN Premium Mini（Tokyo） | HostMonit |
| DMIT | TYO.Pro.TINY | 全球 VPS 余量监控、DMIT 活动推送 |
| GoMami | JPN.Pulse.Nano | GCPCN、GoMami 群管理员 |

独立会话监听新消息及编辑；首次历史静默建立基线，重复消息去重。补货和有货期间的新适用优惠码即时发邮件，不等待日报。监听状态与库存时间独立，频道记录超过 30 分钟仍显示待确认；规则不完整不推算价格。

## 代码与隐私

observatory 包含低频采集、VMISS API、日报和频道监听；network.html 只读取 /api/network 下的公开摘要。原验证码及原始诊断读取 API 已停用，详细证据通过服务器或本机查看。旧 link-gateway 停止写入，status-gateway 只作兼容跳转。静态构建仅包含 index.html、network.html 和 assets，私有凭据、会话、样本和报告不入库。

## 离线测试

```sh
python -m unittest discover -s tests -p 'test_*.py'
node --experimental-loader ./tests/html-loader.mjs --test tests/link-gateway.test.mjs tests/gateway.test.mjs tests/frontend.test.cjs
node scripts/build_public.mjs
```

Node 24 用于 SQLite 集成测试。Linux 实际 HY2 故障演练说明见部署文档；不要为了测试重启日常代理。
