"""Publish only allowlisted, non-secret stock monitor fields."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from stock_targets import INTERVAL, TARGETS

VMISS_MONITOR = Path('/opt/vmiss-stock-monitor')
OUTPUT = Path('/var/lib/vmiss-public-status/status.json')
VPS_STATE = Path('/var/lib/vps-stock-monitor/http-status.json')
STATES = {'available', 'unavailable', 'unknown'}
ALLOWED_HOSTS = {'VMISS': 'app.vmiss.com', 'ZgoCloud': 'clients.zgovps.com', 'RFCHOST': 'my.rfchost.com'}
FALLBACK_URLS = {'VMISS': 'https://app.vmiss.com/', **{t['provider']: t['product_url'] for t in TARGETS}}


def _safe_url(provider, value):
    url = str(value or '')
    parsed = urlsplit(url)
    if parsed.scheme == 'https' and parsed.netloc == ALLOWED_HOSTS.get(provider):
        return url
    return FALLBACK_URLS[provider]


def _explanation(errors, reason, confirmed):
    lowered = str(reason or '').lower()
    if errors and ('cloudflare' in lowered or '403' in lowered):
        return '网站验证或 HTTP 403，本轮无法确认库存'
    if errors:
        return '本轮未能可靠读取目标套餐，请等待下一次检查'
    if confirmed == 'available':
        return '已确认目标套餐有库存'
    if confirmed == 'unavailable':
        return '已确认目标套餐无库存'
    return '等待首次可靠检查'


def _public_product(state, provider, product_id, interval=180):
    confirmed = state.get('last_confirmed', 'unknown')
    if confirmed not in STATES:
        confirmed = 'unknown'
    errors = max(0, int(state.get('unknown_count', 0) or 0))
    current = state.get('status', confirmed)
    if current not in STATES:
        current = 'unknown'
    if errors:
        current = 'unknown'
    stock = state.get('stock')
    if type(stock) is not int or stock < 0 or current == 'unknown':
        stock = None
    if current == 'unavailable':
        stock = 0
    elif current == 'available' and stock == 0:
        stock = None
    return {
        'id': product_id,
        'provider': provider,
        'product_name': str(state.get('product_name', '未知套餐'))[:100],
        'product_url': _safe_url(provider, state.get('product_url')),
        'status': current,
        'last_confirmed': confirmed,
        'last_checked': state.get('last_checked'),
        'unknown_count': errors,
        'stock': stock,
        'explanation': _explanation(errors, state.get('last_unknown_reason'), confirmed),
        'check_interval_seconds': max(30, int(interval)),
        'query_location': 'hong-kong-vps',
    }


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def _inventory_history(public, previous):
    # 历史写在公开快照中，不修改 Playwright 的状态文件。
    previous = previous or {}
    if (previous.get('product_name') != public['product_name']
            or previous.get('product_url') != public['product_url']):
        previous = {}
    checked = _timestamp(public['last_checked'])
    old_checked = _timestamp(previous.get('last_checked'))
    last_available = previous.get('last_available_at')
    if _timestamp(last_available) is None:
        last_available = previous.get('last_checked') if previous.get('status') == 'available' and old_checked is not None else None
    since = None
    if checked is not None and (old_checked is None or checked >= old_checked):
        if public['status'] == 'available':
            last_available = public['last_checked']
        elif public['status'] == 'unavailable':
            old_since = previous.get('unavailable_since')
            continuous = (previous.get('status') == 'unavailable' and old_checked is not None
                          and checked - old_checked <= 3 * public['check_interval_seconds']
                          and _timestamp(old_since) is not None and _timestamp(old_since) <= checked)
            since = old_since if continuous else public['last_checked']
    return {'last_available_at': last_available, 'unavailable_since': since}


def snapshot(state, interval=180, previous=None):
    """只导出 VMISS，保持原有 schema v1 和未知状态语义。"""
    target = state.get('target') or {}
    merged = dict(state)
    merged['product_name'] = target.get('product_name', 'JP.TKY.TRI.Basic')
    merged['product_url'] = target.get('product_url', '')
    merged['status'] = 'unknown' if int(state.get('unknown_count', 0) or 0) else state.get('last_confirmed', 'unknown')
    public = _public_product(merged, 'VMISS', 'vmiss-jp-tky-tri-basic', interval)
    public.update(_inventory_history(public, previous))
    return {'schema_version': 1, **{k: v for k, v in public.items() if k not in {'id', 'provider'}}}


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def combined_snapshot(vmiss_state, other_state=None, interval=180, previous=None):
    """Only VPS checks are exported. Missing results stay unknown, never fabricated."""
    previous = previous or {}
    old_products = previous.get('products') if isinstance(previous.get('products'), list) else []
    previous_vmiss = next((p for p in old_products if isinstance(p, dict) and p.get('provider') == 'VMISS'), None)
    if previous_vmiss is None and previous.get('schema_version') == 1 and 'products' not in previous:
        previous_vmiss = previous
    vmiss = snapshot(vmiss_state, interval, previous_vmiss)
    vmiss.pop('schema_version')
    products = [{'id': 'vmiss-jp-tky-tri-basic', 'provider': 'VMISS', **vmiss}]
    others = (other_state or {}).get('products', [])
    for target in TARGETS:
        matches = [p for p in others if isinstance(p, dict) and p.get('id') == target['id']]
        state = matches[0] if len(matches) == 1 else {}
        public = _public_product({**state, **target}, target['provider'], target['id'], INTERVAL)
        # The checker persists history even if the exporter skips intermediate checks.
        public.update(_inventory_history(public, {**state, **target}))
        products.append(public)
    return {'schema_version': 2, 'query_location': 'hong-kong-vps',
            'products': products, 'published_at': datetime.now(timezone.utc).isoformat()}


def main():
    vmiss_state = _read_json(VMISS_MONITOR / 'state.json')
    config = dotenv_values(VMISS_MONITOR / '.env')
    interval = max(30, int(config.get('CHECK_INTERVAL_SECONDS', 180)))
    data = combined_snapshot(vmiss_state or {}, _read_json(VPS_STATE), interval, previous=_read_json(OUTPUT))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o644)
    temporary.replace(OUTPUT)


if __name__ == '__main__':
    main()
