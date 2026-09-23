"""Query ZgoCloud/RFCHOST/V.PS on the VPS; no web-triggered checks or credentials."""
import argparse
import json
import logging
import os
import re
import sys
import time
import socket
import subprocess
import select
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen, build_opener, ProxyHandler
from urllib.error import HTTPError

from stock_targets import INTERVAL, TARGETS
from export_status import VMISS_MONITOR, _inventory_history, _read_json

STATE = Path('/var/lib/vps-stock-monitor/http-status.json')
MAX_HTML = 1024 * 1024
LOG = logging.getLogger('vps-stock-monitor')
# The shared policy ships with the standalone VMISS engine as well.
sys.path.insert(0, os.environ.get('VMISS_MONITOR_ROOT', str(
    Path(__file__).resolve().parents[1] / 'vmiss-stock-monitor'
    if (Path(__file__).resolve().parents[1] / 'vmiss-stock-monitor').is_dir() else VMISS_MONITOR)))
from access_policy import backoff, diagnostic, exception_category, observe_page, response_category, retry_remaining

CST = timezone(timedelta(hours=8), name='Asia/Shanghai')


def make_alert(product, event, config):
    message = EmailMessage()
    message['From'], message['To'] = config.smtp_from, config.mail_to
    message['Date'] = format_datetime(datetime.now(CST))
    message['Message-ID'] = make_msgid()
    label = '到货' if event == 'stock' else '监控异常'
    message['Subject'] = f"[{product['provider']} {label}] {product['product_name']}"
    checked = datetime.fromisoformat(product['last_checked']).astimezone(CST).isoformat()
    if event == 'stock':
        stock = product.get('stock')
        evidence = f'{stock} Available' if type(stock) is int else '目标套餐的订购控件已启用'
        detail = f"状态：available\n库存依据：{evidence}\n请尽快手动进入官方页面确认并下单。"
    else:
        detail = (f"连续 {product['unknown_count']} 次无法确认库存。\n"
                  'unknown 不表示有货。恢复正常前不重复发送此异常提醒。')
    message.set_content(f"套餐：{product['product_name']}\n检测时间（北京时间）：{checked}\n"
                        f"{detail}\n\n官方购买页面：\n{product['product_url']}\n")
    return message


def mail_config():
    # Reuse the deployed monitor's validated TLS SMTP transport; never copy secrets.
    if str(VMISS_MONITOR) not in sys.path:
        sys.path.insert(0, str(VMISS_MONITOR))
    from monitor import load_config, send_email
    return load_config(require_smtp=True), send_email


def send_alert(product, event):
    config, send = mail_config()
    message = make_alert(product, event, config)
    if not send(config, str(message['Subject']), message.get_content()):
        raise RuntimeError('SMTP delivery failed; notification remains pending')


def notify_product(product, sender, error_after):
    event = None
    if product['status'] == 'available' and not product['stock_notified']:
        event = 'stock'
    elif product['status'] == 'unknown' and product['unknown_count'] >= error_after and not product['error_notified']:
        event = 'error'
    if event and sender:
        try:
            sender(product, event)
        except Exception as exc:
            # Exception text may contain authentication details. Log only its type.
            LOG.error('%s email failed (%s); retry after next matching check', product['id'], type(exc).__name__)
        else:
            product['stock_notified' if event == 'stock' else 'error_notified'] = True
            LOG.info('%s %s email accepted by SMTP', product['id'], event)


def save_snapshot(output, products, checked):
    data = {'schema_version': 2, 'products': [products[t['id']] for t in TARGETS if t['id'] in products], 'published_at': checked}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o600)
    temporary.replace(output)
    return data


