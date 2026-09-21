"""Publish only allowlisted, non-secret monitor status fields."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

MONITOR = Path('/opt/vmiss-stock-monitor')
OUTPUT = Path('/var/lib/vmiss-public-status/status.json')
STATES = {'available', 'unavailable', 'unknown'}


def snapshot(state, interval=180):
    target = state.get('target') or {}
    url = target.get('product_url', '')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'app.vmiss.com':
        url = 'https://app.vmiss.com/'
    confirmed = state.get('last_confirmed', 'unknown')
    if confirmed not in STATES:
        confirmed = 'unknown'
    errors = max(0, int(state.get('unknown_count', 0)))
    reason = str(state.get('last_unknown_reason', '')).lower()
    if 'cloudflare' in reason or '403' in reason:
        explanation = 'Cloudflare 验证，程序将自动尝试恢复'
    elif errors:
        explanation = '本轮未能可靠读取目标套餐，请等待下一次检查'
    else:
        explanation = '已确认目标套餐库存' if confirmed != 'unknown' else '等待首次检查'
    return {
        'schema_version': 1,
        'product_name': str(target.get('product_name', '未知套餐'))[:100],
        'product_url': url,
        'status': 'unknown' if errors else confirmed,
        'last_confirmed': confirmed,
        'last_checked': state.get('last_checked'),
        'unknown_count': errors,
        'explanation': explanation,
        'check_interval_seconds': max(30, int(interval)),
        'published_at': datetime.now(timezone.utc).isoformat(),
    }


def main():
    state = json.loads((MONITOR / 'state.json').read_text())
    config = dotenv_values(MONITOR / '.env')
    data = snapshot(state, config.get('CHECK_INTERVAL_SECONDS', 180))
    temporary = OUTPUT.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o644)
    temporary.replace(OUTPUT)


if __name__ == '__main__':
    main()
