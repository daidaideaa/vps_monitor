# 东京 VPS 库存观察

页面：**https://daidaideaa.github.io/vps_monitor/**

GitHub Pages 只负责展示状态；香港 VPS 负责真正的库存检查。当前统一展示 3 个东京套餐：

- VMISS `JP.TKY.TRI.Basic`：沿用现有 Playwright 监控，约每 180 秒检查。
- ZgoCloud `Tokyo Intel VPS · Starter`：每 180 秒检查 Starter 的 `Continue / Out of stock!`。
- RFCHOST `JP2-CO-Micro-Lite`：每 180 秒检查产品卡的 `N Available` 数字。

页面每 30 秒读取公开状态。`unknown` 与最近一次确认库存分开展示；HTTP 403、Cloudflare/网站验证、解析失败都不会被误判成有货。超过 3 个检查间隔（最低 9 分钟）的记录会显示为过期。

## 结构

```text
index.html                          三目标静态展示页，无构建依赖
server/http_stock_monitor.py        ZgoCloud + RFCHOST 单次检查器，HTTP 优先、Playwright fallback
server/http-stock-monitor.service   单次检查 systemd 服务
server/http-stock-monitor.timer     每 180 秒触发一次
server/export_status.py             合并 VMISS 与另外两个目标并导出公开字段
server/vmiss-public-status.service  状态导出服务（保留旧名称兼容现有部署）
server/vmiss-public-status.timer    每 30 秒导出状态
server/Caddyfile                    HTTPS 状态接口
tests/test_status.py                合并/脱敏状态测试
tests/test_http_stock_monitor.py    两家商户库存解析测试
```

公开接口仍为 `https://vmiss-status.96-126-179-210.sslip.io/status.json`，但部署新版后 schema 为 v2：

```json
{
  "schema_version": 2,
  "products": [
    {"provider": "VMISS", "status": "unavailable"},
    {"provider": "ZgoCloud", "status": "unavailable"},
    {"provider": "RFCHOST", "status": "unavailable", "stock": 0}
  ]
}
```

公开字段仅包含商家、套餐、官方购买链接、检查时间、库存状态、连续异常次数、库存数字（商家提供时）、检查间隔和通用说明。不会公开 `.env`、SMTP 凭据、收件人、浏览器会话、截图或原始错误日志。

## 香港 VPS 部署

现有 VMISS 监控继续位于 `/opt/vmiss-stock-monitor`，使用用户 `vmiss-monitor`。将本仓库 `server/` 内容同步到 `/opt/vmiss-public-status/` 后：

```bash
sudo cp /opt/vmiss-public-status/http-stock-monitor.{service,timer} /etc/systemd/system/
sudo cp /opt/vmiss-public-status/vmiss-public-status.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now http-stock-monitor.timer
sudo systemctl start http-stock-monitor.service
sudo systemctl enable --now vmiss-public-status.timer
sudo systemctl start vmiss-public-status.service
```

`http-stock-monitor.timer` 使用 `OnUnitActiveSec=180`，因此 ZgoCloud 与 RFCHOST 都按约 3 分钟检查一次。检查器优先使用 Python 标准库直接请求；若出现 HTTP 403/连接失败，会尝试复用现有 `/opt/vmiss-stock-monitor/.venv` 中的 Playwright/Chromium。若 fallback 也失败，本轮状态为 `unknown`，不会沿用为当前有货。

现有 Caddy 配置和公开域名无需变更。首次部署后可检查：

```bash
systemctl status http-stock-monitor.timer http-stock-monitor.service vmiss-public-status.timer caddy
journalctl -u http-stock-monitor.service -u vmiss-public-status.service --since today
cat /var/lib/vps-stock-monitor/http-status.json
curl https://vmiss-status.96-126-179-210.sslip.io/status.json
```

若只想停止新增的两家监控，不影响 VMISS：

```bash
sudo systemctl disable --now http-stock-monitor.timer
```

## 本地测试

```bash
python -m unittest discover -s tests -v
```

注意：GitHub Pages 本身无法可靠地每 3 分钟跨域抓取两家商户，也不适合承担常驻监控，所以 180 秒轮询仍由香港 VPS 的 systemd timer 完成。
