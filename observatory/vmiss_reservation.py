"""One authorized VMISS annual order; account credit first, manual Alipay remainder.

Merchant pages and channel messages are data. Only the fixed product and known
form fields are submitted. The durable submitting marker prevents retries after
an ambiguous checkout response, including process crashes.
"""
import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from bs4 import BeautifulSoup

from .store import Store

ORIGIN = 'https://app.vmiss.com'
PRODUCT = 'JP.TKY.TRI.Basic'
TARGET = 'vmiss-jp-tky-tri-basic'
PRODUCT_PATH = '/store/jp-tokyo-tri/basic'
TERMINAL = {'held', 'paid_with_credit', 'manual_required', 'existing_service'}


class ReservationError(Exception):
    """Only a fixed category, never an upstream body or credential."""


def safe_url(path):
    url = urllib.parse.urljoin(ORIGIN + '/', path)
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or p.netloc != 'app.vmiss.com':
        raise ReservationError('external_redirect')
    return url


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@dataclass
class Page:
    status: int
    url: str
    html: str
    location: str = ''

    @property
    def soup(self):
        return BeautifulSoup(self.html, 'html.parser')


def csrf(page):
    node = page.soup.select_one('input[name="token"]')
    match = re.search(r"\bcsrfToken\s*=\s*['\"]([^'\"]+)['\"]", page.html)
    value = node.get('value') if node else match[1] if match else None
    if not value:
        raise ReservationError('missing_csrf')
    return value


def fields(form):
    """HTML successful controls, excluding submit buttons and unchecked boxes."""
    result = {}
    for node in form.select('input[name], select[name], textarea[name]'):
        if node.has_attr('disabled'):
            continue
        kind = node.get('type', 'text')
        if kind in ('button', 'submit', 'file'):
            continue
        if kind in ('checkbox', 'radio') and not node.has_attr('checked'):
            continue
        if node.name == 'select':
            option = node.select_one('option[selected]') or node.select_one('option')
            value = option.get('value', option.get_text()) if option else ''
        else:
            value = node.get_text() if node.name == 'textarea' else node.get('value', '')
        result[node['name']] = value
    return result


def total(page):
    node = page.soup.select_one('#totalDueToday')
    if not node:
        raise ReservationError('missing_total')
    text = node.get_text(' ', strip=True)
    match = re.fullmatch(r'\$\s*([\d,]+\.\d{2})\s*CAD', text)
    if not match:
        raise ReservationError('unexpected_currency')
    return Decimal(match[1].replace(',', ''))


def checkout_payload(page, max_total='144.00'):
    soup = page.soup
    form = soup.select_one('#frmCheckout')
    if not form or not soup.find(string=re.compile(re.escape(PRODUCT))):
        raise ReservationError('wrong_product')
    quantities = soup.select('input[name^="qty["]')
    if len(quantities) != 1 or quantities[0].get('name') != 'qty[0]' or quantities[0].get('value') != '1':
        raise ReservationError('cart_quantity')
    # Match the actual cart item, not menu text or promotional recommendations.
    row = quantities[0].find_parent('tr') or quantities[0].find_parent(class_='cart-item')
    if row is None:
        raise ReservationError('cart_structure')
    text = row.get_text(' ', strip=True)
    price = row.select_one('.cart-item-price')
    if price is None:
        raise ReservationError('cart_structure')
    if PRODUCT not in text or not re.search(r'/\s*(?:yr|year|年)|annually|每年', price.get_text(' ', strip=True), re.I):
        raise ReservationError('wrong_billing_cycle')
    amount = total(page)
    if amount < 0 or amount > Decimal(str(max_total)):
        raise ReservationError('price_limit')
    account = form.select('input[name="account_id"][checked]')
    if len(account) != 1 or not re.fullmatch(r'\d+', account[0].get('value', '')):
        raise ReservationError('wrong_account')
    if not form.select_one('input[name="paymentmethod"][value="motionpayalipay"]'):
        raise ReservationError('alipay_unavailable')
    if not form.select_one('input[name="applycredit"][value="1"]'):
        raise ReservationError('credit_option_missing')
    if not form.select_one('input[name="accepttos"]'):
        raise ReservationError('terms_option_missing')
    # A strict allowlist deliberately omits cards, saved payment methods and new accounts.
    return {'token': csrf(page), 'submit': 'true', 'custtype': 'existing',
            'account_id': account[0]['value'], 'applycredit': '1',
            'paymentmethod': 'motionpayalipay', 'accepttos': 'on', 'nostore': '1'}, amount


