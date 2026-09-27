"""One private Telegram user session; only allowlisted channels are processed."""
import argparse
import asyncio
import json
import os
import time
import urllib.request
from pathlib import Path
from dotenv import dotenv_values
from .store import Store
from .targets import CHANNELS
from .stock import parse_message, accept, snapshot
from .notify import drain_async


async def run(config, login=False):
    from telethon import TelegramClient, events, errors
    from telethon.tl.functions.channels import JoinChannelRequest
    root = Path(config.get('STATE_DIR', '/var/lib/vps-observatory'))
    root.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(root/'telegram'), int(config['TELEGRAM_API_ID']), config['TELEGRAM_API_HASH'], catch_up=True)
    if login:
        await client.start()
        print('Independent Telegram session authorized. No session data printed.')
        await client.disconnect(); return
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError('Run --login interactively before starting the service')
    store = Store(root/'stock.sqlite')
    async def retry_flood(fn):
        while True:
            try:return await fn()
            except errors.FloodWaitError as exc:
                store.set('telegram_health',{'state':'rate_limited','checked_at':time.time(),'retry_at':time.time()+exc.seconds})
                await asyncio.sleep(exc.seconds+1)
    peers = {}
    entities = []
    for name in CHANNELS:
        entity = await retry_flood(lambda:client.get_entity(name))
        peers[entity.id] = name
        entities.append(entity)
        if not store.get('joined:'+name):
            await retry_flood(lambda:client(JoinChannelRequest(entity)))
            store.set('joined:'+name, True)
    def process(message, baseline=False):
        chat = message.peer_id.channel_id if hasattr(message.peer_id, 'channel_id') else None
        if chat not in peers:
            return
        urls = [e.url for e in message.entities or [] if getattr(e, 'url', None)]
        at = (message.edit_date or message.date).timestamp()
        accept(store, parse_message(peers[chat], message.id, message.message or '', at, urls), baseline)
    @client.on(events.NewMessage(chats=entities))
    @client.on(events.MessageEdited(chats=entities))
    async def received(event):
        process(event.message)
    # Read recent history, including edits, to recover interrupted sessions.
    first = not store.get('initialized')
    for peer in entities:
        history = await retry_flood(lambda:client.get_messages(peer,limit=300))
        for message in sorted(history, key=lambda m: (m.edit_date or m.date, m.id)):
            process(message, baseline=first)
    store.set('initialized', True)
    await retry_flood(client.catch_up)
    last_publish = None
    last_trim = 0
    delivery = None
    def publish(data):
        request = urllib.request.Request(config['STATUS_PUBLISH_URL'], data=json.dumps(data).encode(), method='PUT',
            headers={'Authorization': 'Bearer '+config['STATUS_PUBLISH_TOKEN'], 'Content-Type': 'application/json',
                'User-Agent':'vps-observatory/1.0 (+https://github.com/daidaideaa/vps_monitor)'})
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 204: raise RuntimeError('publish_failed')
    while True:
        health = {'state': 'connected' if client.is_connected() else 'disconnected', 'checked_at': time.time()}
        store.set('telegram_health', health)
        if delivery is None or delivery.done():
            if delivery: delivery.result()
            delivery = asyncio.create_task(drain_async(store, config))
        data = snapshot(store)
        digest = json.dumps(data['products'], sort_keys=True)
        if digest != last_publish or time.time()-store.get('last_publish', 0) > 60:
            try:
                await asyncio.to_thread(publish, data)
                last_publish = digest; store.set('last_publish', time.time())
            except Exception as exc:
                print('Stock publish:', type(exc).__name__, flush=True)
        if time.time()-last_trim > 3600:
            store.trim(max_bytes=20*1024*1024); last_trim = time.time()
        await asyncio.sleep(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--login', action='store_true')
    args = parser.parse_args()
    config = {**dotenv_values(args.config, interpolate=False), **os.environ}
    if not config.get('TELEGRAM_API_ID') or not config.get('TELEGRAM_API_HASH'):
        raise SystemExit('Telegram API credentials missing; configure the private environment file.')
    asyncio.run(run(config, args.login))


if __name__ == '__main__':
    main()
