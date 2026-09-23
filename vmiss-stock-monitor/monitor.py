"""VMISS 单套餐库存监控；--once 无邮件、无状态写入。"""

import argparse
import json
import logging
import os
import random
import re
import signal
import smtplib
import ssl
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from threading import Event
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("vmiss")
STATUSES = {"available", "unavailable", "unknown"}
SECRET_KEYS = ("SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "MAIL_TO")
OUT = re.compile(r"\b(?:out\s+of\s+stock|sold\s+out)\b|缺货|售罄|暂无库存|库存不足", re.I)
COUNTS = re.compile(r"(?<![\w.,+\-])(\d+)\s+(?:Available\b|可用)", re.I)
BUY = re.compile(r"^(?:Order\s+Now|立即订购|现在订购)$", re.I)
BLOCKED = re.compile(
    r"just a moment|verify (?:that )?you are human|access denied|checking your browser|"
    r"attention required|sorry, you have been blocked|cloudflare(?:\s+challenge)?|人机验证",
    re.I,
)


@dataclass(frozen=True)
class Config:
    product_name: str = "JP.TKY.TRI.Basic"
    product_url: str = "https://app.vmiss.com/store/jp-tokyo-tri?language=chinese"
    interval: int = 60
    timeout_ms: int = 30000
    error_after: int = 10
    timezone: str = "Asia/Shanghai"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = field(default="", repr=False)
    smtp_password: str = field(default="", repr=False)
    smtp_from: str = field(default="", repr=False)
    mail_to: str = field(default="", repr=False)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        for key in SECRET_KEYS:
            value = os.environ.get(key, "")
            if value:
                message = message.replace(value, "[REDACTED]")
        return message


def setup_logging(debug=False):
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.handlers[:] = [handler]
    LOG.setLevel(logging.DEBUG if debug else logging.INFO)
    LOG.propagate = False


def load_config(require_smtp=False):
    # 不展开 ${...}，且不输出 dotenv/SMTP 的原始错误内容。
    logging.getLogger("dotenv.main").disabled = True
    load_dotenv(ROOT / ".env", override=False, interpolate=False)

    def number(key, default, positive=True):
        try:
            value = int(os.environ.get(key, str(default)))
        except ValueError:
            raise ValueError(f"{key} must be an integer") from None
        if positive and value <= 0:
            raise ValueError(f"{key} must be positive")
        return value

    interval = number("CHECK_INTERVAL_SECONDS", 60, positive=False)
    if interval < 30:
        LOG.warning("CHECK_INTERVAL_SECONDS below 30; using 30 seconds")
    cfg = Config(
        product_name=os.environ.get("PRODUCT_NAME", Config.product_name).strip(),
        product_url=os.environ.get("PRODUCT_URL", Config.product_url).strip(),
        interval=max(30, interval), timeout_ms=number("PAGE_TIMEOUT_MS", 30000),
        error_after=number("ERROR_ALERT_AFTER", 10),
        timezone=os.environ.get("TIMEZONE", "Asia/Shanghai"),
        smtp_host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=number("SMTP_PORT", 587),
        smtp_user=os.environ.get("SMTP_USER", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_from=os.environ.get("SMTP_FROM", ""), mail_to=os.environ.get("MAIL_TO", ""),
    )
    url = urlsplit(cfg.product_url)
    if (not cfg.product_name or "\n" in cfg.product_name or "\r" in cfg.product_name
            or url.scheme != "https" or not url.hostname or url.username or url.password):
        raise ValueError("PRODUCT_NAME / PRODUCT_URL invalid; use a public HTTPS product URL")
    try:
        ZoneInfo(cfg.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("TIMEZONE is invalid") from None
    if require_smtp:
        missing = [key for key in ("SMTP_HOST", *SECRET_KEYS) if not os.environ.get(key, "").strip()]
        # SMTP_HOST 有默认值，不必在外部重复设置。
        if cfg.smtp_host and "SMTP_HOST" in missing:
            missing.remove("SMTP_HOST")
        if missing:
            raise ValueError("Missing SMTP configuration: " + ", ".join(missing))
        if cfg.smtp_port not in (465, 587):
            raise ValueError("SMTP_PORT must be 587 (STARTTLS) or 465 (TLS)")
        if any("\r" in v or "\n" in v for v in (cfg.smtp_host, cfg.smtp_user, cfg.smtp_from, cfg.mail_to)):
            raise ValueError("SMTP configuration contains invalid header characters")
    return cfg


@dataclass(frozen=True)
class Result:
    status: str = "unknown"
    evidence: str = "Cannot determine stock"
    rule: str = "unknown"
    title: str = ""
    snippet: str = ""


def parse_region(text, buttons=()):
    """只接收已验证边界的目标卡片；按钮必须由 DOM 确认为可操作。"""
    text = " ".join(text.split())
    if BLOCKED.search(text):
        return Result(evidence="Challenge / access denied", rule="blocked")
    counts = list(COUNTS.finditer(text))
    out = OUT.search(text)
    zero = next((m for m in counts if int(m.group(1)) == 0), None)
    if out or zero:
        return Result("unavailable", (out or zero).group(), "explicit_unavailable")
    # 不把 -1、1.5、1,000 或库存数字冲突误当作正整数；有歧义不退回按钮。
    if re.search(r"\bAvailable\b|可用", text, re.I):
        if len(counts) == 1 and len(re.findall(r"\bAvailable\b|可用", text, re.I)) == 1:
            return Result("available", counts[0].group(), "positive_count")
        return Result(evidence="Ambiguous or invalid stock count", rule="ambiguous_count")
    for button in buttons:
        if BUY.fullmatch(button.strip()):
            return Result("available", button.strip(), "enabled_order_button")
    return Result(evidence="No explicit stock signal in target product", rule="no_signal")


# 只接受具有明确商品语义的卡片。绝不向上扩大到整行、整个列表或 body。
# 不依赖具体套餐名、套餐前缀或 Core/Pro 等兄弟套餐名称。
EXTRACT = r"""(product) => {
    const norm = s => (s || '').replace(/\s+/g, ' ').trim();
    const visible = e => !!(e.getClientRects().length) &&
        getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
    const cardSelector = '.product, .product-card, .pricing-card, .package, .package-card, [data-product-id]';
    const headingSelector = 'h1,h2,h3,h4,h5,h6,.product-name,.product-title,.package-name,[data-product-name]';
    const candidates = [...document.querySelectorAll(cardSelector)].filter(visible).filter(card => {
        const headings = [...card.querySelectorAll(headingSelector)].filter(visible);
        const matches = headings.filter(h => norm(h.innerText) === norm(product));
        const otherTitles = headings.filter(h => norm(h.innerText) && norm(h.innerText) !== norm(product));
        // 嵌套 h3/span 同名可以；其它标题可能是另一套餐，保守拒绝。
        return matches.length > 0 && otherTitles.length === 0;
    });
    // WHMCS 的 #product1.product 是卡片；#products 列表不属于候选。
    const cards = candidates.filter(card => !candidates.some(other => other !== card && card.contains(other)));
    const cleanText = card => {
        const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
        const parts = [];
        while (walker.nextNode()) {
            const p = walker.currentNode.parentElement;
            if (p && !p.closest('script,style,template,noscript') && visible(p)) parts.push(walker.currentNode.textContent);
        }
        return norm(parts.join(' '));
    };
    const title = document.title;
    const body = (document.body?.innerText || '').slice(0, 100000);
    const challenge = [...document.querySelectorAll('#challenge-form,#challenge-running,.cf-turnstile,iframe[src*="challenges.cloudflare.com"]')].some(visible);
    if (cards.length !== 1) return {title, body, challenge, count: cards.length};
    const card = cards[0];
    // 任何包含第二张商品卡片的容器都不能用作边界。
    const nested = [...card.querySelectorAll(cardSelector)].some(e => visible(e) && e.querySelector(headingSelector));
    if (nested) return {title, body, challenge, count: 0};
    const buttons = [...card.querySelectorAll('a,button,input[type="submit"]')].filter(e => {
        if (!visible(e) || e.matches(':disabled,[aria-disabled="true"],.disabled') || e.closest('[inert],.disabled,[aria-disabled="true"]')) return false;
        if (e.tagName === 'A') {
            const href = e.getAttribute('href');
            if (!href || href === '#' || /^javascript:/i.test(href)) return false;
            const url = new URL(href, location.href);
            if (url.origin !== location.origin || !/cart|order/i.test(url.pathname + url.search)) return false;
        }
        return true;
    }).map(e => norm(e.innerText || e.value));
    return {title, body, challenge, count: 1, text: cleanText(card), buttons};
}"""


def inspect_page(page, product_name, http_status=200):
    if http_status is None or not 200 <= http_status < 300:
        return Result(evidence=f"HTTP {http_status or 'response missing'}", rule="http_error")
    data = page.evaluate(EXTRACT, product_name)
    title = data.get("title", "")[:200]
    if data["challenge"] or BLOCKED.search(title + "\n" + data["body"]):
        return Result(evidence="Cloudflare / challenge / access denied", rule="blocked", title=title)
    if data["count"] != 1:
        return Result(evidence="Target product missing or product boundary ambiguous", rule="product_boundary", title=title)
    parsed = parse_region(data["text"], data["buttons"])
    return Result(parsed.status, parsed.evidence, parsed.rule, title, data["text"][:800])


def save_screenshot(page, status):
    # 固定文件名，最多两张，避免长跑占满磁盘。
    try:
        directory = ROOT / "screenshots"
        directory.mkdir(mode=0o700, exist_ok=True)
        page.screenshot(path=str(directory / f"{status}.png"), timeout=5000)
    except Exception:
        LOG.warning("Screenshot could not be saved")


def check_stock(cfg, screenshot_unknown=False):
    result = Result(evidence="Page load failed", rule="load_error")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="chromium", headless=True, timeout=cfg.timeout_ms,
                env={k: v for k, v in os.environ.items() if k not in SECRET_KEYS},
            )
            try:
                context = browser.new_context(locale="zh-CN", timezone_id=cfg.timezone)
                page = context.new_page()
                page.set_default_timeout(cfg.timeout_ms)
                document = {}
                def observed(response):
                    if response.request.is_navigation_request() and response.frame == page.main_frame:
                        document.update(status=response.status, headers=response.headers)
                page.on('response', observed)
                deadline = time.monotonic() + cfg.timeout_ms / 1000
                response = page.goto(cfg.product_url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
                document.setdefault('status', response.status if response else None)
                expected = urlsplit(cfg.product_url)
                actual = urlsplit(page.url)
                if (actual.scheme, actual.netloc, actual.path.rstrip('/')) != (expected.scheme, expected.netloc, expected.path.rstrip('/')):
                    return Result(evidence="Unexpected page redirect", rule="redirect")
                previous = None
                # 同一次访问两次一致观测，避免加载途中按钮先出现造成误报。
                while True:
                    actual = urlsplit(page.url)
                    if (actual.scheme, actual.netloc, actual.path.rstrip('/')) != (expected.scheme, expected.netloc, expected.path.rstrip('/')):
                        return Result(evidence="Unexpected page redirect", rule="redirect")
                    if document.get('headers', {}).get('cf-mitigated') == 'challenge':
                        result = Result(evidence="Cloudflare challenge", rule="blocked")
                    else:
                        result = inspect_page(page, cfg.product_name, document['status'])
                    signature = (result.status, result.evidence, result.snippet)
                    if result.status != "unknown" and signature == previous:
                        break
                    if time.monotonic() >= deadline:
                        if result.status != "unknown":
                            result = Result(evidence="Product did not stabilize", rule="unstable")
                        break
                    previous = signature
                    page.wait_for_timeout(500)
                if result.status == "available" or (result.status == "unknown" and screenshot_unknown):
                    save_screenshot(page, result.status)
            finally:
                browser.close()
    except Exception:
        # Playwright 原始异常可能含 URL、环境路径等内容，不进入日志或邮件。
        result = Result(evidence="Browser unavailable, timeout, page load or DOM failure", rule="browser_error")
    return result


def now(cfg):
    return datetime.now(ZoneInfo(cfg.timezone)).isoformat(timespec="seconds")


def fresh_state(cfg):
    return {
        "product_name": cfg.product_name, "product_url": cfg.product_url,
        "last_status": "unknown", "alerted_for_current_stock": False,
        "consecutive_errors": 0, "error_alert_sent": False,
        "last_check": None, "last_evidence": "",
    }


def load_state(path, cfg):
    if not path.exists():
        return fresh_state(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get('version') == 1 and 'target' in data and 'last_status' not in data:
            # Convert in memory; original file is backed up only on the first write.
            target = data['target']
            data = {**data, 'product_name': target.get('product_name'), 'product_url': target.get('product_url'),
                    'last_status': data.get('last_confirmed'), 'consecutive_errors': data.get('unknown_count'),
                    'alerted_for_current_stock': data.get('stock_notified'), 'error_alert_sent': data.get('error_notified'),
                    'last_check': data.get('last_checked'), 'last_evidence': data.get('last_unknown_reason', ''),
                    '_legacy_migration': True}
        expected = fresh_state(cfg)
        if not isinstance(data, dict) or not expected.keys() <= data.keys():
            raise ValueError
        if (data["last_status"] not in STATUSES or type(data["consecutive_errors"]) is not int
                or data["consecutive_errors"] < 0
                or any(type(data[k]) is not bool for k in ("alerted_for_current_stock", "error_alert_sent"))
                or not isinstance(data["last_evidence"], str)
                or data["last_check"] is not None and not isinstance(data["last_check"], str)):
            raise ValueError
        if (data["product_name"], data["product_url"]) != (cfg.product_name, cfg.product_url):
            LOG.warning("Product configuration changed; starting a new stock cycle")
            return expected
        return {**data, **{key: data[key] for key in expected}}
    except (OSError, ValueError, TypeError):
        raise ValueError("State file unreadable or invalid; repair it before restarting") from None


def save_state(path, state):
    # 先 fsync 临时文件，再原子替换；失败时不继续发送邮件。
    if state.pop('_legacy_migration', False):
        backup = path.with_name(path.name + '.before-repo-split')
        if path.exists() and not backup.exists():
            with backup.open('xb') as stream:
                stream.write(path.read_bytes())
            os.chmod(backup, 0o600)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as f:
            temp = Path(f.name)
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def send_email(cfg, subject, body):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg.smtp_from, cfg.mail_to
    msg.set_content(body)
    context = ssl.create_default_context()
    try:
        if cfg.smtp_port == 465:
            client = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30, context=context)
        else:
            client = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
        try:
            if cfg.smtp_port != 465:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            client.login(cfg.smtp_user, cfg.smtp_password)
            refused = client.send_message(msg)
            if refused:
                LOG.error("SMTP recipient refused; check MAIL_TO")
                return False
        finally:
            # DATA 已成功后，QUIT 失败不能造成下一轮重复发信。
            client.close()
    except smtplib.SMTPAuthenticationError:
        LOG.error("SMTP authentication failed; check account and App Password configuration")
        return False
    except Exception:
        LOG.error("SMTP send failed; check TLS, network and mail configuration")
        return False
    return True


def process_result(cfg, state, result, path, sender=send_email):
    updated = {**state, "last_check": now(cfg), "last_evidence": result.evidence}
    alert = None
    if result.status == "unknown":
        updated["consecutive_errors"] += 1
        if updated["consecutive_errors"] >= cfg.error_after and not updated["error_alert_sent"]:
            alert = "error"
    else:
        updated.update(last_status=result.status, consecutive_errors=0, error_alert_sent=False)
        if result.status == "unavailable":
            updated["alerted_for_current_stock"] = False
            updated.pop("baseline_required", None)
        elif updated.pop("baseline_required", False):
            updated["alerted_for_current_stock"] = True
        elif not updated["alerted_for_current_stock"]:
            alert = "stock"
    save_state(path, updated)
    if alert:
        if alert == "stock":
            subject = f"[VMISS 到货] {cfg.product_name} 有货了"
            body = (f"{cfg.product_name} 检测到有货。\n\n检测时间：\n{updated['last_check']}\n\n"
                    f"当前状态：\navailable\n\n库存证据：\n{result.evidence}\n\n"
                    f"购买页面：\n{cfg.product_url}\n\n请尽快手动确认并下单。\n")
        else:
            subject = f"[VMISS 监控异常] {cfg.product_name}"
            body = (f"套餐：{cfg.product_name}\n检测时间：{updated['last_check']}\n"
                    f"当前状态：unknown\n连续异常：{updated['consecutive_errors']} 次\n"
                    f"库存证据：{result.evidence}\n可能原因：Cloudflare、页面结构变化、访问失败或解析失败。\n"
                    f"最近确认状态：{updated['last_status']}\n官方页面：{cfg.product_url}\n")
        if sender(cfg, subject, body):
            updated["alerted_for_current_stock" if alert == "stock" else "error_alert_sent"] = True
            save_state(path, updated)
            LOG.info("%s email accepted by SMTP", alert)
    return updated


@contextmanager
def single_instance():
    """Protect the persistent profile and state from concurrent CLI/service runs."""
    with (ROOT / ".monitor.lock").open("a+b") as handle:
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0"); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ValueError("Another monitor/--once is running; stop the service before --once") from None
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(handle, fcntl.LOCK_UN)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def log_result(cfg, result):
    LOG.info("product=%s status=%s evidence=%s", cfg.product_name, result.status, result.evidence)
    LOG.debug("page_title=%s target_text=%s rule=%s", result.title, result.snippet, result.rule)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="检查一次，不发邮件、不更新状态；unknown 返回 2")
    modes.add_argument("--test-email", action="store_true", help="只发送测试邮件，不访问 VMISS")
    parser.add_argument("--debug", action="store_true", help="打印页面标题、目标卡片和规则，不打印凭据")
    args = parser.parse_args(argv)
    setup_logging(args.debug)
    try:
        cfg = load_config(require_smtp=not args.once)
        if args.test_email:
            sent = send_email(cfg, "[VMISS Monitor] Test Email", "SMTP configuration is working.")
            if sent:
                LOG.info("Test email accepted by SMTP; confirm receipt in your mailbox")
            return 0 if sent else 1
        if args.once:
            result = check_stock(cfg)
            log_result(cfg, result)
            return 2 if result.status == "unknown" else 0
        stop = Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        with single_instance():
            path = ROOT / "state.json"
            state = load_state(path, cfg)
            while not stop.is_set():
                started = time.monotonic()
                result = check_stock(cfg, screenshot_unknown=state["consecutive_errors"] + 1 >= cfg.error_after)
                log_result(cfg, result)
                state = process_result(cfg, state, result, path)
                delay = max(30, cfg.interval * random.uniform(0.9, 1.1) - (time.monotonic() - started))
                stop.wait(delay)
        return 0
    except ValueError as exc:
        LOG.error("%s", exc)
        return 1
    except Exception:
        LOG.error("Monitor stopped due to local configuration, state I/O or runtime failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