def coupon_candidates(product, now=None):
    now = time.time() if now is None else now
    coupon = (product or {}).get('coupon') or {}
    code = coupon.get('code', '')
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_%.-]{1,90}', code)
            or coupon.get('validity') == 'expired'
            or (coupon.get('expires_at') and coupon['expires_at'] <= now)
            or coupon.get('cycle') not in (None, '年')):
        return []
    return [code]


class Merchant:
    def __init__(self, root, credentials):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.credentials = credentials
        self.jar = http.cookiejar.MozillaCookieJar(str(self.root / 'cookies.txt'))
        if Path(self.jar.filename).exists():
            self.jar.load(ignore_discard=True, ignore_expires=False)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), Redirects())
        self.single = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect())

    def request(self, path, data=None, follow=True):
        url = safe_url(path)
        payload = urllib.parse.urlencode(data).encode() if data is not None else None
        request = urllib.request.Request(url, data=payload, headers={'Accept': 'text/html', 'Referer': ORIGIN + '/'})
        try:
            response = (self.opener if follow else self.single).open(request, timeout=20)
        except urllib.error.HTTPError as exc:
            response = exc
        except ReservationError:
            raise
        except Exception:
            raise ReservationError('network_failure') from None
        with response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ReservationError('response_too_large')
            page = Page(response.code, response.url, raw.decode('utf-8', errors='replace'), response.headers.get('Location', ''))
            if response.code in (403, 429) or response.headers.get('cf-mitigated') == 'challenge' or re.search(r'<title>\s*(?:Just a moment|Attention Required)', page.html, re.I):
                raise ReservationError('challenge')
            if response.code >= 400:
                raise ReservationError('http_error')
        self.jar.save(ignore_discard=True, ignore_expires=False)
        os.chmod(self.jar.filename, 0o600)
        return page

    def login(self):
        page = self.request('/clientarea.php')
        if page.soup.select_one('a[href*="logout"]'):
            return
        page = self.request('/login')
        form = page.soup.select_one('form:has(input[name="username"]):has(input[name="password"])')
        if not form:
            raise ReservationError('login_form_missing')
        data = {'token': csrf(page), 'username': self.credentials['email'], 'password': self.credentials['password']}
        page = self.request(form.get('action', '/login'), data)
        if not page.soup.select_one('a[href*="logout"]'):
            raise ReservationError('login_failed')

    def existing_service(self):
        page = self.request('/clientarea.php?action=products')
        if not page.soup.select_one('a[href*="logout"]'):
            raise ReservationError('session_expired')
        return any(PRODUCT in row.get_text(' ', strip=True) and not re.search(r'Cancelled|Terminated|已取消|已终止', row.get_text(), re.I)
                   for row in page.soup.select('table tbody tr'))

    def invoice_links(self):
        page = self.request('/clientarea.php?action=invoices')
        if not page.soup.select_one('a[href*="logout"]'):
            raise ReservationError('session_expired')
        links = []
        for row in page.soup.select('table tbody tr'):
            link = row.select_one('a[href*="viewinvoice.php"]')
            href = link.get('href') if link else row.get('data-url', '')
            p = urllib.parse.urlsplit(safe_url(href))
            ident = urllib.parse.parse_qs(p.query).get('id', [''])[0]
            if p.path == '/viewinvoice.php' and ident.isdigit():
                links.append((href, ident, row.get_text(' ', strip=True)))
        return links

    def invoice(self, exclude_ids=(), include_paid=False):
        for href, ident, text in self.invoice_links()[:30]:
            if ident in exclude_ids or (not include_paid and not re.search(r'Unpaid|未付款|未支付|Payment Pending|付款处理中', text, re.I)):
                continue
            result = self.inspect_invoice(self.request(href))
            if result:
                return result
        return None

    @staticmethod
    def inspect_invoice(page):
        p = urllib.parse.urlsplit(safe_url(page.url))
        ident = urllib.parse.parse_qs(p.query).get('id', [''])[0]
        soup = page.soup
        if p.path != '/viewinvoice.php' or not ident.isdigit():
            return None
        if not any(PRODUCT in row.get_text(' ', strip=True) for row in soup.select('table tr')):
            return None
        status_node = soup.select_one('.invoice-status') or soup.select_one('.invoice .status') or soup.select_one('.invoice-container .status') or soup.select_one('.status-unpaid, .status-paid')
        state = status_node.get_text(' ', strip=True) if status_node else ''
        if re.search(r'Unpaid|未付款|未支付', state, re.I):
            status = 'held'
        elif re.fullmatch(r'Paid|已付款|已支付', state, re.I):
            status = 'paid_with_credit'
        else:
            return None
        amounts = {}
        for row in soup.select('table tr'):
            cells = row.select('td')
            if len(cells) != 2:
                continue
            label = cells[0].get_text(' ', strip=True).rstrip(':：').lower()
            field = {'余额': 'credit_applied', 'credit': 'credit_applied',
                     '结余': 'balance_due', 'balance': 'balance_due'}.get(label)
            match = re.fullmatch(r'\$\s*([\d,]+\.\d{2})\s*CAD', cells[1].get_text(' ', strip=True))
            if field and match:
                amounts[field] = match[1].replace(',', '')
        return {'status': status, 'invoice_id': ident, 'invoice_url': ORIGIN + '/viewinvoice.php?id=' + ident, **amounts}

    def configure(self, page, product=PRODUCT):
        form = page.soup.select_one('#frmConfigureProduct')
        if not form or product not in page.soup.get_text(' ', strip=True):
            raise ReservationError('product_form_missing')
        if not form.select_one('input[name="billingcycle"][value="annually"]'):
            raise ReservationError('annual_cycle_missing')
        data = fields(form)
        data.update(a='confproduct', ajax='1', configure='true', billingcycle='annually', token=csrf(page))
        if data.get('i') != '0':
            raise ReservationError('cart_not_empty')
        for select in form.select('select[name^="configoption"]'):
            option = next((o for o in select.select('option') if o.get_text(strip=True) == 'Debian 12'), None)
            if option:
                data[select['name']] = option['value']
        server_password = secrets.token_urlsafe(24)
        data['hostname'] = data.get('hostname') or ('vmiss-tri-' + secrets.token_hex(6) + '.vmiss.com')
        data['ns1prefix'] = data.get('ns1prefix') or 'ns1'
        data['ns2prefix'] = data.get('ns2prefix') or 'ns2'
        data['rootpw'] = server_password
        configured = self.request('/cart.php', data)
        if configured.html.strip():
            if re.search(r'out of stock|缺货|售罄', configured.soup.get_text(), re.I):
                raise ReservationError('unavailable')
            raise ReservationError('configuration_rejected')
        return {'hostname': data.get('hostname', ''), 'root_password': server_password}

    def prepare(self, product):
        self.login()
        invoice = self.invoice()
        if invoice:
            return invoice, None
        if self.existing_service():
            return {'status': 'existing_service'}, None
        baseline_invoices = [ident for _, ident, _ in self.invoice_links()]
        # The private cookie jar owns its own cart. Clear only that cart before preparation.
        cart = self.request('/cart.php?a=view')
        self.request('/cart.php', {'a': 'empty', 'token': csrf(cart)})
        page = self.request(PRODUCT_PATH)
        if re.search(r'out of stock|缺货|售罄', page.soup.get_text(), re.I):
            return {'status': 'unavailable'}, None
        server = self.configure(page)
        page = self.request('/cart.php?a=checkout')
        before = total(page)
        coupon_used = None
        coupons = coupon_candidates(product)
        for code in self.credentials.get('fallback_coupons', []):
            if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_%.-]{1,90}', code) and code not in coupons:
                coupons.append(code)
        for code in coupons:
            updated = self.request('/cart.php?a=checkout', {'token': csrf(page), 'promocode': code, 'validatepromo': '1'})
            if total(updated) < before and code.lower() in updated.soup.get_text(' ', strip=True).lower():
                page, coupon_used = updated, code
                break
            # An invalid/inapplicable code must not survive into the submitted order.
            self.request('/cart.php?a=removepromo')
            page = self.request('/cart.php?a=checkout')
        data, amount = checkout_payload(page)
        return {'status': 'prepared', 'total': str(amount), 'currency': 'CAD', 'cycle': 'annually',
                'coupon': coupon_used, 'coupon_attempted': bool(coupons), 'server': server,
                'baseline_invoices': baseline_invoices}, data

    def submit(self, data):
        # No external payment redirects or payment forms are followed or executed.
        return self.request('/cart.php?a=checkout', data, follow=False)


