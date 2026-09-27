# 部署、授权与回滚

## 当前运行架构

公开站点有库存、链路、故障记录三个入口。库存读取 `jp-vps-status.daidaidefish.workers.dev/status.json`；链路进入独立 `vps-link-monitor.daidaidefish.workers.dev/network`。两套 Worker 的存储、令牌、接口互不共用。

每个来源有独立 Durable Object 和随机上报令牌。`POST /ingest` 仅允许自身 source，样本 ID 为 source + boot_id + seq；采样时间和接收时间分开保存。乱序补传不会倒退 latest。云端原始采样保留 7 天，分钟汇总和证据 30 天；容量超限裁剪并返回告警。较长时间范围返回 5/30 分钟聚合，避免曲线被条数上限截断。磁盘本地缓存设 80/100/20 MiB（Windows/VMISS/家宽）的有效载荷预算，SQLite 索引和 WAL 有额外开销；每分钟清理。

Windows 失去物理默认路由时不把缺测算成丢包。网卡、地址、DNS 变化会重建对应独立探针，记录新的启动标识；Power/WLAN 事件进入故障证据。单纯采集间隔只标记“休眠或探针暂停，待确认”，不冒充已经确认休眠。

## 一次性授权

### Cloudflare Access

当前账号需要先激活 Zero Trust Free。激活页面要求接受条款，并授权超过免费额度后的付款；需账户所有者自行决定。未完成时所有链路读取返回 401，采集上传可继续。

激活后创建 Self-hosted 应用，域名 `vps-link-monitor.daidaidefish.workers.dev`、路径 `/network`，邮箱策略只允许账户所有者，启用一次性邮件验证码。不要把 `/ingest` 放入浏览器登录策略。将应用 AUD、team domain 写入 Worker 的 `ACCESS_AUD`、`ACCESS_TEAM_DOMAIN`；后者格式为 `team.cloudflareaccess.com`。`ALLOWED_EMAIL` 是唯一被允许的邮箱。

