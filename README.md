# 东京 VPS 库存观察

页面：[GitHub Pages](https://daidaideaa.github.io/vps_monitor/)。VMISS、ZgoCloud、RFCHOST、V.PS 四家五个套餐统一由香港 VPS 查询；Windows 电脑无需常开。

> 迁移代码需要先部署到 VPS，再发布页面。仅更新仓库不会自动安装 VPS 服务或停用 Cloudflare 上已存在的 Cron。

| 套餐 | 检查位置 | 检查方式 |
| --- | --- | --- |
| VMISS `JP.TKY.TRI.Basic` | 香港 VPS | 原有 Playwright 服务，保留邮件提醒 |
| ZgoCloud `Tokyo Intel VPS · Starter` | 香港 VPS | HTTP，无法确认时使用 Playwright Chromium |
| RFCHOST `JP2-CO-Micro-Lite` | 香港 VPS | HTTP，无法确认时使用 Playwright Chromium |
| V.PS `Tokyo Cloud KVM · Starter` | 香港 VPS | 官方订购页，套餐编号 148 |
| V.PS `Tokyo Cloud KVM · Essential` | 香港 VPS | 官方订购页，套餐编号 149 |

所有套餐目标间隔约 300 秒（5 分钟）。页面每 30 秒读取一次 [VPS 状态接口](https://vmiss-status.96-126-179-210.sslip.io/status.json)，不请求商家或 Cloudflare Worker，也不触发库存检查。

## 结构

```text
index.html                        五卡页面，仅读取 VPS API
server/stock_targets.py            ZgoCloud / RFCHOST / V.PS 官方套餐地址
server/vps_stock_monitor.py        四个套餐查询、解析与独立持久化
server/vps-stock-monitor.*         约 5 分钟运行一次的 systemd 服务与定时器
server/export_status.py            合并五个套餐，白名单导出 schema v2
server/vmiss-public-status.*       每 30 秒导出公开状态
server/Caddyfile                   原有 HTTPS 接口配置
tests/                            解析、状态历史、单数据源页面测试
worker/                           旧 Worker，定时配置已清空，保留代码与 KV
```

四个新增套餐按顺序检查以限制内存使用，HTTP 已确认库存时不启动浏览器。浏览器复用 VMISS 已安装的 Chromium，但使用独立临时会话，不读写 VMISS 的浏览器配置或 SMTP 凭据。验证页、访问失败、套餐边界不明、库存标记冲突都返回 `unknown`。

V.PS 使用官网链接的实际订购页，分别核对选中位置 Tokyo、套餐编号 148/149 及对应 Starter/Essential 标题；卡片前置 Out of stock 和 outofstock 类优先判无货。仅当目标卡片唯一且选中、订单按钮启用时确认有货，不能用 Premium 的选中状态或营销页 Order 链接代替。页面加载的未激活验证码脚本不等于验证拦截；真实验证组件、验证提示和 HTTP 403 仍为 unknown。

只识别 ZgoCloud 的 Starter 到 Standard、RFCHOST 的 Micro-Lite 到 Mini-Lite 范围，不能用其它套餐的库存代替。每检查完一个套餐立即原子保存；另一个套餐失败不丢失已有结果。服务限制单次运行 270 秒，不会重叠执行。

## 在已有香港 VPS 上部署

前提：已有 `/opt/vmiss-stock-monitor/.venv`、Playwright Chromium、`vmiss-monitor` 用户和 Caddy。现有 VMISS 服务继续运行，无需替换它的代码；安装脚本仅将其 `.env` 中的检查间隔设为 300 秒。

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
- 连续无货时间仅累计连续有效观测，unknown、有货或超过 3 个间隔的断档会中断累计。
- 超过 3 个检查间隔的记录为过期；接口断连显示连接异常。某家 unknown 不影响其它套餐。
- 公开 API 只导出允许字段，不发布邮箱、密码、cookies、页面 HTML 或原始错误详情。

VMISS 历史沿用上次公开快照；其它套餐的历史保存在 `/var/lib/vps-stock-monitor/http-status.json`。旧 Worker 的历史不冒充 VPS 的新检查。VMISS 邮件提醒保持原样；其它四个套餐提供页面状态展示，不增加邮件发送。

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
