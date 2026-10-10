> **现行配置说明（2026-10-10）**：VMISS 日常主链路为 VLESS TCP TLS Vision，HY2 备用；Windows / VMISS 常规采样与上报均为 300 秒，独立新会话测试关闭。此文描述代码与部署目标，实际服务状态和 24 小时覆盖率需要实时验收。流量计算见 [VLESS 与流量预算](vless-and-budget.md)，单次年付订单见 [VMISS 年付订单](vmiss-reservation.md)。

# VMISS 低消耗监控：部署与回滚

## 当前架构

GitHub Pages 与可选 Cloudflare 静态入口仅提供公开页面。库存、公开摘要 API、SQLite 与邮件队列集中在 VMISS；Windows 只主动探测 VMISS，保留网关、国内及日常代理作为对照。日本家宽目标已停用，历史私下保留；其监控服务与 Komari 均暂停。

链路页只显示 VMISS。最近 24 小时图表按实际已有记录的时间段展开，ICMP、TCP 和当前主协议的独立探针（VLESS；历史为 HY2）分行展示；新旧协议分别记录，不将旧 HY2 故障冒充新 VLESS 故障。失败标记、缺测断线、单独成功点与确认异常只按对应 VMISS 目标统计，不混入已停用家宽目标或日常客户端。原始取证及日志保留策略不变。

- 公开读取入口：`https://vps-monitor.daidaidefish.workers.dev/api/status` 和 `/api/network/latest`、`history`、`incidents`。GitHub Pages 与 Worker 静态页面均免登录。
- VMISS 只通过 `/monitor/public/latest`、`history`、`incidents` 构造白名单摘要：时间、数值型指标、固定状态枚举和通用故障结论。原始网络清单、IP、订阅/节点名称、自由文本日志、邮件正文和凭据均不进入公开 API。
- 正式配置 `public_dashboard=true`：原 `/auth/*` 和 `/api/*` 详细诊断接口返回 410。原始证据继续保留在本机和 VMISS，通过已授权 SSH 排查。
- Worker 的 `MONITOR_ORIGIN` 是私有 secret，仅指向有有效 TLS 证书的只读回源服务；不转发重定向、错误正文或上游响应头。旧公开代码曾使用直接 IP 地址；当前页面、资产及兼容跳转均改用 Worker，Git 历史没有重写。
- 上报：`POST /monitor/ingest`，每来源独立令牌，仅能写入自己来源；gzip 请求有压缩及解压大小上限。source/boot_id/seq 去重，采样和接收时间分离，最新结果按采样时间选择。
- 上报仍需要每来源独立令牌。公开库存不含链路数据；公开链路仅含白名单摘要。任何原始诊断数据都不写入静态资产。

Cloudflare 私有 Worker 保持 MONITORING_PAUSED；库存 Worker 设置 STORAGE_RETIRED，只返回重定向或停止响应，不访问 DO。不启用付费套餐或需要付款授权的功能。

## 采样与通知

Windows 与 VMISS 的常规网络、资源、服务和 HTTPS 检查按 **300 秒**配置，上报正常间隔也为 **300 秒**；上传失败最多退避到 900 秒。本地 Windows 日志仍按约 5 秒增量读取，额外新连接（cold）探针 **关闭**，不是每 1800 秒自动执行。网页可见时每 60 秒读取摘要，隐藏时暂停；历史每 5 分钟读取一次、以 5 分钟桶汇总。Worker 库存缓存 60 秒、网络最新状态/故障缓存 120 秒、历史缓存 300 秒。Cloudflare 不承担 DO / KV / 定时写入或付费绑定。

首次失败后最多间隔 5 秒复核两次，连续 3 次实际失败确认异常；恢复需两次成功。同一指标持续异常只建一个事件，快速失败复核每目标冷却 600 秒。当前日常客户端与 **独立 VLESS 探针**分别记录，旧 HY2 记录保持原名；休眠/无物理路由/上传失败不计作丢包，快速复核不纳入常规成功率。

链路无即时邮件或 Telegram 推送。VMISS 每天北京时间 09:00 发送前一天日报，按日期去重、失败重试；离线期间日报延后生成。旧断连及 Telegram 队列标记取消，避免集中补发。

Telegram 独立会话在 VMISS 监听四款目标，源自 [targets.py](../observatory/targets.py)：VMISS 的 `JP.TKY.TRI.Basic` 由 `@hostmonit`、`@vmisstz`、`@vmiss_com`、`@vmisscom` 四个来源提供线索；其中客户群只采信管理员或群自身的非转发消息，`@vmisstz` 并非已核实的官方频道。首次历史和一次性精确搜索静默建基线，消息编辑与补拉去重。补货和新的适用优惠码即时邮件，正常网络下目标为收到事件后 10 秒内提交 SMTP，但不保证频道发现与邮箱入箱时限。心跳与库存事件时间分开展示，来源消息超过 30 分钟进入待确认。

## 节省流量与故障空间