Worker 对所有 GET 独立验证签名、issuer、audience、email、expiry；隐藏或替代域名仍需 JWT。预览 URL 已禁用。完成后验收真实邮箱登录及退出后 401；没有完成登录验收不能标记此项通过。[Cloudflare 官方说明](https://developers.cloudflare.com/workers/configuration/cloudflare-access/)

### Telegram

在 `my.telegram.org/apps` 获取自己的 API ID / API Hash。执行 `vps_build/observability/authorize-telegram.cmd`，在本机终端输入凭据、手机号、验证码和需要的两步验证密码。不要写入聊天或仓库。此程序建立独立 Telethon 会话和通知机器人，不读取 Desktop 的会话文件。

然后在 `vps_build` 执行 `python observability/deploy_stock.py` 部署会话并启动 VMISS 的 `vps-stock-telegram.service`。默认历史基线静默，消息编辑会修正售罄状态，超过 30 分钟不当作当前库存。每个通知渠道独立重试；真实消息到通知提交的 10 秒目标须在授权后验收。[Telegram API](https://core.telegram.org/api/obtaining_api_id)

### Windows Packet Monitor

执行 `vps_build/observability/enable-pktmon.cmd` 并处理 UAC。系统驱动需要管理员权限，当前普通探针无法提供内部丢弃原因。脚本仅开启限定 VPS UDP 的 counters-only 计数，不保存包内容；若已有其他 PktMon 会话则停止启动并报告。Linux 使用内核 BPF 限定目标 UDP，持久化头部元数据及计数。[微软说明](https://learn.microsoft.com/en-us/windows-server/networking/technologies/pktmon/pktmon)

## 部署顺序

1. 从仓库根目录执行 `wrangler deploy --config status-gateway/wrangler.jsonc`，保留现有 PUBLISH_TOKEN。
2. `wrangler deploy --config link-gateway/wrangler.jsonc`；通过 secret bulk 私下配置 PROBE_TOKENS 和 KOMARI_URL，Access 配置完成前保持拒绝读取。
3. `wrangler deploy --config wrangler.jsonc`。构建白名单只有 index.html、assets；不要直接上传整个仓库目录。
4. 在 `vps_build` 执行 `observability/deploy_probes.py generate`、`vmiss --collector-only`、`home --collector-only`。Windows 用 `restart-windows.ps1` 重启自身探针。首次安装用不带 collector-only 的命令与 install-windows.ps1。
5. `verify.py` 检查三个来源上传、原业务服务状态及未登录拒绝读取。更改通知配置后需重新生成并分发 probe.json。

以上命令使用现有 Python venv、Node 24 和已授权 Wrangler。Komari 固定 server 1.5.1、agent 1.5.11，校验官方发布资产的 SHA256；禁用自动更新和 Web SSH。服务器 loopback 后置独立 HTTPS 入口，启用私有站点；管理员凭据只在忽略目录和服务器 0600 文件。

## 验收和边界

`python -m observatory.drill --binary /path/to/hysteria --output /private/path/report.json` 在 Linux 上创建临时回环服务、证书、HY2 进程与 UDP 黑洞，模拟真实网络 I/O。证据时钟加速模拟 10+5 分钟；不能把它当作 24 小时实测。生产代理无须停机。

日常告警：首次失败触发 10 分钟历史冻结和 5 分钟尾部采集，每目标 10 分钟冷却；连续 3 次失败、2 次成功分别入队告警与恢复。Windows 故障通过各节点独立上报通道请求服务端关联证据。历史故障收到的取证请求只能补取日志，无法补造当时 UDP 元数据。服务器丢弃、OOM、出口 DNS 等保留事实，未能确认的层面标记待定位。

Linux UDP 采集 `capture_version: 2` 使用 ETH_P_ALL 接收发送方向副本，再由内核 BPF 限定 IPv4 UDP 与目标，方向采用内核 sockaddr_ll.packet_type。此前没有版本标记的采集会漏记发送方向，不能用来证明服务器未发送响应。新增回环回归验证双向记录、目标过滤与无载荷持久化；Linux CI 使用 CAP_NET_RAW 所需权限运行。[Linux packet 接口说明](https://man7.org/linux/man-pages/man7/packet.7.html)

Windows 使用同一 Gmail 服务的 SSL 465 端口，VPS 使用 STARTTLS 587，均校验证书。电脑完全离线时本地队列待恢复发送。当前客户端没有选择 VMISS 时，页面明确列出真实订阅及选中节点。

仍需真实用户操作验收：Access 登录、Telegram 会话与实时送达、管理员 PktMon、Windows 重启/休眠及实际 TUN/订阅切换。避免为验收打断日常代理或重启用户电脑。

## 回滚

- Cloudflare 使用 `wrangler rollback <previous-version-id> --config <对应配置>`，先查看部署历史。私有 Worker 回滚仍须保持 JWT 验证，不得撤掉 Access 后公开数据。
- 停止 `vps-observatory.service`、`vps-observatory-hy2-*.service`、`komari-agent.service` 仅影响监控；不要停止原来的 HY2 业务服务。
- Windows 停止任务 VPS Observatory，使用 stop-supervisor 标记让它结束自己的子进程；不按名称批量结束所有 mihomo。
- 停止 `vps-stock-telegram` 后可参考家宽 `/var/lib/vps-observatory/rollback/legacy-timers.txt` 恢复旧调度。旧数据/profile 保留。v3 会拒绝旧 v2 发布器，回滚旧库存必须同步回滚页面和公开 Worker，而非强行覆盖状态。
- Komari 使用独立 9443 Nginx 配置，回滚只移除该监控配置并校验 nginx 配置；443 订阅不受影响。

## 24 小时复核

以最终部署时间为起点，次日检查三个来源采样覆盖、上传积压、磁盘裁剪、服务重启、事件取证完整性、通知投递与未登录拒绝读取。报告保存本机私有目录，公开仓库只提交不含真实链路数据的实现和演练摘要。授权未完成的验收项目需明确列出，不能判为全量通过。

Cloudflare 公开站点的 Git 构建已设为根目录 `/`、生产分支 `main`，使用根目录 wrangler.jsonc 中的公开文件白名单。24 小时复核已设置为本聊天每小时检查一次，2026-09-28 13:35（北京时间）之后输出报告并停止复核。本地定时任务需电脑开机、桌面应用运行；[官方说明](https://learn.chatgpt.com/docs/automations?surface=app)。
