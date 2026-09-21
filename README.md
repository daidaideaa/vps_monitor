# 东京 VPS 库存观察

页面：[GitHub Pages](https://daidaideaa.github.io/vps_monitor/)。VMISS、ZgoCloud、RFCHOST、V.PS 四家五个套餐由香港 VPS 查询，无法确认时改用日本 VPS 备用出口复查；Windows 电脑无需常开。

> 迁移代码需要先部署到 VPS，再发布页面。仅更新仓库不会自动安装 VPS 服务或停用 Cloudflare 上已存在的 Cron。

| 套餐 | 检查位置 | 检查方式 |
| --- | --- | --- |
| VMISS `JP.TKY.TRI.Basic` | 香港 VPS | 原有 Playwright 服务，保留邮件提醒 |
| ZgoCloud `Tokyo Intel VPS · Starter` | 香港 VPS | HTTP，无法确认时使用 Playwright Chromium |
| RFCHOST `JP2-CO-Micro-Lite` | 香港 VPS | 完整 Chromium + 虚拟显示器，等待正常验证并复用独立会话 |
| V.PS `Tokyo Cloud KVM · Starter` | 香港 VPS | 官方订购页，套餐编号 148 |
| V.PS `Tokyo Cloud KVM · Essential` | 香港 VPS | 官方订购页，套餐编号 149 |

所有套餐每轮随机间隔 480～720 秒（8～12 分钟）。页面每 30 秒读取一次 [VPS 状态接口](https://vmiss-status.96-126-179-210.sslip.io/status.json)，浏览器页面不请求商家或 Cloudflare Worker，也不触发库存检查。

页面的“页面同步于”表示成功读取结果的时间；各套餐的“实际检查”才是商家库存检测时间。页面不显示秒级倒计时，“同步结果”按钮也不会增加商家请求。四家商户采用 2×2 布局，V.PS 的两个套餐并列显示独立状态，手机端自动单列。

页面采用暖白、炭灰和陶土色，主要数据直接展示，历史和库存数字保留在“检查详情”。样式集中在 `assets/site.css`。英文和数字使用 Manrope；中文正文在 Apple 设备优先使用系统苹方，其他设备使用 MiSans，Noto Sans SC 为补充。标题和正文统一使用无衬线字形。MiSans 内部 Regular/Medium 的轴值分别为 330/380，通过字体声明映射到界面的 400/500 字重，避免中文比英文过粗。字体均随站点提供，运行时不依赖外部字体 CDN。字体与许可在 `assets/fonts/`，合计约 933 KiB，浏览器按字符范围加载所需文件并缓存。MiSans 使用小米官方发布的原始网页分片，不改动字体二进制；其他字体使用开放许可的字符子集。增加中文文案后可在本地安装 `fonttools brotli`，运行 `python scripts/build_fonts.py` 重新生成。未收录的新字符会使用系统字体，仍可正常显示。原始下载文件和构建依赖只保存在已忽略的 `.local/`，无需安装到 VPS。

## 结构

```text
index.html                        2×2 商户卡片，V.PS 两个套餐合并展示，仅读取 VPS API
server/stock_targets.py            ZgoCloud / RFCHOST / V.PS 官方套餐地址
server/vps_stock_monitor.py        四个套餐查询、解析与独立持久化
server/vps-stock-monitor.*         随机 8～12 分钟运行一次的 systemd 服务与定时器
server/export_status.py            合并五个套餐，白名单导出 schema v2
server/vmiss-public-status.*       每 30 秒导出公开状态
server/Caddyfile                   原有 HTTPS 接口配置
tests/                            解析、状态历史、单数据源页面测试
worker/                           旧 Worker，定时配置已清空，保留代码与 KV
```

四个新增套餐按顺序检查以限制内存使用，HTTP 已确认库存时不启动浏览器。浏览器复用 VMISS 已安装的 Chromium，但使用独立持久会话，不读写 VMISS 的浏览器配置。邮件复用 VMISS `.env` 内的 SMTP 配置，不复制凭据。验证页、访问失败、套餐边界不明、库存标记冲突都返回 `unknown`。

V.PS 使用官网链接的实际订购页，分别核对选中位置 Tokyo、套餐编号 148/149 及对应 Starter/Essential 标题；卡片前置 Out of stock 和 outofstock 类优先判无货。仅当目标卡片唯一且选中、订单按钮启用时确认有货，不能用 Premium 的选中状态或营销页 Order 链接代替。页面加载的未激活验证码脚本不等于验证拦截；真实验证组件、验证提示和 HTTP 403 仍为 unknown。

只识别 ZgoCloud 的 Starter 到 Standard、RFCHOST 的 Micro-Lite 到 Mini-Lite 范围，不能用其它套餐的库存代替。每检查完一个套餐立即原子保存；另一个套餐失败不丢失已有结果。服务限制单次运行 470 秒，不会重叠执行。

## 在已有香港 VPS 上部署

前提：已有 `/opt/vmiss-stock-monitor/.venv`、Playwright Chromium、`vmiss-monitor` 用户和 Caddy。现有 VMISS 服务继续运行，需同步本地 VMISS 项目的日本回退版本；安装脚本将其 `.env` 中的基准间隔设为 600 秒，并配置日本备用出口。

先将本地 VMISS 项目的最新版 `monitor.py` 同步至 `/opt/vmiss-stock-monitor/`。仓库也提供对应的 `server/vmiss-japan-fallback.patch`；旧版本可先备份并用 `patch --dry-run -p1 < vmiss-japan-fallback.patch` 检查匹配后应用，已更新版本不要重复应用。

将本仓库 `server/` 上传到 VPS 的临时目录（例如 `/tmp/vps-monitor-server`），以 root 执行：

```bash
bash /tmp/vps-monitor-server/install.sh
```

安装程序备份现有公开导出文件和状态、停止旧的 `http-stock-monitor` 定时器，安装新的四个套餐查询服务，执行首轮查询并启动定时器，最后恢复五个套餐合并导出。不覆盖 Caddy 配置；为应用新间隔会重启 VMISS 库存监控，保留状态和邮件去重记录。

检查全部套餐的来源和时间：

```bash
systemctl status vps-stock-monitor.timer vmiss-public-status.timer vmiss-stock-monitor
curl -fsS https://vmiss-status.96-126-179-210.sslip.io/status.json
```

响应应为 `schema_version: 2`，包含五个套餐的 `products`、各自的 `last_checked` 以及 `query_location: hong-kong-vps`。正常执行但商家验证受阻仍可能是 `unknown`；这不代表有货或无货。旧时间会被页面标为过期，不会被导出时间刷新成新检查。

确认 VPS 输出后，再将页面提交到 GitHub Pages。使用部署旧 Worker 的 Cloudflare 账号，在控制台删除 `vps-monitor` 的 Cron Trigger。保留 Worker 和 KV，不删除历史数据。仅修改本地配置不能停止线上 Cron。

## 状态与时间记录

- `available` / `unavailable`：本轮已可靠确认；无货库存为 0。
- `unknown`：无法确认，不使用历史有货作为当前状态；保留最近确认结果和上次有货时间。
- 本轮无货起点在 unknown、断连及检查间断时保留，仅明确有货时清除；页面标为“含未确认时段”，不能据此断言整个期间一定无货。
- 超过 3 个检查间隔的记录为过期；接口断连显示连接异常。某家 unknown 不影响其它套餐。
- 公开 API 只导出允许字段，不发布邮箱、密码、cookies、页面 HTML 或原始错误详情。

VMISS 历史沿用上次公开快照；其它套餐的历史保存在 `/var/lib/vps-stock-monitor/http-status.json`。旧 Worker 的历史不冒充 VPS 的新检查。VMISS 邮件提醒保持原样；其它四个套餐同样启用到货及连续异常邮件提醒，发送至同一个 `MAIL_TO`。

## 维护

```bash
journalctl -u vps-stock-monitor.service -u vmiss-public-status.service --since today
sudo systemctl start vps-stock-monitor.service       # 手动检查一轮
sudo systemctl start vmiss-public-status.service     # 仅重新导出，不访问商家
sudo systemctl disable --now vps-stock-monitor.timer # 停止四个套餐定时检查
```

必要的本地验证：

```bash
python -m unittest discover -s tests -v
node --test tests/frontend.test.cjs
```

旧 RFCHOST GitHub Actions 浏览器探测仍是手动诊断，无定时计划，也不会写入线上状态。

## 邮件提醒

五个套餐均发送到 `/opt/vmiss-stock-monitor/.env` 中的 `MAIL_TO`。VMISS 保留原服务，其余套餐复用相同 SMTP 传输（587 STARTTLS 或 465 SSL、证书校验），不在本仓库保存凭据。

- 各套餐独立去重：首次确认有货发送一次，持续有货不重复；确认无货后再次有货重新提醒。
- unknown 不发送到货邮件，不清除已发标记；SMTP 失败不标记成功，下次仍确认有货时重试。
- 连续异常达到 `.env` 中 `ERROR_ALERT_AFTER`（默认 10）发送一次异常邮件，恢复明确库存后重置。
- 重启保留状态；状态损坏会保存副本，并以首次明确结果建立静默基线。
- SMTP 与本地 JSON 无法形成事务：极少数“SMTP 已接收、状态还未写回时进程崩溃”的情况可能导致重发。

发送一封明确标注为测试的邮件（不访问商家、不修改状态）：

```bash
sudo -u vmiss-monitor /opt/vmiss-stock-monitor/.venv/bin/python /opt/vmiss-public-status/vps_stock_monitor.py --test-email
```

`--once` 是供 systemd 使用的一轮完整检查，会保存状态并按规则发邮件。

## RFCHOST 验证

普通请求及普通无头浏览器可能返回 `403` 和 Cloudflare 验证页。RFCHOST 使用 VPS 上现有 Xvfb 启动完整 Chromium，等待网站自行验证，保留独立浏览器会话；不点击验证码、不使用第三方解题服务。验证后再次请求官方页面，要求 HTTP 200、正确域名及两次一致的目标库存解析。每轮结束关闭浏览器，失败仍为 unknown。

RFCHOST 正常产品页也包含后台 JavaScript Detections 的 `scripts/jsd/main.js`，它与拦截整页的验证页面不同（[Cloudflare 官方说明](https://developers.cloudflare.com/cloudflare-challenges/concepts/how-challenges-work/)）。仅该已实测的脚本地址不再单独触发 unknown；真实验证标记、403、验证提示和目标套餐边界规则仍保持严格判断。网站策略可能变化，无法保证永久不再验证。

浏览器配置保存在 `/var/lib/vps-stock-monitor/browser-profiles/`，不对外发布。全流程仍仅在香港 VPS 运行，Windows 无需常开。

## 日本备用出口与随机间隔

每轮先使用香港出口；仅最终 unknown 的套餐追加一次日本复查。日本仍 unknown 时不发送到货邮件。已确认有货或无货不再重复请求日本。

浏览器和状态机仍运行在香港 VPS，备用流量经用户现有日本家宽 VPS 出口访问商家（不是 Windows 代理）。`query_location` 分别为 `hong-kong-vps` / `japan-vps-egress`。两条路线各自使用独立浏览器目录，避免混用验证会话。

- `vps-monitor-japan.service`：复用现有 Hysteria 程序；私密配置 `/etc/vps-monitor-japan/config.json` 为 600 权限，绑定 `127.0.0.1:10881/10882`，不公开代理端口。使用原日本证书 SHA-256 固定校验，凭据不进入 Git。
- `.env` 与四套餐服务设置 `JAPAN_PROXY_URL=http://127.0.0.1:10882`。
- VMISS 基准间隔 600 秒，±20% 随机；其余套餐定时器等待至少 480 秒，再随机增加 0～240 秒。检查超时不会密集补跑。
- 后台 JSD 不能靠普通 HTTP 结果直接确认：必须启动浏览器、获得有效验证会话，再以正常 HTTP 200 产品页和稳定库存结果确认。验证未通过仍 unknown，无法保证商家永远放行。

安装私密出口配置后启用备用服务：

```bash
sudo cp server/vps-monitor-japan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vps-monitor-japan
```

配置需要现有日本节点凭据及证书指纹，仓库不提供实际值，也不会自动从公共来源获取。
