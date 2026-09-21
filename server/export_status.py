"""Publish only allowlisted, non-secret stock monitor fields."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

VMISS_MONITOR = Path('/opt/vmiss-stock-monitor')
HTTP_MONITOR_STATE = Path('/var/lib/vps-stock-monitor/http-status.json')
OUTPUT = Path('/var/lib/vmiss-public-status/status.json')
STATES = {'available', 'unavailable', 'unknown'}
ALLOWED_HOSTS = {
    'VMISS': 'app.vmiss.com',
    'ZgoCloud': 'clients.zgovps.com',
    'RFCHOST': 'my.rfchost.com',
}
FALLBACK_URLS = {
    'VMISS': 'https://app.vmiss.com/',
    'ZgoCloud': 'https://clients.zgovps.com/index.php?/cart/tokyo-intel-vps/',
    'RFCHOST': 'https://my.rfchost.com/index.php?rp=/store/jp-2-china-optimization-network-lite',
}
EXPECTED_HTTP = (
    ('zgocloud-tokyo-intel-starter', 'ZgoCloud', 'Tokyo Intel VPS · Starter'),
    ('rfchost-jp2-co-micro-lite', 'RFCHOST', 'JP2-CO-Micro-Lite'),
)


def _safe_url(provider, value):
    url = str(value or '')
    parsed = urlsplit(url)
    if parsed.scheme == 'https' and parsed.hostname == ALLOWED_HOSTS.get(provider):
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
    if not isinstance(stock, int) or stock < 0 or current == 'unknown':
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
    }


def snapshot(state, interval=180):
    """Backward-compatible VMISS single-product snapshot used by tests/tools."""
    target = state.get('target') or {}
    merged = dict(state)
    merged['product_name'] = target.get('product_name', '未知套餐')
    merged['product_url'] = target.get('product_url', '')
    merged['status'] = 'unknown' if int(state.get('unknown_count', 0) or 0) else state.get('last_confirmed', 'unknown')
    public = _public_product(merged, 'VMISS', 'vmiss-jp-tky-tri-basic', interval)
    return {'schema_version': 1, **{k: v for k, v in public.items() if k not in {'id', 'provider'}}}


def combined_snapshot(vmiss_state=None, http_state=None, interval=180):
    products = []
    if vmss := vmiss_state:
        legacy = snapshot(vmss, interval)
        products.append({
            'id': 'vmiss-jp-tky-tri-basic',
            'provider': 'VMISS',
            **{k: v for k, v in legacy.items() if k != 'schema_version'},
        })

    http_products = {}
    if isinstance(http_state, dict):
        for item in http_state.get('products', []):
            if isinstance(item, dict) and item.get('id'):
                http_products[item['id']] = item
    for product_id, provider, name in EXPECTED_HTTP:
        state = http_products.get(product_id, {
            'product_name': name,
            'product_url': FALLBACK_URLS[provider],
            'status': 'unknown',
            'last_confirmed': 'unknown',
            'unknown_count': 1,
            'last_unknown_reason': 'monitor has not produced a state yet',
        })
        products.append(_public_product(state, provider, product_id, interval))

    return {
        'schema_version': 2,
        'products': products,
        'published_at': datetime.now(timezone.utc).isoformat(),
    }


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def main():
    vmiss_state = _read_json(VMISS_MONITOR / 'state.json')
    config = dotenv_values(VMISS_MONITOR / '.env')
    interval = max(30, int(config.get('CHECK_INTERVAL_SECONDS', 180)))
    data = combined_snapshot(vmiss_state, _read_json(HTTP_MONITOR_STATE), interval)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o644)
    temporary.replace(OUTPUT)


if __name__ == '__main__':
    main()
