# 东京 VPS 库存观察

独立的 Python VMISS 邮件监控见 [vmiss-stock-monitor/](vmiss-stock-monitor/README.md)，支持 Debian 12、systemd 和 GitHub 手动 SMTP 测试。日本端调度现在直接复用本仓库这一版本，不再依赖 vps_build 中的旧接口。

[库存页面](https://daidaideaa.github.io/vps_monitor/)由 **JP-HOME-HY2（日本家宽 VPS）**独立检查四家商户的五个套餐并发送邮件。Windows 无需常开，运行链路不再依赖香港 VPS。

| 套餐 | 检查方式 |
| --- | --- |
| VMISS JP.TKY.TRI.Basic | 完整 Chromium Headless；挑战页保守返回 unknown |
| ZgoCloud Tokyo Intel VPS · Starter | HTTP，无法确认时使用 Chromium |
| RFCHOST JP2-CO-Micro-Lite | Chromium + Xvfb，等待正常验证后严格解析 |
| V.PS Tokyo Cloud KVM · Starter | 官方订购页，套餐编号 148 |
| V.PS Tokyo Cloud KVM · Essential | 官方订购页，套餐编号 149 |

五个套餐依次检查，避免浏览器同时占用内存。每轮开始间隔随机 480～720 秒（8～12 分钟），systemd 不会重叠执行。页面每 30 秒读取保存的结果，手动“同步结果”也不访问商家。各套餐的“实际检查”才是库存检测时间。

日本 VPS 没有公网 HTTPS 入站端口，因此通过 HTTPS 将白名单公开状态上传到[状态接口](https://jp-vps-status.jp-home-subscription.workers.dev/status.json)。Cloudflare Worker 只保存和返回最后一份结果，不查询商家、不发送邮件、没有定时任务。日本检查的 query_location 为 japan-home-vps。上传失败保留旧结果并重试；旧检查时间不刷新，页面会标出过期记录。

页面采用暖白、炭灰和陶土色，主要数据直接展示，历史和库存数字保留在“检查详情”。样式集中在 `assets/site.css`。英文和数字使用站点所有者提供的 OpenAI Sans（Regular 400、Medium 500）；中文正文在 Apple 设备优先使用系统苹方，其他设备使用 MiSans，Noto Sans SC 为补充。标题和正文统一使用无衬线字形。MiSans 内部 Regular/Medium 的轴值分别为 330/380，通过字体声明映射到界面的 400/500 字重，避免中文比英文过粗。字体均随站点提供，运行时不依赖外部字体 CDN。字体与许可在 `assets/fonts/`，合计约 990 KiB，浏览器按字符范围加载所需文件并缓存。MiSans 使用小米官方发布的原始网页分片，不改动字体二进制；OpenAI Sans 保留原字体字形与元数据，仅转换为 WOFF2；Noto Sans SC 使用开放许可的字符子集。增加中文文案后可在本地安装 `fonttools brotli`，运行 `python scripts/build_fonts.py` 重新生成。首次构建需添加 `--openai-font-zip "字体包路径/OpenAI Sans.zip"`；原始字体包由所有者提供，不从第三方下载。未收录的新字符会使用系统字体，仍可正常显示。原始下载文件和构建依赖只保存在已忽略的 `.local/`，无需安装到 VPS。


## 结构

- index.html：四张商户卡片，V.PS 两个套餐合并展示独立状态。
- server/run_japan_cycle.py：日本端五套餐串行检查、邮件与持久化。
- server/vps_stock_monitor.py、stock_targets.py：其余四套餐的检查和状态机。
- server/export_status.py、publish_status.py：白名单导出和主动上传。
- server/vps-stock-japan.*：8～12 分钟随机定时器与服务。
- server/vps-status-publish.*：每 30 秒导出，结果改变才上传。
- server/install-japan.sh：不覆盖状态的安装脚本。
- status-gateway/：仅保存公开快照的 Cloudflare Worker。
- tests/：解析、邮件、历史、上传与网页测试。

VMISS 主程序来自独立项目 vmiss-stock-monitor/monitor.py，部署到 /opt/vmiss-stock-monitor/。本仓库不保存密码、上传令牌、浏览器会话或实际运行状态。旧香港部署文件仅作历史参考，不要同时启用旧监控。旧 worker/ 和 RFCHOST GitHub Actions 均没有自动检查计划。

## 日本端安装：Debian 12 / 13

将 VMISS 的 monitor.py、requirements.txt 和私密 .env 上传到 /opt/vmiss-stock-monitor/。已有用户、环境和状态可复用，不覆盖运行数据。

```bash
sudo apt update
sudo apt install -y python3 python3-venv xvfb systemd-timesyncd
sudo systemctl enable --now systemd-timesyncd
timedatectl status
sudo useradd --system --home /opt/vmiss-stock-monitor --shell /usr/sbin/nologin vmiss-monitor
sudo python3 -m venv /opt/vmiss-stock-monitor/.venv
sudo /opt/vmiss-stock-monitor/.venv/bin/pip install -r /opt/vmiss-stock-monitor/requirements.txt
sudo env PLAYWRIGHT_BROWSERS_PATH=/opt/vmiss-stock-monitor/.browsers /opt/vmiss-stock-monitor/.venv/bin/playwright install --with-deps chromium --no-shell
sudo chown -R vmiss-monitor:vmiss-monitor /opt/vmiss-stock-monitor
sudo chmod 700 /opt/vmiss-stock-monitor
sudo chmod 600 /opt/vmiss-stock-monitor/.env
```

小内存 VPS 先确认磁盘和交换空间足够，并确认时间已同步。服务限制实际内存 280 MiB、交换空间 192 MiB，每轮关闭浏览器。资源不足时检查日志，不能放宽库存判断。网关拒绝超过两分钟的未来导出时间；校时后可以自动替换此前错误的未来快照。

.env 设置 CHECK_INTERVAL_SECONDS=600、ERROR_ALERT_AFTER=10；旧 JAPAN_PROXY_URL 删除或留空，新服务直接使用日本出口。SMTP 支持 587 STARTTLS 和 465 SSL，验证服务器证书；Gmail 使用应用专用密码。

在有 Cloudflare 授权的开发电脑部署网关，不向 VPS 放置 Cloudflare 账号令牌：

```bash
npx wrangler deploy --config status-gateway/wrangler.jsonc
npx wrangler secret put PUBLISH_TOKEN --config status-gateway/wrangler.jsonc
```

为网关设置独立的长随机上传令牌，并通过 SSH 安全写入日本 VPS 的 /etc/vps-status-publisher.env，不提交或打印：

```dotenv
STATUS_PUBLISH_URL=https://jp-vps-status.jp-home-subscription.workers.dev/publish
STATUS_PUBLISH_TOKEN=<与网关一致的专用上传令牌>
```

上传完整仓库（至少 server/ 与 vmiss-stock-monitor/）后先验证，再安装。已有状态在内存中迁移，首次写回前保存 state.json.before-repo-split，保留去重标记和未知状态语义：

```bash
# 开发电脑：替换实际 SSH 地址和端口
scp -P <SSH端口> -r server vmiss-stock-monitor <管理用户>@<日本主机>:vps-monitor-stage/
# 日本 VPS
sudo bash ~/vps-monitor-stage/server/install-japan.sh --validate
sudo bash ~/vps-monitor-stage/server/install-japan.sh --apply
sudo -u vmiss-monitor /opt/vmiss-stock-monitor/.venv/bin/python /opt/vmiss-stock-monitor/monitor.py --test-email
sudo systemctl start vps-stock-japan.service
sudo journalctl -u vps-stock-japan.service -n 40 --no-pager
sudo systemctl start vps-status-publish.service
```

确认真实检查、内存及上传正常后启用：

```bash
sudo systemctl enable --now vps-stock-japan.timer vps-status-publish.timer
systemctl list-timers 'vps-*'
curl -fsS https://jp-vps-status.jp-home-subscription.workers.dev/status.json
```

正常迁移时，先停掉旧机的 VMISS 服务、四套餐检查定时器及服务、状态导出定时器及服务、日本备用代理，再复制以下三份最新状态并启用新端：

- /opt/vmiss-stock-monitor/state.json：VMISS 去重和最近确认状态。
- /var/lib/vps-stock-monitor/http-status.json：其它四套餐历史和去重。
- /var/lib/vmiss-public-status/status.json：VMISS 公开观测历史。

2026-09-22 本次迁移期间，Evoxt 控制台已无香港实例，旧机及接口失联，无法完整取回最新历史和去重文件。新端以静默基线开始，首次明确结果不制造补货邮件，后续补货正常通知；缺失历史不伪造为连续观测。

## 状态与邮件

- available / unavailable 是本轮明确结果；unknown 永远不表示有货。
- 各套餐独立去重；持续有货不重复，确认无货后再有货重新提醒。SMTP 失败不标记成功。
- unknown 保留最近确认状态、上次有货时间和无货起点。无货时长注明含未确认时段，不声称期间完全无货。
- 连续异常达到 ERROR_ALERT_AFTER 后发一次异常通知，恢复明确结果后重置。
- 重启保留状态；损坏文件保留副本并建立静默基线。SMTP 和 JSON 无事务保证，极少数发送后未写回即崩溃的情况可能重发。
- 五套餐均发送至 VMISS .env 的 MAIL_TO。

验证页面、403、访问失败、套餐边界不明或库存冲突均返回 unknown。RFCHOST 等待正常 Chromium 验证，不点击验证码或调用第三方解题服务；正常后台验证脚本与整页拦截分别处理，只有正常响应和稳定库存解析才采用结果。商家策略可能变化，不能保证永久放行。

V.PS 严格核对 Tokyo 选中位置、148/149 编号、对应标题及订单控件，其它套餐有货不能替代目标。公开数据不含邮箱、密码、cookies、HTML 或原始错误详情。

## 维护与测试

```bash
journalctl -u vps-stock-japan.service -u vps-status-publish.service --since today
sudo systemctl start vps-stock-japan.service         # 一轮检查，会保存并按规则通知
sudo systemctl start vps-status-publish.service      # 只上传，不访问商家
sudo systemctl disable --now vps-stock-japan.timer  # 关闭定时检查
sudo systemctl stop vps-stock-japan.service         # 停止正在执行的检查
```

VMISS --once 只检查，不发邮件、不改状态；串行服务会保存并按规则通知。修改 VMISS 套餐或 URL 后下一轮读取新 .env；其它目标修改 stock_targets.py 后部署。轮换上传令牌须同时更新 Worker secret 与 VPS 私密配置。

```bash
python -m unittest discover -s tests -v
node --test tests/frontend.test.cjs tests/gateway.test.mjs
```

## 仓库边界与迁移

节点部署、SSH、订阅及链路诊断由 [vps_build](https://github.com/daidaideaa/vps_build) 管理；本仓库负责库存、浏览器、邮件、状态页面、Cloudflare 状态接口及 systemd 定时器。两者独立检出为相邻目录。旧监控代码的有用历史保留在 Git 历史和本地 `.local/legacy-vmiss-stock-monitor/`；私密会话与状态不移动、不覆盖。

`run_japan_cycle.py` 使用 `check_stock` 与新版 Config/Result/邮件接口；`process_result`、`single_instance` 仍存在于本仓库当前版本。旧 `monitored_check` 不再是部署依赖。新版不复用旧持久浏览器会话恢复；遇验证页返回 unknown，不冒充库存已确认。升级可用安装脚本给出的私有备份恢复代码和 unit。
