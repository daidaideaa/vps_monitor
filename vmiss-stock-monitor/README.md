# VMISS VPS 库存监控

Python + Playwright Headless Chromium，默认每 60 秒检查 `JP.TKY.TRI.Basic`，有货时通过 SMTP 提醒。仅使用 JSON 保存状态，无数据库、Docker、Web UI 或自动购买。

本目录是**独立部署服务**，GitHub workflow 位于仓库根目录 `.github/workflows/monitor-test.yml`。现有 `server/run_japan_cycle.py` 使用另一版 VMISS 接口，本项目不是它的直接替换件。已有日本端多商户监控的机器，不要覆盖其 `/opt/vmiss-stock-monitor` 或同时启动两套 VMISS 邮件监控。下面命令适用于全新安装；迁移已有服务须单独安排接口、状态迁移和停用旧调度。

## 配置

复制 `.env.example` 为 `.env`，配置程序从项目所在目录读取；系统环境变量优先。`.env` 的值不做 `${...}` 展开。真实凭据只在 VPS 的私密 `.env` 或 GitHub Environment Secrets 中填写，不放进源码、聊天、日志或 Git。

```dotenv
PRODUCT_NAME=JP.TKY.TRI.Basic
PRODUCT_URL=https://app.vmiss.com/store/jp-tokyo-tri?language=chinese

CHECK_INTERVAL_SECONDS=60
PAGE_TIMEOUT_MS=30000
ERROR_ALERT_AFTER=10
TIMEZONE=Asia/Shanghai

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
MAIL_TO=
```

- 更换套餐只改 `PRODUCT_NAME` 和 `PRODUCT_URL`，例如 `JP.TKY.BGP.Basic` / `JP.TKY.BGP.Pro`；程序自动开始该目标的新库存周期。
- Gmail 的 `SMTP_PASSWORD` 必须用 **Google App Password**，不是 Google 登录密码。`SMTP_FROM` 使用该账号允许发送的地址；建议 `MAIL_TO` 配置一个收件地址。
- 587 强制 STARTTLS，465 使用 TLS；两者均验证证书，禁止明文认证。不打印 SMTP 服务端原始错误或调试会话。
- 检查间隔约 ±10% 随机抖动；配置低于 30 秒会警告并调整为 30 秒。每轮结束后至少等待 30 秒，不追赶积压请求。
- `PAGE_TIMEOUT_MS` 分别限制浏览器启动、导航以及等待 DOM 稳定的时间；不是整个进程总时长上限。SMTP 每次网络操作超时 30 秒。

## Debian 12 VPS 全新部署

先确认没有已有监控占用 `/opt/vmiss-stock-monitor`。以下安装过程不会覆盖该目录：

```bash
sudo apt update
sudo apt install -y python3 python3-venv git ca-certificates tzdata

git clone https://github.com/daidaideaa/vps_monitor.git
cd vps_monitor/vmiss-stock-monitor

# 已存在目录时 mkdir 失败，&& 后的复制不会执行。
sudo mkdir /opt/vmiss-stock-monitor && sudo cp -a . /opt/vmiss-stock-monitor/
# 如果上一步提示目录已存在，在此停止，不执行下面步骤。

id vmiss-monitor >/dev/null 2>&1 || sudo useradd --system --home /opt/vmiss-stock-monitor --shell /usr/sbin/nologin vmiss-monitor
sudo python3 -m venv /opt/vmiss-stock-monitor/.venv
sudo /opt/vmiss-stock-monitor/.venv/bin/pip install --upgrade pip
sudo /opt/vmiss-stock-monitor/.venv/bin/pip install -r /opt/vmiss-stock-monitor/requirements.txt
sudo env PLAYWRIGHT_BROWSERS_PATH=/opt/vmiss-stock-monitor/.browsers /opt/vmiss-stock-monitor/.venv/bin/playwright install --with-deps chromium

cd /opt/vmiss-stock-monitor
sudo cp .env.example .env
sudo chmod 600 .env
sudo nano .env
sudo chown -R vmiss-monitor:vmiss-monitor /opt/vmiss-stock-monitor
sudo chmod 700 /opt/vmiss-stock-monitor
```

VPS 部署前，按顺序测试（不要在命令行直接写密码）：

```bash
sudo -u vmiss-monitor /opt/vmiss-stock-monitor/.venv/bin/python /opt/vmiss-stock-monitor/monitor.py --test-email
sudo -u vmiss-monitor env PLAYWRIGHT_BROWSERS_PATH=/opt/vmiss-stock-monitor/.browsers /opt/vmiss-stock-monitor/.venv/bin/python /opt/vmiss-stock-monitor/monitor.py --once
```

先确认邮箱收到测试邮件，再确认真实库存是 `available` 或 `unavailable`。`unknown` 不代表无货；持续 unknown 时先排查 Chromium 安装、网络出口、Cloudflare 和页面结构，不可放宽规则来制造有货结果。标准 Headless Chromium 也可能被 Cloudflare 拦截，本项目不会绕过验证码，不能承诺每个网络出口都可访问。

确认完成后启用开机自启和异常重启：

```bash
sudo cp /opt/vmiss-stock-monitor/vmiss-stock-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vmiss-stock-monitor

sudo systemctl status vmiss-stock-monitor
sudo journalctl -u vmiss-stock-monitor -f
sudo systemctl restart vmiss-stock-monitor
```

