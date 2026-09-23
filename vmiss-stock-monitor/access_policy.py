"""Shared, bounded observation and private access diagnostics; no stock parsing."""
import math
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

CHALLENGES = {'http_403', 'cf_mitigated_challenge', 'challenge_page'}


def exception_category(exc, browser=False):
    reason = getattr(exc, 'reason', exc)
    text = str(reason).lower()  # Classify locally; never log raw exceptions.
    if isinstance(reason, ssl.SSLError) or any(s in text for s in ('err_cert_', 'err_ssl_', 'certificate verify', 'tls')):
        return 'tls_failure'
    if isinstance(reason, socket.gaierror) or any(s in text for s in ('err_name_not_resolved', 'name resolution', 'getaddrinfo')):
        return 'dns_failure'
    if isinstance(reason, TimeoutError) or 'timeout' in type(exc).__name__.lower() or 'timed out' in text:
        return 'browser_timeout' if browser else 'network_timeout'
    if isinstance(reason, OSError) or 'net::err_' in text:
        return 'network_failure'
    return 'browser_failure' if browser else 'network_failure'


def response_category(status, headers):
    if str(headers.get('cf-mitigated', '')).lower() == 'challenge':
        return 'cf_mitigated_challenge'
    if status == 403:
        return 'http_403'
    return '' if status == 200 else ('browser_timeout' if status is None else 'http_error')


def diagnostic(provider, status=None, headers=None, url='', title='', started=None, category=''):
    headers = headers or {}
    parsed = urlsplit(url)
    clean = lambda value, size: re.sub(r'[\x00-\x1f\x7f]', ' ', str(value or ''))[:size]
    # No query, fragment, userinfo, cookies, DOM, response bodies or exception text.
    return {'provider': provider, 'http_status': status,
            'cf_mitigated': clean(headers.get('cf-mitigated'), 32),
            'cf_ray': clean(headers.get('cf-ray'), 80),
            'final_url_host': parsed.hostname or '', 'final_url_path': parsed.path[:200],
            'page_title': clean(title, 160),
            'elapsed_ms': round((time.monotonic() - started) * 1000) if started is not None else 0,
            'failure_category': category}


def backoff(previous, status, diag, now=None):
    now = time.time() if now is None else now
    count = previous.get('challenge_consecutive_count', 0)
    count = count if type(count) is int and count >= 0 else 0
    if status != 'unknown':
        count = 0
    elif diag.get('failure_category') in CHALLENGES or diag.get('challenge_observed'):
        count += 1
    # A network error is not evidence that a previous challenge has cleared.
    delay = (1800, 3600, 7200)[min(count, 3) - 1] if count else 0
    return {'challenge_consecutive_count': count, 'challenge_next_check_at': now + delay if delay else 0}


def retry_remaining(state, now=None):
    value = state.get('challenge_next_check_at', 0)
    if type(value) not in (int, float) or not math.isfinite(value):
        return 0
    return max(0, value - (time.time() if now is None else now))


def observe_page(page, url, provider, inspect, allowed, timeout_ms=40000):
    """One navigation; observe natural main-document replacements, never reload."""
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    document = {}
    title = ''
    category = 'browser_timeout'
    seen_challenge = False
    result = {'status': 'unknown', 'explanation': '浏览器未获得稳定商家页面'}

    def observed(response):
        nonlocal seen_challenge
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            document.update(status=response.status, headers=response.headers)
            seen_challenge |= response_category(response.status, response.headers) in CHALLENGES

    def finish(value):
        diag = diagnostic(provider, document.get('status'), document.get('headers'),
                          page.url, title, started, category)
        diag['challenge_observed'] = seen_challenge and value['status'] == 'unknown'
        return {**value, 'diagnostics': diag}

    page.on('response', observed)
    page.set_default_timeout(3000)
    try:
        try:
            response = page.goto(url, wait_until='domcontentloaded', timeout=min(timeout_ms, 20000))
            if response is not None and not document:
                document.update(status=response.status, headers=response.headers)
        except Exception as exc:
            if exception_category(exc, True) != 'browser_timeout':
                raise
            # The first document can time out while normal verification navigates.
        previous = None
        while time.monotonic() < deadline:
            # goto can time out before the first document arrives. about:blank
            # is a pending navigation, not evidence of a merchant redirect.
            if not document:
                category = 'browser_timeout'
                page.wait_for_timeout(500)
                continue
            headers = document.get('headers', {})
            category = response_category(document.get('status'), headers)
            seen_challenge |= category in CHALLENGES
            try:
                title = page.title()
            except Exception as exc:
                if 'execution context was destroyed' not in str(exc).lower():
                    raise
                previous = None
                page.wait_for_timeout(500)
                continue
            if not allowed(page.url):
                category = category or 'unexpected_redirect'
                if not seen_challenge:
                    return finish({'status': 'unknown', 'explanation': '浏览器离开目标页面'})
                previous = None
            elif not category:
                if headers.get('content-range') or not re.match(r'^(text/html|application/xhtml\+xml)\b', headers.get('content-type', ''), re.I):
                    category = 'invalid_response'
                    return finish({'status': 'unknown', 'explanation': '浏览器响应格式异常'})
                result = inspect(page)
                if result.get('challenge'):
                    category = 'challenge_page'
                    seen_challenge = True
                elif result['status'] == 'unknown':
                    category = 'parse_failure'
                signature = {k: v for k, v in result.items() if k != 'diagnostics'}
                if result['status'] != 'unknown' and signature == previous:
                    category = ''
                    return finish(result)
                previous = signature
            else:
                previous = None
            page.wait_for_timeout(500)
        final_category = response_category(document.get('status'), document.get('headers', {}))
        if final_category:
            category = final_category
        if category in CHALLENGES:
            result = {'status': 'unknown', 'explanation': 'Cloudflare Challenge / HTTP 403，等待后仍无法确认库存'}
        else:
            result = {'status': 'unknown', 'explanation': result.get('explanation', '目标套餐未稳定显示')}
            category = category or 'parse_failure'
        return finish(result)
    except Exception as exc:
        category = exception_category(exc, True)
        return finish({'status': 'unknown', 'explanation': category})
    finally:
        page.remove_listener('response', observed)