def unknown(reason='未能可靠识别目标套餐库存'):
    return {'status': 'unknown', 'stock': None, 'explanation': reason}


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = (any(frame['hidden'] for frame in self.stack)
                  or tag in {'script', 'style', 'noscript', 'template'}
                  or 'hidden' in attrs or attrs.get('aria-hidden') == 'true'
                  or bool(re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', attrs.get('style', ''), re.I)))
        disabled = ('disabled' in attrs or attrs.get('aria-disabled') == 'true'
                    or 'disabled' in attrs.get('class', '').split())
        href = attrs.get('href', '') or ''
        order = not hidden and not disabled and (
            (tag == 'button' and attrs.get('type', 'submit') == 'submit')
            or (tag == 'a' and href and not href.lower().startswith(('#', 'javascript:', 'data:'))))
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append({'tag': tag, 'hidden': hidden, 'order': order, 'text': []})

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            frame = self.stack[index]
            if frame['tag'] == tag:
                if frame['order'] and ' '.join(' '.join(frame['text']).split()) == 'Continue':
                    self.parts.append(' ENABLED_CONTINUE_CONTROL ')
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not any(frame['hidden'] for frame in self.stack):
            self.parts.append(data)
            for frame in self.stack:
                if frame['order']:
                    frame['text'].append(data)


class VpsCards(VisibleText):
    """Read HostBill product cards by ID and heading, including badges before titles."""
    def __init__(self):
        super().__init__()
        self.cards = []
        self.locations = []
        self.order_enabled = False
        self.challenge = False

    def handle_starttag(self, tag, attrs):
        before = len(self.stack)
        super().handle_starttag(tag, attrs)
        if len(self.stack) > before:
            frame = self.stack[-1]
            frame.update(attrs=dict(attrs), headings=[], visible_text=[])
            if not frame['hidden'] and set(dict(attrs).get('class', '').split()) & {'g-recaptcha', 'cf-turnstile', 'h-captcha'}:
                self.challenge = True

    def handle_data(self, data):
        super().handle_data(data)
        if not any(f['hidden'] for f in self.stack):
            for frame in self.stack:
                frame['visible_text'].append(data)

    def handle_endtag(self, tag):
        frame = next((f for f in reversed(self.stack) if f['tag'] == tag), None)
        if frame and not frame['hidden']:
            text = ' '.join(' '.join(frame['visible_text']).split())
            classes = set(frame['attrs'].get('class', '').split())
            if tag == 'h4':
                for parent in self.stack:
                    if 'cart-product' in parent['attrs'].get('class', '').split():
                        parent['headings'].append(text)
            if 'cart-product' in classes:
                self.cards.append({**frame['attrs'], 'headings': frame['headings'], 'text': text})
            if {'cart-category', 'selected'} <= classes:
                self.locations.append(text)
            if (tag == 'button' and frame['order'] and text == 'Order'
                    and 'submitOrder()' in frame['attrs'].get('onclick', '')
                    and any(f['attrs'].get('id') == 'orderpage-summary' for f in self.stack)):
                self.order_enabled = True
        super().handle_endtag(tag)


def parse_vps(html, target):
    parser = VpsCards()
    parser.feed(html)
    if parser.challenge or parser.locations != ['Tokyo'] or not target:
        return unknown()
    matches = [c for c in parser.cards if c.get('data-value') == target.get('plan_id')]
    if len(matches) != 1 or matches[0]['headings'] != [target.get('plan_name')]:
        return unknown()
    card = matches[0]
    classes = set(card.get('class', '').split())
    # Never infer stock from the marketing site's permanent Order links.
    if 'outofstock' in classes or re.search(r'\b(?:Out\s+of\s+stock|Sold\s+out)\b', card['text'], re.I):
        return {'status': 'unavailable', 'stock': 0}
    selected = [c for c in parser.cards if 'selected' in c.get('class', '').split()]
    if selected == [card] and parser.order_enabled and not classes & {'disabled', 'unavailable'}:
        return {'status': 'available', 'stock': None}
    return unknown()


def parse_stock(html, provider, target=None):
    if not isinstance(html, str) or not re.search(r'<(?:html|body|div|h[1-6]|section|form)\b', html, re.I):
        return unknown()
    # RFCHOST's normal HTTP 200 catalog includes Cloudflare's background JSD.
    # That exact library path is not an interstitial; all challenge markers,
    # visible verification text and HTTP response guards remain mandatory.
    challenge_html = html.replace('/cdn-cgi/challenge-platform/scripts/jsd/main.js', '') if provider == 'RFCHOST' else html
    if (re.search(r'cf-chl-|/cdn-cgi/challenge-platform', challenge_html, re.I)
            or (provider != 'V.PS' and re.search(r'g-recaptcha|h-captcha|cf-turnstile', html, re.I))):
        return unknown('网站要求验证，本轮无法确认库存')
    parser = VisibleText()
    parser.feed(html)
    text = re.sub(r'\s+', ' ', ' '.join(parser.parts)).strip()
    if re.search(r'just a moment|checking your browser|performing security verification|verifying you are human|verify (?:that )?you are human|access denied|captcha|验证码|人机验证', text, re.I):
        return unknown('网站要求验证，本轮无法确认库存')
    if provider == 'V.PS':
        return parse_vps(html, target)
    if provider == 'ZgoCloud':
        if not re.search(r'\bTokyo Intel VPS\b', text):
            return unknown()
        first, following = 'Starter', 'Standard'
    elif provider == 'RFCHOST':
        first, following = 'JP2-CO-Micro-Lite', 'JP2-CO-Mini-Lite'
    else:
        return unknown()
    starts = list(re.finditer(r'\b' + re.escape(first) + r'\b', text))
    ends = list(re.finditer(r'\b' + re.escape(following) + r'\b', text))
    if len(starts) != 1 or len(ends) != 1 or starts[0].end() >= ends[0].start():
        return unknown()
    card = text[starts[0].end():ends[0].start()]
    if provider == 'ZgoCloud':
        if re.search(r'\b(?:Pro|Premium)\b', card):
            return unknown()
        out = bool(re.search(r'\bOut\s+of\s+stock\b', card, re.I))
        available = 'ENABLED_CONTINUE_CONTROL' in card
        if out == available:
            return unknown()
        return {'status': 'unavailable' if out else 'available', 'stock': 0 if out else None}
    if re.search(r'JP2-CO-(?:Standard|Advanced|Max)', card, re.I):
        return unknown()
    counts = re.findall(r'(?<!\S)(\d+)\s+Available\b', card, re.I)
    if len(counts) != 1 or len(re.findall(r'\bAvailable\b', card, re.I)) != 1:
        return unknown()
    stock = int(counts[0])
    if stock > 2**53 - 1 or (stock > 0 and re.search(r'Out\s+of\s+Stock', card, re.I)):
        return unknown()
    return {'status': 'available' if stock else 'unavailable', 'stock': stock}


def allowed_url(url, target):
    actual, expected = urlsplit(url), urlsplit(target['product_url'])
    return actual.scheme == 'https' and actual.hostname == expected.hostname


def check_http(target, proxy_url=None):
    started = time.monotonic()
    status, headers, final_url = None, {}, target['product_url']
    category = ''
    try:
        request = Request(target['product_url'], headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'text/html', 'Accept-Language': 'en-US,en;q=0.9',
        })
        opener = build_opener(ProxyHandler({'http': proxy_url, 'https': proxy_url} if proxy_url else {}))
        with opener.open(request, timeout=20) as response:
            status, headers, final_url = response.status, response.headers, response.url
            category = response_category(status, headers)
            if category:
                result = unknown('商家响应异常或要求验证')
            elif (not allowed_url(final_url, target)
                    or headers.get_content_type() not in {'text/html', 'application/xhtml+xml'}
                    or headers.get('Content-Range')):
                category, result = 'invalid_response', unknown('商家响应格式异常')
            else:
                body = response.read(MAX_HTML + 1)
                if len(body) > MAX_HTML:
                    category, result = 'invalid_response', unknown('商家页面过大，未解析')
                else:
                    result = parse_stock(body.decode(headers.get_content_charset() or 'utf-8', errors='replace'), target['provider'], target)
                    if result['status'] == 'unknown':
                        category = 'challenge_page' if '验证' in result.get('explanation', '') else 'parse_failure'
    except HTTPError as exc:
        status, headers, final_url = exc.code, exc.headers, exc.url
        category, result = response_category(status, headers), unknown('HTTP ' + str(status))
        exc.close()
    except Exception as exc:
        category = exception_category(exc)
        result = unknown(category)
    result['diagnostics'] = diagnostic(target['provider'], status, headers, final_url, started=started, category=category)
    return result


def inspect_browser(page, target):
    html = page.content()
    if len(html.encode('utf-8')) > MAX_HTML:
        return unknown('商家页面过大，未解析')
    result = parse_stock(html, target['provider'], target)
    return {**result, 'challenge': '验证' in result.get('explanation', '')}


def browser_profile(target, proxy_url=None):
    # Reuse existing RFCHOST/ZgoCloud paths; the two V.PS plans share one merchant.
    merchant = next(t for t in TARGETS if t['provider'] == target['provider'])
    profile = STATE.parent / 'browser-profiles' / (merchant['id'] + ('-japan' if proxy_url else ''))
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    return profile


def check_browser(target, proxy_url=None):
    if target['provider'] == 'RFCHOST':
        return check_rfchost_browser(target, proxy_url)
    started = time.monotonic()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch_persistent_context(
                str(browser_profile(target, proxy_url)),
                channel='chromium', headless=True, locale='en-US', timezone_id='Asia/Tokyo',
                proxy={'server': proxy_url} if proxy_url else None,
                args=['--disable-dev-shm-usage'], timeout=15000)
            try:
                page = browser.pages[0] if browser.pages else browser.new_page()
                for extra in browser.pages[1:]:
                    extra.close()
                return observe_page(page, target['product_url'], target['provider'],
                                    lambda page: inspect_browser(page, target), lambda url: allowed_url(url, target))
            finally:
                browser.close()
    except Exception as exc:
        category = exception_category(exc, True)
        return {**unknown(category), 'diagnostics': diagnostic(target['provider'], started=started, category=category)}


@contextmanager
def virtual_display():
    """Own the temporary X display; independent of the VMISS engine version."""
    if os.environ.get('DISPLAY'):
        yield
        return
    read_fd, write_fd = os.pipe()
    display = None
    try:
        display = subprocess.Popen(['Xvfb', '-displayfd', str(write_fd), '-screen', '0', '1280x900x24', '-nolisten', 'tcp'],
                                   pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(write_fd)
        write_fd = None
        if not select.select([read_fd], [], [], 5)[0]:
            raise TimeoutError('Virtual display startup timeout')
        number = os.read(read_fd, 32).decode('ascii').strip()
        if not number.isdecimal():
            raise RuntimeError('Virtual display did not start')
        os.environ['DISPLAY'] = ':' + number
        yield
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        os.environ.pop('DISPLAY', None)
        if display is not None:
            display.terminate()
            try:
                display.wait(timeout=2)
            except subprocess.TimeoutExpired:
                display.kill()
                display.wait(timeout=2)


def check_rfchost_browser(target, proxy_url=None):
    """Normal Chromium on a VPS virtual display; let site verification run normally."""
    native = browser = None
    started = time.monotonic()
    try:
        from playwright.sync_api import sync_playwright
        profile = browser_profile(target, proxy_url)
        with virtual_display(), sync_playwright() as playwright:
            with socket.socket() as bound:
                bound.bind(('127.0.0.1', 0))
                port = bound.getsockname()[1]
            endpoint = f'http://127.0.0.1:{port}'
            native = subprocess.Popen([
                playwright.chromium.executable_path, '--no-sandbox', '--disable-dev-shm-usage',
                '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
                '--lang=en-US',
                '--disable-extensions', '--disable-sync', '--window-size=1280,900',
                '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={port}',
                f'--user-data-dir={profile}', *([f'--proxy-server={proxy_url}'] if proxy_url else []), 'about:blank',
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, 'TZ': 'Asia/Tokyo'})
            try:
                ready = False
                deadline = time.monotonic() + 40
                control = build_opener(ProxyHandler({}))
                while time.monotonic() < deadline and native.poll() is None:
                    try:
                        # Loopback control endpoint only; no merchant scraping here.
                        with control.open(endpoint + '/json/list', timeout=2) as response:
                            tabs = json.load(response)
                        ready = any(t.get('type') == 'page' for t in tabs)
                        if ready:
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(2)
                if not ready:
                    raise TimeoutError('Browser startup timeout')
                browser = playwright.chromium.connect_over_cdp(endpoint, timeout=10000)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()
                for extra in context.pages:
                    if extra != page:
                        extra.close()
                session = context.new_cdp_session(page)
                session.send('Emulation.setTimezoneOverride', {'timezoneId': 'Asia/Tokyo'})
                session.send('Emulation.setLocaleOverride', {'locale': 'en-US'})
                context.set_extra_http_headers({'Accept-Language': 'en-US,en;q=0.9'})
                return observe_page(page, target['product_url'], target['provider'],
                                    lambda page: inspect_browser(page, target), lambda url: allowed_url(url, target))
            finally:
                if browser:
                    try:
                        context.new_cdp_session(page).send('Browser.close')
                    except Exception:
                        pass
                if native:
                    try:
                        native.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        native.terminate()
                        try:
                            native.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            native.kill()
                            native.wait(timeout=2)
    except Exception as exc:
        category = exception_category(exc, True)
        return {**unknown(category), 'diagnostics': diagnostic('RFCHOST', started=started, category=category)}


def update_product(target, result, previous, checked):
    previous = previous if isinstance(previous, dict) else {}
    if previous.get('product_url') != target['product_url']:
        previous = {}
    status = result.get('status', 'unknown')
    if status not in {'available', 'unavailable'}:
        status = 'unknown'
    old_confirmed = previous.get('last_confirmed', 'unknown')
    if old_confirmed not in {'available', 'unavailable'}:
        old_confirmed = 'unknown'
    errors = previous.get('unknown_count', 0)
    errors = errors if isinstance(errors, int) and errors >= 0 else 0
    product = {**target, 'status': status,
               'stock': None if status == 'unknown' else result.get('stock'),
               'last_confirmed': old_confirmed if status == 'unknown' else status,
               'last_checked': checked, 'unknown_count': errors + 1 if status == 'unknown' else 0,
               'last_unknown_reason': result.get('explanation', '') if status == 'unknown' else '',
               'check_interval_seconds': INTERVAL,
               'query_location': 'japan-home-vps'}
    product['stock_notified'] = False if status == 'unavailable' else previous.get('stock_notified') is True
    product['error_notified'] = previous.get('error_notified') is True if status == 'unknown' else False
    product['baseline_required'] = previous.get('baseline_required') is True
    if status != 'unknown' and product['baseline_required']:
        product['stock_notified'] = status == 'available'
        product['baseline_required'] = False
    product.update(backoff(previous, status, result.get('diagnostics', {})))
    if 'diagnostics' in result:
        product['diagnostics'] = {**result['diagnostics'], 'challenge_consecutive_count': product['challenge_consecutive_count']}
    product.update(_inventory_history(product, previous))
    return product


def run_once(output=STATE, http_check=check_http, browser_check=check_browser, sender=None, error_after=10):
    state = _read_json(output)
    corrupt = output.exists() and (state is None or not isinstance(state.get('products'), list))
    if state and not corrupt:
        rows = state['products']
        ids = [p.get('id') for p in rows if isinstance(p, dict)]
        corrupt = (len(ids) != len(rows) or any(not isinstance(i, str) for i in ids)
                   or len(set(ids)) != len(ids)
                   or any(type(p[k]) is not bool for p in rows for k in
                          ('stock_notified', 'error_notified', 'baseline_required') if k in p))
    if corrupt:
        output.replace(output.with_name(output.name + '.corrupt-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')))
        LOG.error('Invalid inventory state backed up; next confirmed checks establish a silent baseline')
        state = {}
    state = state or {}
    previous = {p['id']: p for p in state.get('products', []) if isinstance(p, dict) and 'id' in p}
    if corrupt:
        previous = {t['id']: {**t, 'baseline_required': True} for t in TARGETS}
    products = dict(previous)
    data = state
    for target in TARGETS:
        old = previous.get(target['id'], {})
        if any(p.get('provider') == target['provider'] and retry_remaining(p)
               for p in products.values()):
            continue
        if target['provider'] == 'RFCHOST':
            result, method = browser_check(target), 'playwright'
        else:
            result, method = http_check(target), 'http'
            if result['status'] == 'unknown':
                http_diag = result.get('diagnostics', {})
                result, method = browser_check(target), 'playwright'
                # Preserve evidence of a challenge if browser startup subsequently fails.
                if http_diag.get('failure_category') in {'http_403', 'cf_mitigated_challenge', 'challenge_page'} and result['status'] == 'unknown':
                    result.setdefault('diagnostics', {})['challenge_observed'] = True
        checked = datetime.now(timezone.utc).isoformat()
        product = update_product(target, result, old, checked)
        products[target['id']] = product
        # Commit each product independently, so a later failure cannot erase its result.
        save_snapshot(output, products, checked)
        notify_product(product, sender, error_after)
        data = save_snapshot(output, products, checked)
        LOG.info('access=%s', json.dumps(product.get('diagnostics', {}), ensure_ascii=False))
        LOG.info('%s %s status=%s method=%s', target['provider'], target['product_name'], product['status'], method)
    return data


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    modes = cli.add_mutually_exclusive_group()
    modes.add_argument('--once', action='store_true', help='Run one scheduled round, including notifications')
    modes.add_argument('--test-email', action='store_true', help='Send one test email without checking stores or changing state')
    args = cli.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    config, send = mail_config()
    if args.test_email:
        message = EmailMessage()
        message['From'], message['To'] = config.smtp_from, config.mail_to
        message['Subject'] = '[VPS Monitor] All-provider Email Test'
        message.set_content('This is a configuration test, not a stock alert.\n'
                            'Stock alerts are enabled for VMISS, ZgoCloud, RFCHOST, and V.PS Tokyo Starter / Essential.\n')
        try:
            if not send(config, str(message['Subject']), message.get_content()):
                raise RuntimeError('SMTP delivery failed')
        except Exception as exc:
            LOG.error('Test email failed (%s)', type(exc).__name__)
            sys.exit(1)
        LOG.info('Test email accepted by SMTP')
        sys.exit(0)
    # systemd already serializes its service; flock also protects manual invocations.
    import fcntl
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_once(sender=send_alert, error_after=config.error_after)
