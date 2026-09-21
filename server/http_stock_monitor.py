"""Check ZgoCloud and RFCHOST stock without exposing credentials.

Runs once per invocation. systemd timer controls the 180-second cadence.
Direct HTTP is preferred; Playwright is used only as a fallback when available.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STATE_PATH = Path('/var/lib/vps-stock-monitor/http-status.json')
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/154.0.0.0 Safari/537.36'
)

TARGETS = (
    {
        'id': 'zgocloud-tokyo-intel-starter',
        'provider': 'ZgoCloud',
        'product_name': 'Tokyo Intel VPS · Starter',
        'product_url': 'https://clients.zgovps.com/index.php?/cart/tokyo-intel-vps/',
    },
    {
        'id': 'rfchost-jp2-co-micro-lite',
        'provider': 'RFCHOST',
        'product_name': 'JP2-CO-Micro-Lite',
        'product_url': 'https://my.rfchost.com/index.php?currency=8&rp=/store/jp-2-china-optimization-network-lite',
    },
)


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {'script', 'style', 'noscript'}:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in {'script', 'style', 'noscript'} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def visible_text(html: str) -> str:
    parser = _VisibleText()
    parser.feed(html)
    return re.sub(r'\s+', ' ', ' '.join(parser.parts)).strip()


def parse_zgocloud(html: str) -> tuple[str, int | None]:
    text = visible_text(html)
    if 'Tokyo Intel VPS' not in text:
        raise ValueError('Tokyo Intel VPS marker not found')
    start = text.find('Starter')
    end = text.find('Standard', start + len('Starter'))
    if start < 0:
        raise ValueError('Starter marker not found')
    segment = text[start:end if end > start else len(text)]
    if re.search(r'Out\s+of\s+stock!?', segment, re.I):
        return 'unavailable', 0
    if re.search(r'\bContinue\b', segment, re.I):
        return 'available', None
    raise ValueError('Starter stock control not recognized')


def parse_rfchost(html: str) -> tuple[str, int | None]:
    text = visible_text(html)
    start = text.find('JP2-CO-Micro-Lite')
    end = text.find('JP2-CO-Mini-Lite', start + len('JP2-CO-Micro-Lite'))
    if start < 0:
        raise ValueError('JP2-CO-Micro-Lite marker not found')
    segment = text[start:end if end > start else len(text)]
    match = re.search(r'\b(\d+)\s+Available\b', segment, re.I)
    if match:
        stock = int(match.group(1))
        return ('available' if stock > 0 else 'unavailable'), stock
    if re.search(r'Out\s+of\s+Stock', segment, re.I):
        return 'unavailable', 0
    raise ValueError('RFCHOST availability count not found')


def _http_fetch(url: str, timeout: int = 25) -> str:
    request = Request(
        url,
        headers={
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8',
            'Cache-Control': 'no-cache',
        },
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or 'utf-8'
        return raw.decode(charset, errors='replace')


def _playwright_fetch(url: str) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError('Playwright fallback is unavailable') from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(user_agent=USER_AGENT, locale='en-US')
            page.goto(url, wait_until='domcontentloaded', timeout=45_000)
            page.wait_for_timeout(1_500)
            return page.content()
        finally:
            browser.close()


def fetch_page(url: str) -> str:
    try:
        return _http_fetch(url)
    except (HTTPError, URLError, TimeoutError, OSError):
        return _playwright_fetch(url)


def check_target(target: dict) -> tuple[str, int | None]:
    html = fetch_page(target['product_url'])
    if target['provider'] == 'ZgoCloud':
        return parse_zgocloud(html)
    if target['provider'] == 'RFCHOST':
        return parse_rfchost(html)
    raise ValueError(f"Unsupported provider: {target['provider']}")


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {'schema_version': 1, 'products': []}


def run_once(state_path: Path = STATE_PATH) -> dict:
    previous = _load_state(state_path)
    old_by_id = {
        product.get('id'): product
        for product in previous.get('products', [])
        if isinstance(product, dict) and product.get('id')
    }
    checked_at = datetime.now(timezone.utc).isoformat()
    products = []
    for target in TARGETS:
        old = old_by_id.get(target['id'], {})
        confirmed = old.get('last_confirmed', 'unknown')
        unknown_count = max(0, int(old.get('unknown_count', 0) or 0))
        try:
            status, stock = check_target(target)
            confirmed = status
            unknown_count = 0
            reason = ''
            current = status
        except Exception as exc:  # a failed read must never become "available"
            current = 'unknown'
            stock = None
            unknown_count += 1
            reason = f'{type(exc).__name__}: {exc}'[:300]
        products.append({
            **target,
            'status': current,
            'last_confirmed': confirmed if confirmed in {'available', 'unavailable', 'unknown'} else 'unknown',
            'last_checked': checked_at,
            'unknown_count': unknown_count,
            'last_unknown_reason': reason,
            'stock': stock,
        })
    state = {'schema_version': 1, 'products': products, 'updated_at': checked_at}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(state_path.suffix + '.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o644)
    temporary.replace(state_path)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description='Check ZgoCloud and RFCHOST stock once')
    parser.add_argument('--state', type=Path, default=STATE_PATH)
    args = parser.parse_args()
    state = run_once(args.state)
    summary = ', '.join(f"{p['provider']}={p['status']}" for p in state['products'])
    print(summary)


if __name__ == '__main__':
    main()