修改 `.env` 后重启服务。服务以专用非登录用户运行，关闭 SSH 后继续执行。停止：

```bash
sudo systemctl disable --now vmiss-stock-monitor
```

## 本地命令与测试

Debian 12 / Linux，Python 3.11+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
cp .env.example .env
chmod 600 .env
nano .env

pytest -q
python monitor.py --once
python monitor.py --once --debug
python monitor.py --test-email
python monitor.py --debug
```

`--once` 只导航访问一次 VMISS，打印 `product=... status=... evidence=...`，不发邮件、不读取/修改 `state.json`；明确状态返回退出码 0，unknown 返回 2，配置错误返回 1。同一次页面加载内等待两个一致观测，不做刷新重试。

`--test-email` 不启动浏览器、不访问 VMISS，发送主题 `[VMISS Monitor] Test Email`，正文 `SMTP configuration is working.`。成功仅表示 SMTP 接收，仍需到收件箱检查投递。

不加模式参数则长期轮询。`--debug` 额外打印页面标题、目标卡片文本、匹配规则。到货或连续异常达阈值时可保存 `screenshots/available.png`、`screenshots/unknown.png`，各自覆盖，最多两张，不作为邮件附件或 Actions artifact。

## 库存解析与状态持久化

- 精确匹配配置的商品标题，并验证唯一商品卡片边界。未找到、重复卡片、跨卡片容器、HTTP 错误、重定向、验证页和 DOM 异常均是 unknown。
- 无货词或 `0 Available` 优先于正库存和按钮；单个正整数 `N Available` 表示有货；无库存数字时才允许当前卡片内可用的 `Order Now` / `立即订购` / `现在订购` 按钮作为依据。禁用、隐藏、纯文本或跳转外站的按钮不算。
- 冲突或异常数字不能降级为按钮判断。Core / Pro 等套餐的库存不参与当前目标判断。
- `state.json` 保存 `last_status`、`alerted_for_current_stock`、`consecutive_errors`、`error_alert_sent`、`last_check`、`last_evidence`，另外绑定套餐名称和 URL，防止切换套餐继承去重。
- 首次运行已经有货时提醒一次；随后持续有货不重复，确认无货后重置，下次补货再提醒。unknown 只更新检查时间、证据与错误次数，保留最近确认状态和到货去重标记。
- 连续 unknown 达阈值后发一次异常邮件，明确恢复后重置；发送失败不标记成功，下一轮符合条件时重试。
- 原子替换和文件同步防止半写状态；常驻进程用文件锁防止重复启动。状态损坏时停止并保留原文件，修复后重启，避免静默丢失去重。
- SMTP 与本地 JSON 不能做到事务性的 exactly-once：SMTP 已接收但进程尚未保存成功标记就崩溃，极少数情况下会重发；先保存“成功”则可能漏报，因此本项目选择成功发信后保存。

## GitHub Environment Secrets 与手动邮件测试

进入 **GitHub → Repository → Settings → Environments → New environment → `vmiss-test`**。

在 **Environment secrets** 添加：

| Secret | 填写内容 |
| --- | --- |
| `SMTP_USER` | SMTP 登录邮箱 |
| `SMTP_PASSWORD` | Gmail App Password 或其他服务商的 SMTP 授权密码 |
| `SMTP_FROM` | 允许的发件邮箱 |
| `MAIL_TO` | 接收到货提醒的邮箱 |

四个值都用 Secret 保存，仓库不保存真实邮箱或密码。不需要、也不要把密码发给 Codex。可自行为 Environment 配置审批和分支限制。[GitHub 官方 Secrets 说明](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)。

普通 push / PR（本项目相关路径变更）只安装 Python、依赖与 Chromium，执行 `pytest` 和 `--once`，**不关联 Environment、不注入 SMTP Secrets、不发送邮件**。

手动测试：**Actions → VMISS Monitor Test → Run workflow → 选择分支 → Run workflow**。先执行同样的 pytest 与真实单次检查，随后 `integration-email-test` 使用 `environment: vmiss-test` 和上述四个 Secrets 执行 `--test-email`。它只允许 `workflow_dispatch`。没有上传日志、截图或配置的 artifact。

VMISS 实时检查若返回 unknown，workflow 会明确显示 warning 和 Job summary，保留真实状态，不把 unknown 写成“库存检查成功”；离线测试通过后仍允许手动 SMTP 测试，以便独立诊断邮件配置。CI 绿色只说明离线测试和命令执行正常，不证明商家在该出口可访问。

若 `Authentication failed`，检查自己填写的 App Password、账号和发件权限；不要打印 Secret 或开启 SMTP 协议 debug。Secrets 缺失时仅输出缺失的变量名。

## 文件

核心逻辑只有 `monitor.py`；`requirements.txt` 锁定依赖，`tests/test_parser.py` 使用离线 Chromium 页面和 SMTP 替身验证误报、状态与邮件。`.env`、状态、截图、锁文件和虚拟环境均被忽略。

浏览器与系统依赖安装参考 [Playwright 官方文档](https://playwright.dev/python/docs/browsers#install-system-dependencies)。