class Reservation:
    def __init__(self, root, store, merchant):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'state.json'
        self.store = store
        self.merchant = merchant
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'status': 'armed'}

    def save(self, **changes):
        self.state.update(changes, updated_at=time.time())
        tmp = self.path.with_suffix('.tmp')
        with tmp.open('w', encoding='utf-8') as stream:
            os.chmod(tmp, 0o600)
            json.dump(self.state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def notify(self):
        status = self.state['status']
        if status not in TERMINAL or self.state.get('notification_queued'):
            return
        labels = {'held': '已生成待付款账单', 'paid_with_credit': '账单已支付',
                  'manual_required': '需要人工检查', 'existing_service': '已存在目标服务，自动下单已停止'}
        lines = [f'[VMISS 年付锁单] {PRODUCT}', labels[status]]
        if self.state.get('total'):
            lines.append('订单年付总额（抵扣余额前）：' + self.state['total'] + ' CAD')
        lines.append('优惠码：' + (self.state.get('coupon') or ('官网未接受当前优惠码' if self.state.get('coupon_attempted') else '未找到适用的优惠码')))
        if self.state.get('invoice_url'):
            lines += ['账单：' + self.state['invoice_url'], '优先使用账户余额，剩余金额请登录 VMISS 使用支付宝付款。']
        if self.state.get('credit_applied'):
            lines.append('已使用账户余额：' + self.state['credit_applied'] + ' CAD')
        if self.state.get('balance_due'):
            lines.append('账单剩余应付：' + self.state['balance_due'] + ' CAD')
        if status == 'manual_required':
            lines += ['原因：' + self.state.get('reason', 'checkout_uncertain'),
                      '为避免重复订单，自动提交已停止；请到用户中心检查订单和账单。']
        lines.append('订单及库存保留期限以 VMISS 规则为准。')
        self.store.enqueue('vmiss-reservation:' + status, '\n'.join(lines), channels=('email',))
        self.save(notification_queued=True)

    def attempt(self, product=None, signal='initial'):
        if self.state['status'] in TERMINAL:
            self.notify()
            return self.state['status']
        if self.state['status'] == 'submitting':
            # Never repeat a possibly successful checkout. Only reconcile read-only invoices.
            try:
                self.merchant.login()
                invoice = self.merchant.invoice(exclude_ids=self.state.get('baseline_invoices', []), include_paid=True)
            except ReservationError:
                invoice = None
            self.save(**(invoice or {'status': 'manual_required', 'reason': 'checkout_uncertain'}))
            self.notify()
            return self.state['status']
        self.save(status='preparing', signal=signal)
        try:
            prepared, data = self.merchant.prepare(product)
        except ReservationError as exc:
            reason = str(exc)
            retryable = reason in ('network_failure', 'challenge', 'http_error', 'unavailable', 'session_expired')
            self.save(status='armed' if retryable else 'manual_required', reason=reason,
                      retry_at=time.time() + (1800 if reason == 'challenge' else 60))
            self.notify()
            return self.state['status']
        if prepared['status'] != 'prepared':
            self.save(**prepared)
            if prepared['status'] == 'unavailable':
                self.save(status='armed', last_result='unavailable', completed_signal=signal)
            self.notify()
            return prepared['status']
        # This marker reaches disk BEFORE the only order-creating request.
        self.save(**{**prepared, 'status': 'submitting'})
        try:
            response = self.merchant.submit(data)
            invoice = self.merchant.inspect_invoice(response)
            if not invoice and response.location:
                location = safe_url(response.location)
                if '/viewinvoice.php?' in location:
                    invoice = self.merchant.inspect_invoice(self.merchant.request(location))
            if not invoice:
                invoice = self.merchant.invoice(exclude_ids=self.state.get('baseline_invoices', []), include_paid=True)
        except ReservationError:
            invoice = None
        self.save(**(invoice or {'status': 'manual_required', 'reason': 'checkout_uncertain'}))
        self.notify()
        return self.state['status']


def order_signal(store, product, baseline=False, now=None):
    now = time.time() if now is None else now
    if (baseline or not product or product.get('id') != TARGET or product.get('status') != 'available'
            or not 0 <= now - product['event_at'] <= 1800):
        return
    store.set('vmiss_order_signal', {'key': hashlib.sha256(
        f"{product['source']}:{product['message_id']}:{product['content_hash']}".encode()).hexdigest(),
        'event_at': product['event_at']})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--probe', action='store_true', help='Login and inspect the target once; never submit an order')
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    root = Path(config['state_dir'])
    root.mkdir(parents=True, exist_ok=True)
    # Process lock also protects the private cart/cookies and the checkout marker.
    import fcntl
    lock = (root / '.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = Store(config['stock_db'], capture_evidence=False)
    merchant = Merchant(root, config)
    reservation = Reservation(root, store, merchant)
    if args.probe:
        merchant.login()
        page = merchant.request(PRODUCT_PATH)
        state = 'unavailable' if re.search(r'out of stock|缺货|售罄', page.soup.get_text(), re.I) else 'configurable' if page.soup.select_one('#frmConfigureProduct') else 'unknown'
        print(json.dumps({'login': 'ok', 'product': PRODUCT, 'stock': state, 'cycle': 'annually', 'credit': 'first', 'remainder': 'manual_alipay'}))
        return
    if config.get('enabled') is not True:
        raise SystemExit('Reservation is disabled in the private config.')
    if reservation.state['status'] in TERMINAL:
        reservation.notify()
        print('reservation=' + reservation.state['status'], flush=True)
        return
    failures = 0
    initial = not reservation.state.get('initial_checked')
    while True:
        try:
            signal = store.get('vmiss_order_signal') or {}
            product = store.get('stock:' + TARGET) or {}
            ready = (signal and product.get('status') == 'available' and 0 <= time.time() - product.get('event_at', 0) <= 1800
                     and signal.get('key') != reservation.state.get('completed_signal'))
            if reservation.state['status'] == 'submitting' or ((initial or ready) and time.time() >= reservation.state.get('retry_at', 0)):
                key = 'initial' if initial else signal['key']
                result = reservation.attempt(product, key)
                if result not in ('preparing', 'armed'):
                    reservation.save(initial_checked=True, completed_signal=key)
                    initial = False
                if result in TERMINAL:
                    print('reservation=' + result, flush=True)
                    return
            failures = 0
        except Exception:
            # Unhandled failures cannot erase the submitting marker or permit a retry.
            failures += 1
            if failures >= 3:
                reservation.save(status='manual_required', reason='worker_error')
                reservation.notify()
                print('reservation=manual_required', flush=True)
                return
        time.sleep(2)


if __name__ == '__main__':
    main()