不测速、不重复查询商家、不持续抓包或执行 traceroute。保留原日志来源和时间、少量错误片段、状态变化摘要；前 10 分钟/后 5 分钟采样引用既有记录，不在每份故障里复制。VMISS 链路异常最多在首次和窗口结束时各提取一次服务端日志摘要，并引用同窗服务端采样。摘要保留 30 天，原始采样 7 天，汇总 30 天；原始证据只在本机/服务器查看，保留期以外可能已裁剪。

VMISS 活跃数据预算 150 MiB：hub 100 MiB、探针 20 MiB、库存 20 MiB、通知 10 MiB；故障摘要有效载荷合计最多 10 MiB，超过按旧记录裁剪。Windows 数据库预算 40 MiB，独立内核日志轮转；初次回滚备份和家宽冻结历史单独保留在私有备份目录，不参与活动数据增长。

所有 VMISS 新后台服务归 vps-monitoring.slice，CPUQuota=20%、MemoryHigh=280M、MemoryMax=320M。监控流量以 **1 GB/月以内**为目标：上传有效载荷实计、主动探测与保活按模型估算，页面明确不是运营商账单。预算降级逻辑保留，但当前额外新会话测试已关闭、常规间隔已设为 300 秒，**不能**把“未来可能降到 300 秒”当成尚未实施的步骤。实际覆盖不足 24 小时不据此声称长期稳定；库存监听不随探针降频。

## 部署

应用代码在本仓库 observatory，安装器在私有 vps_build/observability/deploy_low_usage.py。

1. `stage`：创建新目录、API、资源限制和 Nginx 的 /monitor/ 路径，保留订阅配置；写入新低频配置，尚不启动探针。
2. 验证公开接口白名单及隐藏源地址；原详细接口和验证码均返回 410，任意 query/path 不得扩展 Worker 读取范围。
3. `telegram`：复制已授权且本机已断开的会话，启动唯一监听实例。
4. `collector`：启动 VMISS 低频探针；最后启用 Windows VPS Observatory 任务，管理进程为 low_collector 与独立 **VLESS** 探针，旧 HY2 / JP-Home 独立探针与 Komari 不恢复。
5. 更新公开页面、退休库存 Worker；Cloudflare 高写入模式始终保持关闭。

VMISS 代码 /opt/vps-observatory-low，配置 /etc/vps-observatory-low（私有），活动数据 /var/lib/vps-observatory-low；服务 vps-monitor-api、vps-monitor-collector、vps-stock-telegram。Nginx 只反代 loopback 18790，沿用已有 IP 证书和自动续期。原 vmiss-hy2、vmiss-hy2-ios 与订阅服务不改动。

## 2026-10-09 新增：单次年付订单服务

`vps-vmiss-reservation` 与低频监控三项服务独立，由私有仓库 `vps_build/observability/deploy_vmiss_reservation.py` 管理，仅 `probe`、`install`、`status` 固定操作；针对 `JP.TKY.TRI.Basic` 一台 Debian 12 年付。只有初始官网检查和新有效补货消息会触发额外官网访问，不进行定时商品网页轮询；重复订单防护、144 CAD 总额上限及人工支付宝结算见 [VMISS 年付订单](vmiss-reservation.md)。**不要**在日常监控 `update`、回滚或重试中重新安装/重新触发订单服务。2026-10-09 的历史验证为目标缺货，当前订单状态只能只读检查。

## 回滚与验证

只停止三个新监控服务及 Windows 的本监控任务，恢复网站暂停说明。不要启动旧 collector、Komari 或 Cloudflare 写入。所有数据库、会话及原故障日志留在私有目录。不得按进程名批量结束 mihomo。

测试覆盖 OTP 单次使用/限流/退出、匿名和探针读取拒绝、来源伪造、离线补传/重复/乱序、缺测和常规统计、快速确认/恢复、单故障不重复复制证据、库存邮件与积压取消、已退休 Worker 不访问 DO。免登录摘要及防泄露、实际频道补货 10 秒、24 小时覆盖分别验收，未完成不得标为通过。旧邮箱登录流程已按用户要求停用。

## 频道来源与历史解释

除 HostMonit、vps_spiders、GCPCN，现接入账号已有的 vmiss_com、dmitnews，以及 vmisscom 和 GoMamiNetworks。客户群仅接受管理员或群自身发布的原始消息，排除转发和普通聊天；管理员名单每小时刷新，获取失败时停止采信该群。精确套餐和地区匹配继续生效，系列优惠不能代表 Basic 等具体规格有货。

2026-09-29 新增用户指定的 `vmisstz`（VMISS 补货通知频道），使 VMISS 合计有上述四个来源。非商家公告来源需匹配 JP.TKY.TRI.Basic 及 VMISS 商品链接；不能只因频道名称就将其视为已验证的官方公告。首次历史静默建基线，新消息与售罄编辑沿用原监听及跨来源状态去重，不增加周期网页抓取。

GreenCloud 基线改按商品编号 2213 搜索，避免被 Singapore Mini 挤占。库存页区分“上次报告结论”和“当前待确认”，不把读取旧消息改成当前证据。历史优惠码显示适用周期、续费、有效期和来源条款，超过 30 天的未注明过期优惠提示当前有效性待确认。
