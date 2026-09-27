"""Parse allowlisted stock messages into observations, not guaranteed live inventory."""
import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from urllib.parse import urlsplit, parse_qs
from .targets import TARGETS, public_target

CST = timezone(timedelta(hours=8))


def parse_message(channel, message_id, text, event_at, urls=()):
    channel = channel.lower().lstrip('@')
    text = unicodedata.normalize('NFKC', text[:16000])
    normalized = re.sub(r'\s+', ' ', text).lower()
    if re.search(r'二手|溢价|收一个|出一个|push出|拼车|\[交易\]', text, re.I):
        return None
    links = list(urls) + re.findall(r'https://[^\s<>`~]+', text)
    for target in TARGETS:
        if channel not in target['channels']:
            continue
        if not any(alias in normalized for alias in target['aliases']):
            continue
        # A plan must appear in the message's product block, not an ad appended below another product.
        main = text.split('\n\n🔥')[0].lower()
        if not any(alias in re.sub(r'\s+', ' ', main) for alias in target['aliases']):
            continue
        vendor_links = [urlsplit(u) for u in links if urlsplit(u).hostname in {target['host'], target['host'].removeprefix('www.') }]
        pids = [parse_qs(u.query).get('pid', [''])[0] for u in vendor_links]
        if any(pids) and target['pid'] not in pids:
            continue
        if target['provider'] != 'DMIT' and not vendor_links:
            continue
        state, count = 'unknown', None
        quantities = re.findall(r'(?:库存|stock)\s*[:：]\s*(?:\d+\s*(?:→|->)\s*)?(\d+)\b', text, re.I)
        quantities += re.findall(r'(?:🔴|🟢|🛒)\s*(\d+)\b', text)
        if re.search(r'已售罄|售罄|#无货|Stock\s*:\s*(?:无|无货)|out of stock|sold out', text, re.I):
            state, count = 'unavailable', 0
        elif quantities:
            count = int(quantities[-1]); state = 'available' if count else 'unavailable'
        elif re.search(r'#有货|Stock\s*:\s*有|✅[^\n]*(?:补货|有货)|🟢\s*∞|#Available', text, re.I):
            state = 'available'
        coupon = parse_coupon(text, event_at)
        if coupon:coupon['source_url']=f'https://t.me/{channel}/{message_id}'
        return {**public_target(target), 'status': state, 'stock': count,
                'source': channel, 'message_id': message_id,
                'source_url': f'https://t.me/{channel}/{message_id}',
                'event_at': event_at, 'received_at': time.time(), 'coupon': coupon,
                'prices': parse_prices(text), 'content_hash': hashlib.sha256(text.encode()).hexdigest()}
    return None


def parse_prices(text):
    results = []
    for line in text.splitlines():
        amount = re.search(r'(?:\$|USD\s*|CAD\s*)(\d+(?:\.\d+)?)', line)
        if not amount:
            continue
        currency = 'CAD' if 'CAD' in line.upper() else 'USD' if 'USD' in line.upper() else '币种待确认'
        cycle = next((label for pattern, label in [
            (r'Biennially|两年|二年', '2年'), (r'Semi|半年', '半年'),
            (r'Annually|每年|年付|/年', '年'), (r'Quarterly|每季|季付', '季'),
            (r'Monthly|每月|月付|/月', '月')] if re.search(pattern, line, re.I)), None)
        if cycle:
            results.append({'amount': amount[1], 'currency': currency, 'cycle': cycle})
    return results[:8]


def parse_coupon(text, event_at):
    match = re.search(r'(?:优惠码|折扣码|Discount Code|Promo Code|🏷️?)\s*[:：]?\s*[`「\"\']?([A-Za-z0-9][A-Za-z0-9_%.-]{1,90})', text, re.I)
    if not match:
        return None
    code = match[1]
    context = text[max(0, match.start()-60):min(len(text), match.end()+160)]
    cycle = '年' if re.search(r'年付|annual', context, re.I) else None
    expiry = re.search(r'(?:截止|有效期至|expires?)[^\d]{0,12}(20\d{2}-\d{2}-\d{2})', context, re.I)
    expires_at = None
    if expiry:
        try:
            expires_at = (datetime.fromisoformat(expiry[1]).replace(tzinfo=CST) + timedelta(days=1)).timestamp()
        except ValueError:
            pass
    percent = re.search(r'(\d{1,2})%\s*OFF', code, re.I) or re.search(r'(\d{1,2})%\s*OFF', context, re.I)
    zhe = re.search(r'(\d(?:\.\d)?)\s*折', context)
    multiplier = (Decimal(100-int(percent[1]))/100 if percent else Decimal(zhe[1])/10 if zhe else None)
    if re.search(r'最高|低至|up to|指定|满\s*\d', context, re.I):
        multiplier = None
    prices = parse_prices(text)
    candidates = [p for p in prices if not cycle or p['cycle'] == cycle]
    discounted = None
    if cycle and multiplier is not None and Decimal(0) < multiplier < Decimal(1) and len(candidates) == 1 and candidates[0]['currency'] != '币种待确认':
        p = candidates[0]
        discounted = {**p, 'amount': str((Decimal(p['amount'])*multiplier).quantize(Decimal('.01')))}
    recurring = bool(re.search(r'循环|recurring', context, re.I)) and not bool(re.search(r'不循环|非循环|不续费|non.recurring|not recurring|首期|first (?:term|payment)', context, re.I))
    return {'code': code, 'cycle': cycle, 'recurring': recurring,
            'expires_at': expires_at, 'terms': context.strip()[:240], 'discounted': discounted,
            'validity': 'expired' if expires_at and expires_at <= time.time() else 'source_reported', 'observed_at': event_at}


