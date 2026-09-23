"""离线真实 Chromium DOM + 状态机 + SMTP 替身；测试不访问商家、不发真实邮件。"""

import logging
import smtplib
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor as m

PRODUCT = "JP.TKY.TRI.Basic"


def card(text="0 Available", name=PRODUCT, button=""):
    return f'<div class="product" id="product1"><header><h3>{name}</h3></header><div>{text}</div>{button}</div>'


ORDER = '<a href="/cart.php?a=add&pid=1">Order Now</a>'


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chromium", headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    # 截断一切外网：只有测试 HTML，测试中出现任意请求均在此本地响应。
    page.route("**/*", lambda route: route.fulfill(body="<html></html>", content_type="text/html"))
    page.goto("https://app.vmiss.com/store/jp-tokyo-tri")
    yield page
    page.close()


@pytest.mark.parametrize("html,status", [
    (card("0 Available"), "unavailable"),
    (card("3 Available"), "available"),
    (card("Out of Stock"), "unavailable"),
    (card("", button=ORDER), "available"),
    (card("", name="JP.TKY.TRI.Pro", button=ORDER), "unknown"),
    (card("0 Available") + card("", "JP.TKY.TRI.Pro", ORDER), "unavailable"),
    ('<title>Just a moment...</title>' + card("3 Available"), "unknown"),
    ('<p>Cloudflare</p>' + card("3 Available"), "unknown"),
    ('<form id="challenge-form">verify</form>' + card("3 Available"), "unknown"),
    (card("Sold Out", button=ORDER), "unavailable"),
    (card("库存不足 3 Available", button=ORDER), "unavailable"),
    (card("3 Available 0 Available"), "unavailable"),
    (card("3 Available 4 Available", button=ORDER), "unknown"),
    (card("-1 Available", button=ORDER), "unknown"),
    (card("1.5 Available", button=ORDER), "unknown"),
    (card("1,000 Available", button=ORDER), "unknown"),
    (card("暂无库存"), "unavailable"),
    (card("缺货"), "unavailable"),
    (card("售罄"), "unavailable"),
    (card("", button='<button disabled>Order Now</button>'), "unknown"),
    (card("", button='<a class="disabled" href="/cart.php">Order Now</a>'), "unknown"),
    (card("", button='<a href="#">Order Now</a>'), "unknown"),
    (card("", button='<a href="https://example.invalid/cart">Order Now</a>'), "unknown"),
    (card("Order Now"), "unknown"),  # 文案不是可操作按钮。
    (card("", button='<button>立即订购</button>'), "available"),
    (card("", button='<button>现在订购</button>'), "available"),
    (card('<span hidden>3 Available</span>'), "unknown"),
    (card('<script>"3 Available"</script>'), "unknown"),
    (card("3 Available") + card("3 Available"), "unknown"),
    (card("", name=PRODUCT + "Plus", button=ORDER), "unknown"),
    (f'<h3>{PRODUCT}</h3><div>3 Available</div>', "unknown"),
    (f'<div id="products"><h3>{PRODUCT}</h3><div class="product"><h3>Pro</h3>{ORDER}</div></div>', "unknown"),
    (f'<div id="products"><h3>{PRODUCT}</h3><div>JP.TKY.TRI.Pro 3 Available</div></div>', "unknown"),
    (card("") + card("5 Available", "JP.TKY.TRI.Pro", ORDER), "unknown"),
])
def test_parser_in_real_dom(page, html, status):
    page.set_content(html)
    assert m.inspect_page(page, PRODUCT).status == status


def test_different_product_and_http_error(page):
    page.set_content(card("9 Available", "JP.TKY.BGP.Pro"))
    assert m.inspect_page(page, "JP.TKY.BGP.Pro").status == "available"
    assert m.inspect_page(page, "JP.TKY.BGP.Pro", 403).status == "unknown"


def test_check_stock_visits_once_with_mock_browser(monkeypatch, tmp_path):
    page = MagicMock()
    page.url = m.Config.product_url
    page.goto.return_value.status = 200
    page.evaluate.return_value = dict(title="Products", body="", challenge=False, count=1,
                                     text=PRODUCT + " 0 Available", buttons=[])
    playwright = MagicMock()
    page.goto.return_value.headers = {'content-type': 'text/html'}
    page.title.return_value = 'Products'
    context = playwright.chromium.launch_persistent_context.return_value
    context.pages = [page]
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    monkeypatch.setattr(m, "sync_playwright", lambda: manager)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    assert m.check_stock(m.Config()).status == "unavailable"
    kwargs = playwright.chromium.launch_persistent_context.call_args.kwargs
    assert kwargs["timezone_id"] == "Asia/Tokyo"
    assert kwargs["locale"] == "en-US"
    page.goto.assert_called_once()


def test_browser_exception_becomes_unknown(monkeypatch):
    monkeypatch.setattr(m, "sync_playwright", MagicMock(side_effect=RuntimeError("private error")))
    result = m.check_stock(m.Config())
    assert result.status == "unknown"
    assert "private error" not in result.evidence


