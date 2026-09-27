# VMISS 低消耗监控：部署与回滚

## 当前架构

GitHub Pages 与可选 Cloudflare 静态入口仅提供公开页面。库存、邮箱登录、私有链路 API、SQLite 与邮件队列集中在 VMISS；Windows 保留本机探测。日本家宽只作为 Windows 的暂时被测目标，其监控服务与 Komari 均暂停。

- 公开库存：`https://38.47.125.205/monitor/status.json`，schema_version 3。
- 私有页面：`network.html`。数据 API：`/monitor/api/latest`、`history`、`incidents`、`evidence`、`reports`。
- 登录：`POST /monitor/auth/request`、`verify`、`logout`。仅向私有配置中的本人邮箱发送验证码，10 分钟有效，每小时最多 5 次、最短间隔 60 秒、每验证码最多 5 次尝试；会话 8 小时，令牌只留在页面内存。
- 上报：`POST /monitor/ingest`，每来源独立令牌，仅能写入自己来源；gzip 请求有压缩及解压大小上限。source/boot_id/seq 去重，采样和接收时间分离，最新结果按采样时间选择。
- CORS 仅允许已配置的网站来源。私有读取需要用户会话，探针令牌不能读取。公开库存不含链路数据。HTML 是公开空壳，私有数据从不写入静态文件。

Cloudflare 私有 Worker 保持 MONITORING_PAUSED；库存 Worker 设置 STORAGE_RETIRED，只返回重定向或停止响应，不访问 DO。不启用付费套餐或需要付款授权的功能。

## 采样与通知

常规网络、资源、服务和 HTTPS 每 120 秒；本地日志每 5 秒增量读取；独立新连接每 1800 秒。上报每 120 秒，失败退避至 900 秒。网页可见时每 60 秒读取、隐藏时暂停；曲线按 cursor 获取增量。

首次失败后间隔 5 秒复核两次，连续 3 次实际失败确认异常；首次成功后 5 秒复核恢复。同一指标持续异常只建一个事件；快速失败复核每目标冷却 600 秒。当前客户端与独立 HY2 分别标识，休眠/无物理路由/上传失败不计作丢包，快速复核不进入常规成功率。

链路无即时邮件或 Telegram 推送。VMISS 每天北京时间 09:00 发送前一天日报，按日期去重、失败重试；离线期间日报延后生成。旧断连及 Telegram 队列标记取消，避免集中补发。

Telegram 独立会话在 VMISS 监听四款目标。首次历史和一次性精确搜索静默建基线，消息编辑/补拉去重。补货和新的适用优惠码仅发邮件，正常网络目标为收到频道事件后 10 秒内提交 SMTP；不保证来源频道发现或邮箱入箱延迟。监听心跳与库存消息时间分别显示，旧消息超过 30 分钟仍为待确认。

## 节省流量与故障空间

不测速、不重复查询商家、不持续抓包或执行 traceroute。保留原日志来源和时间、少量错误片段、状态变化摘要；前 10 分钟/后 5 分钟采样引用既有记录，不在每份故障里复制。VMISS 链路异常最多在首次和窗口结束时各提取一次服务端日志摘要，并引用同窗服务端采样。摘要保留 30 天，原始采样 7 天，汇总 30 天；历史取证下载说明原始记录可能已裁剪。

VMISS 活跃数据预算 150 MiB：hub 100 MiB、探针 20 MiB、库存 20 MiB、通知 10 MiB；故障摘要有效载荷合计最多 10 MiB，超过按旧记录裁剪。Windows 数据库预算 40 MiB，独立内核日志轮转；初次回滚备份和家宽冻结历史单独保留在私有备份目录，不参与活动数据增长。

所有 VMISS 新后台服务归 vps-monitoring.slice，CPUQuota=20%、MemoryHigh=280M、MemoryMax=320M。流量预算以 1GB/月为目标：上传有效载荷实计，主动探测及保活采用估算额度，页面明确不是运营商账单。全来源合计超过预算时每 24 小时最多降一级：先关闭额外新会话测试，再把常规间隔降到 300 秒。实际运行覆盖不足 24 小时不据此做降级结论。库存监听不降频。

## 部署

应用代码在本仓库 observatory，安装器在私有 vps_build/observability/deploy_low_usage.py。

1. `stage`：创建新目录、API、资源限制和 Nginx 的 /monitor/ 路径，保留订阅配置；写入新低频配置，尚不启动探针。
2. 验证公开接口、跨域白名单、所有私有接口未登录为 401。
3. `telegram`：复制已授权且本机已断开的会话，启动唯一监听实例。
4. `collector`：启动 VMISS 低频探针；最后启用 Windows VPS Observatory 任务，管理的进程改为 low_collector 和独立 HY2，无 Komari。
5. 更新公开页面、退休库存 Worker；Cloudflare 高写入模式始终保持关闭。

VMISS 代码 /opt/vps-observatory-low，配置 /etc/vps-observatory-low（私有），活动数据 /var/lib/vps-observatory-low；服务 vps-monitor-api、vps-monitor-collector、vps-stock-telegram。Nginx 只反代 loopback 18790，沿用已有 IP 证书和自动续期。原 vmiss-hy2、vmiss-hy2-ios 与订阅服务不改动。

## 回滚与验证

只停止三个新监控服务及 Windows 的本监控任务，恢复网站暂停说明。不要启动旧 collector、Komari 或 Cloudflare 写入。所有数据库、会话及原故障日志留在私有目录。不得按进程名批量结束 mihomo。

测试覆盖 OTP 单次使用/限流/退出、匿名和探针读取拒绝、来源伪造、离线补传/重复/乱序、缺测和常规统计、快速确认/恢复、单故障不重复复制证据、库存邮件与积压取消、已退休 Worker 不访问 DO。真实邮箱登录、实际频道补货 10 秒、24 小时覆盖分别验收，未完成不得标为通过。
