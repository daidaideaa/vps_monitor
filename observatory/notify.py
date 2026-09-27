"""Durable, per-channel delivery. No credentials or Telegram URLs in logs."""
import json
import asyncio
import smtplib
import ssl
import time
import urllib.request
from email.message import EmailMessage
from urllib.error import HTTPError


def telegram(config, body):
    token, chat = config.get('TELEGRAM_BOT_TOKEN'), config.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        raise ValueError('telegram_not_configured')
    request = urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',
        data=json.dumps({'chat_id': chat, 'text': body[:4000], 'link_preview_options': {'is_disabled': True}}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=12) as response:
        if not json.load(response).get('ok'):
            raise ValueError('telegram_rejected')


def email(config, body):
    if not config.get('SMTP_HOST') or not config.get('MAIL_TO'):
        raise ValueError('email_not_configured')
    message = EmailMessage()
    message['From'] = config.get('SMTP_FROM') or config.get('SMTP_USER')
    message['To'] = config['MAIL_TO']
    message['Subject'] = body.splitlines()[0][:160]
    message.set_content(body)
    port = int(config.get('SMTP_PORT', 587))
    factory = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    kwargs = {'context': ssl.create_default_context()} if port == 465 else {}
    with factory(config['SMTP_HOST'], port, timeout=15, **kwargs) as client:
        if port != 465:
            client.starttls(context=ssl.create_default_context())
        client.login(config['SMTP_USER'], config['SMTP_PASSWORD'])
        if client.send_message(message):
            raise RuntimeError('email_refused')


def drain(store, config, senders=None):
    senders = senders or {'telegram': telegram, 'email': email}
    rows = list(store.db.execute('SELECT id,channel,body,attempts FROM outbox WHERE sent IS NULL AND next_try<=? LIMIT 10', (time.time(),)))
    for event_id, channel, body, attempts in rows:
        try:
            senders[channel](config, body)
        except Exception as exc:
            delay = min(3600, 15 * 2**min(attempts, 8))
            if isinstance(exc, HTTPError) and exc.code == 429:
                try:
                    delay = max(delay, int(json.load(exc)['parameters']['retry_after']))
                except Exception:
                    pass
            store.db.execute('UPDATE outbox SET attempts=attempts+1,next_try=? WHERE id=? AND channel=?',
                             (time.time()+delay, event_id, channel))
        else:
            store.db.execute('UPDATE outbox SET sent=? WHERE id=? AND channel=?', (time.time(), event_id, channel))
        store.db.commit()


async def drain_async(store, config):
    # SQLite stays on its owner thread; remote delivery must not block channel events.
    rows = list(store.db.execute('SELECT id,channel,body,attempts FROM outbox WHERE sent IS NULL AND next_try<=? LIMIT 10', (time.time(),)))
    async def deliver(row):
        event_id, channel, body, attempts = row
        try:
            await asyncio.to_thread({'telegram': telegram, 'email': email}[channel], config, body)
        except Exception as exc:
            delay = min(3600, 15 * 2**min(attempts, 8))
            if isinstance(exc, HTTPError) and exc.code == 429:
                try: delay = max(delay, int(json.load(exc)['parameters']['retry_after']))
                except Exception: pass
            store.db.execute('UPDATE outbox SET attempts=attempts+1,next_try=? WHERE id=? AND channel=?',
                (time.time()+delay, event_id, channel))
        else:
            store.db.execute('UPDATE outbox SET sent=? WHERE id=? AND channel=?', (time.time(), event_id, channel))
        store.db.commit()
    await asyncio.gather(*(deliver(row) for row in rows))