def notification(product, kind='补货'):
    when = datetime.fromtimestamp(product['event_at'], CST).strftime('%m-%d %H:%M:%S')
    lines = [f"[{product['provider']} {kind}] {product['product_name']}", f'频道报告时间：{when}（北京时间）',
             '库存：'+(str(product['stock']) if product['stock'] is not None else '频道报告有货')]
    for price in product.get('prices', []):
        lines.append(f"原价：{price['amount']} {price['currency']}/{price['cycle']}")
    c = product.get('coupon')
    if c and c['validity'] != 'expired':
        lines += [f"优惠码：{c['code']}", f"适用周期：{c['cycle'] or '来源未明确'}；{'循环优惠' if c['recurring'] else '续费优惠未确认'}"]
        if c.get('discounted'):
            p = c['discounted']; lines.append(f"按来源规则计算：{p['amount']} {p['currency']}/{p['cycle']}（结算待验证）")
        lines.append('有效期：'+(datetime.fromtimestamp(c['expires_at'], CST).isoformat() if c['expires_at'] else '来源未披露'))
        lines.append('条款：'+c['terms'])
    else:
        lines.append('优惠码：未找到适用且未过期的优惠码')
    if c and c.get('source_url'):lines.append('优惠来源：'+c['source_url'])
    return '\n'.join(lines+[f"库存消息：{product['source_url']}", f"官网：{product['product_url']}"])


def accept(store, product, baseline=False, now=None):
    if product is None:
        return False
    now = now or time.time()
    if product['event_at'] > now+120:
        return False
    previous = store.get('stock:'+product['id'])
    cursor = store.db.execute('SELECT edited,hash FROM messages WHERE channel=? AND id=?',
                              (product['source'], product['message_id'])).fetchone()
    if cursor and (cursor[0] > product['event_at'] or cursor[1] == product['content_hash']):
        return False
    store.db.execute('INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?)',
        (product['source'], product['message_id'], product['event_at'], product['content_hash'], json.dumps(product)))
    if previous and previous['event_at'] > product['event_at']:
        store.db.commit(); return False
    if product['status'] == 'unknown':
        # A coupon-only edit may update an available product without refreshing its stock timestamp.
        if not previous or not product.get('coupon'):
            store.db.commit(); return False
        coupon_event=product
        product={**previous,'coupon':coupon_event['coupon'],'message_id':coupon_event['message_id'],
            'content_hash':coupon_event['content_hash']}
    elif not product.get('coupon') and previous and previous.get('coupon'):
        product['coupon']=previous['coupon']
    old_coupon = (previous or {}).get('coupon') or {}
    new_coupon = product.get('coupon') or {}
    kind = '补货' if not previous or previous['status'] != 'available' else '优惠更新' if (
        new_coupon.get('code') and new_coupon.get('code') != old_coupon.get('code') and new_coupon.get('validity') != 'expired') else None
    if not baseline and kind and product['status'] == 'available' and now-product['event_at'] <= 1800:
        store.enqueue(f"stock:{product['id']}:{product['source']}:{product['message_id']}:{product['content_hash']}", notification(product, kind))
    store.set('stock:'+product['id'], product)
    return True


def snapshot(store, now=None):
    now = now or time.time(); products = []
    for target in TARGETS:
        value = store.get('stock:'+target['id'])
        item = {**public_target(target), 'status': 'unknown', 'stock': None, 'event_at': None,
                'source_url': None, 'coupon': None, 'prices': [], 'last_confirmed': 'unknown'}
        if value:
            item.update({k: value[k] for k in ('status', 'stock', 'event_at', 'source_url', 'source', 'coupon', 'prices')})
            item['last_confirmed'] = value['status']
            if now-value['event_at'] > 1800:
                item.update(status='unknown', stock=None, stale=True)
            if item['coupon'] and item['coupon']['expires_at'] and item['coupon']['expires_at'] <= now:
                item['coupon'] = {**item['coupon'], 'validity': 'expired', 'discounted': None}
        products.append(item)
    return {'schema_version': 3, 'published_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'products': products, 'collector': store.get('telegram_health', {'state': 'awaiting_authorization'})}
