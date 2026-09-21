"""Query ZgoCloud/RFCHOST/V.PS on the VPS; no web-triggered checks or credentials."""
import argparse
import json
import logging
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from stock_targets import INTERVAL, TARGETS
from export_status import _inventory_history, _read_json

STATE = Path('/var/lib/vps-stock-monitor/http-status.json')
MAX_HTML = 1024 * 1024
LOG = logging.getLogger('vps-stock-monitor')


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
    if (re.search(r'cf-chl-|/cdn-cgi/challenge-platform', html, re.I)
            or (provider != 'V.PS' and re.search(r'g-recaptcha|h-captcha|cf-turnstile', html, re.I))):
        return unknown('网站要求验证，本轮无法确认库存')
    parser = VisibleText()
    parser.feed(html)
    text = re.sub(r'\s+', ' ', ' '.join(parser.parts)).strip()
    if re.search(r'just a moment|checking your browser|verify (?:that )?you are human|access denied|captcha|验证码|人机验证', text, re.I):
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


def check_http(target):
    try:
        request = Request(target['product_url'], headers={
            'User-Agent': 'VPSStockMonitor/2.0 (+https://github.com/daidaideaa/vps_monitor)',
            'Accept': 'text/html', 'Accept-Language': 'en-US,en;q=0.9',
        })
        with urlopen(request, timeout=20) as response:
            if (response.status != 200 or not allowed_url(response.url, target)
                    or response.headers.get_content_type() not in {'text/html', 'application/xhtml+xml'}
                    or response.headers.get('cf-mitigated') == 'challenge'
                    or response.headers.get('Content-Range')):
                return unknown('商家响应异常或要求验证')
            body = response.read(MAX_HTML + 1)
            if len(body) > MAX_HTML:
                return unknown('商家页面过大，未解析')
            return parse_stock(body.decode(response.headers.get_content_charset() or 'utf-8', errors='replace'), target['provider'], target)
    except Exception:
        return unknown('商家请求失败、超时或要求验证')


def check_browser(target):
    # Reuse the installed Chromium binary, never the VMISS browser session/profile.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chromium', headless=True,
                                                  args=['--disable-dev-shm-usage'])
            try:
                page = browser.new_page(locale='en-US')
                page.route('**/*', lambda route: route.abort() if route.request.resource_type in {'image', 'media', 'font'} else route.continue_())
                response = page.goto(target['product_url'], wait_until='domcontentloaded', timeout=30000)
                if (response is None or response.status != 200 or not allowed_url(page.url, target)
                        or response.headers.get('cf-mitigated') == 'challenge'
                        or response.headers.get('content-range')
                        or not re.match(r'^(text/html|application/xhtml\+xml)\b', response.headers.get('content-type', ''), re.I)):
                    return unknown('浏览器访问未获得正常商家页面')
                # A short bounded render wait, without clicking or solving challenges.
                page.wait_for_timeout(1500)
                if not allowed_url(page.url, target):
                    return unknown('浏览器离开目标商家页面')
                html = page.content()
                if len(html.encode('utf-8')) > MAX_HTML:
                    return unknown('商家页面过大，未解析')
                return parse_stock(html, target['provider'], target)
            finally:
                browser.close()
    except Exception:
        return unknown('浏览器检查失败、超时或要求验证')


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
               'check_interval_seconds': INTERVAL, 'query_location': 'hong-kong-vps'}
    product.update(_inventory_history(product, previous))
    return product


def run_once(output=STATE, http_check=check_http, browser_check=check_browser):
    state = _read_json(output) or {}
    previous = {p['id']: p for p in state.get('products', []) if isinstance(p, dict) and 'id' in p}
    products = dict(previous)
    for target in TARGETS:
        result = http_check(target)
        method = 'http'
        if result['status'] == 'unknown':
            result = browser_check(target)
            method = 'playwright'
        checked = datetime.now(timezone.utc).isoformat()
        product = update_product(target, result, previous.get(target['id']), checked)
        products[target['id']] = product
        # Commit each product independently, so a later failure cannot erase its result.
        data = {'schema_version': 2, 'products': [products[t['id']] for t in TARGETS if t['id'] in products], 'published_at': checked}
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False) + '\n', encoding='utf-8')
        os.chmod(temporary, 0o600)
        temporary.replace(output)
        LOG.info('%s %s status=%s method=%s', target['provider'], target['product_name'], product['status'], method)
    return data


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--once', action='store_true', help='Run a single scheduled check (the default)')
    cli.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    # systemd already serializes its service; flock also protects manual invocations.
    import fcntl
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_once()
