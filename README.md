# VMISS 库存观察

页面：**https://daidaideaa.github.io/vps_monitor/**

GitHub Pages 托管静态页面，香港 VPS 上的现有 Playwright 监控负责检查库存和发送邮件。页面本身不会请求 VMISS 或触发额外库存检查。

- 默认监控 `JP.TKY.TRI.Basic`，约每 180 秒检查。
- 页面每 30 秒读取最新公开状态，显示有货、无货、无法确认或数据过期。
- `unknown` 与最近确认的库存分开展示，Cloudflare 验证不会显示成有货。
- 超过 3 个检查间隔（最低 9 分钟）的记录显示为过期；接口断连显示连接异常。
- 最近检查时间使用北京时间；修改监控项目 `.env` 中的套餐、页面和间隔后重启监控即可同步。

## 结构

```text
index.html                       静态页面，无构建依赖
server/export_status.py           从监控状态中提取允许公开的字段
server/vmiss-public-status.service
server/vmiss-public-status.timer  每 30 秒导出状态
server/Caddyfile                  HTTPS 状态接口
tests/test_status.py              状态导出测试
```

公开接口：`https://vmiss-status.96-126-179-210.sslip.io/status.json`。

公开字段仅包含套餐、购买链接、检查时间、库存状态、连续异常次数、检查间隔和通用说明。不会公开 `.env`、SMTP 凭据、收件人、浏览器会话、截图或原始错误日志。Web 服务只允许访问 `/status.json`。

## VPS 部署

现有库存监控位于 `/opt/vmiss-stock-monitor`，使用用户 `vmiss-monitor`；其虚拟环境已有 `python-dotenv`。将 `server/` 的文件上传到 `/opt/vmiss-public-status/` 后：

```bash
sudo apt-get install -y --no-install-recommends caddy
sudo cp /opt/vmiss-public-status/vmiss-public-status.{service,timer} /etc/systemd/system/
# 首次安装前检查已有 Caddy 配置；已有站点时应合并配置。
sudo cp /opt/vmiss-public-status/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now vmiss-public-status.timer
sudo systemctl start vmiss-public-status.service
sudo systemctl enable --now caddy
```

本次部署已设置 Caddy 的内存上限 80 MiB、交换空间上限 128 MiB；导出任务内存上限 48 MiB。监控的 300 MiB 内存上限保持不变。

接口使用 sslip.io DNS 将域名解析到 VPS，Caddy 自动申请和续期 HTTPS 证书，需 TCP 80/443 可达。更换 VPS 或域名时同步修改 `server/Caddyfile` 和 `index.html` 中的接口地址。可替换为自己持有的域名。

GitHub 仓库 Settings → Pages 已选择 `main` 分支根目录。页面代码变更推送后部署；库存变更直接由接口更新，不产生 Git 提交或反复触发 Pages 构建。

## 检查与维护

```bash
systemctl status vmiss-stock-monitor vmiss-public-status.timer caddy
journalctl -u vmiss-public-status.service -u caddy --since today
curl https://vmiss-status.96-126-179-210.sslip.io/status.json
```

独立停止展示接口不会停止库存监控和邮件提醒：

```bash
sudo systemctl stop vmiss-public-status.timer caddy
```

参考：[GitHub Pages 文档](https://docs.github.com/en/pages)、[Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)。