def test_cycles_unknown_restart_and_smtp_retry(tmp_path):
    cfg = m.Config(error_after=2)
    path = tmp_path / "state.json"
    state = m.fresh_state(cfg)
    sent = []

    def sender(cfg, subject, body):
        sent.append((subject, body))
        return True

    def step(status, send=sender):
        nonlocal state
        state = m.process_result(cfg, state, m.Result(status, "evidence"), path, send)
        state = m.load_state(path, cfg)  # 每次从磁盘恢复，验证重启去重。

    step("unavailable")
    step("unknown")
    assert state["last_status"] == "unavailable"
    step("available", lambda *args: False)
    assert not state["alerted_for_current_stock"]
    step("available")
    step("available")
    step("unknown")
    step("unknown")
    step("unknown")
    assert state["last_status"] == "available"
    assert len(sent) == 2
    step("available")
    assert not state["error_alert_sent"]
    step("unknown")
    step("unknown")
    assert len(sent) == 3
    step("unavailable")
    step("available")
    assert len(sent) == 4
    assert "[VMISS 到货]" in sent[-1][0]
    assert cfg.product_url in sent[-1][1]


def test_error_email_retries_and_first_available(tmp_path):
    cfg = m.Config(error_after=1)
    path = tmp_path / "state.json"
    failed = MagicMock(return_value=False)
    sent = MagicMock(return_value=True)
    state = m.process_result(cfg, m.fresh_state(cfg), m.Result(), path, failed)
    assert not state["error_alert_sent"]
    state = m.process_result(cfg, state, m.Result(), path, sent)
    assert state["error_alert_sent"]
    m.process_result(cfg, state, m.Result("available", "3 Available"), path, sent)
    assert sent.call_count == 2


def test_state_corruption_and_changed_target(tmp_path):
    cfg = m.Config()
    path = tmp_path / "state.json"
    path.write_text('{"broken": true}')
    with pytest.raises(ValueError, match="State file"):
        m.load_state(path, cfg)
    m.save_state(path, m.fresh_state(cfg))
    assert m.load_state(path, replace(cfg, product_name="JP.TKY.BGP.Pro"))["product_name"] == "JP.TKY.BGP.Pro"
    assert not list(tmp_path.glob("state.json.*"))
    assert path.stat().st_mode & 0o077 == 0


def test_state_write_failure_prevents_email(tmp_path, monkeypatch):
    sender = MagicMock(return_value=True)
    monkeypatch.setattr(m, "save_state", MagicMock(side_effect=OSError()))
    with pytest.raises(OSError):
        m.process_result(m.Config(), m.fresh_state(m.Config()), m.Result("available"), tmp_path / "state.json", sender)
    sender.assert_not_called()


def test_smtp_tls_and_safe_failure(monkeypatch, capsys):
    cfg = replace(m.Config(), smtp_user="unit-user", smtp_password="unit-password",
                  smtp_from="sender@example.invalid", mail_to="recipient@example.invalid")
    smtp = MagicMock()
    smtp.send_message.return_value = {}
    monkeypatch.setattr(m.smtplib, "SMTP", MagicMock(return_value=smtp))
    assert m.send_email(cfg, "Test", "Body")
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with(cfg.smtp_user, cfg.smtp_password)
    methods = [call[0] for call in smtp.method_calls]
    assert methods.index("starttls") < methods.index("login") < methods.index("send_message")
    m.setup_logging(True)
    smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"unit-password")
    assert not m.send_email(cfg, "Test", "Body")
    output = capsys.readouterr().err
    assert "authentication failed" in output
    assert cfg.smtp_password not in output


def test_ssl_and_recipient_refusal(monkeypatch):
    smtp = MagicMock()
    smtp.send_message.return_value = {"recipient@example.invalid": (550, b"refused")}
    monkeypatch.setattr(m.smtplib, "SMTP_SSL", MagicMock(return_value=smtp))
    assert not m.send_email(replace(m.Config(), smtp_port=465), "Test", "Body")
    smtp.starttls.assert_not_called()


def test_modes_are_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "load_config", lambda **kwargs: m.Config())
    check = MagicMock(return_value=m.Result("unavailable", "0 Available"))
    send = MagicMock(return_value=True)
    monkeypatch.setattr(m, "check_stock", check)
    monkeypatch.setattr(m, "send_email", send)
    assert m.main(["--once"]) == 0
    assert not (tmp_path / "state.json").exists()
    send.assert_not_called()
    check.reset_mock()
    assert m.main(["--test-email"]) == 0
    check.assert_not_called()
    send.assert_called_once_with(m.Config(), "[VMISS Monitor] Test Email", "SMTP configuration is working.")


def test_config_clamp_and_redaction(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setenv("CHECK_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("SMTP_PASSWORD", "unit-only-sentinel")
    assert m.load_config().interval == 30
    assert "unit-only-sentinel" not in repr(m.load_config())
    formatter = m.RedactingFormatter("%(message)s")
    record = logging.LogRecord("test", logging.INFO, "", 1, "failed unit-only-sentinel", (), None)
    assert formatter.format(record) == "failed [REDACTED]"

