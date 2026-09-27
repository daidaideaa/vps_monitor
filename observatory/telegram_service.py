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
from .targets import CHANNELS, ADMIN_GROUPS, trusted_message
from .stock import parse_message, accept, snapshot
from .notify import drain_async


async def run(config, login=False):
    from telethon import TelegramClient, events, errors
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.types import ChannelParticipantsAdmins
    root = Path(config.get('STATE_DIR', '/var/lib/vps-observatory'))
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root/'stock.sqlite',capture_evidence=False)
    store.cancel_legacy_notifications()
    store.set('telegram_health',{'state':'starting','checked_at':time.time()})
    client = TelegramClient(str(root/'telegram'), int(config['TELEGRAM_API_ID']), config['TELEGRAM_API_HASH'], catch_up=True)
    if login:
        await client.start()
        print('Independent Telegram session authorized. No session data printed.')
        await client.disconnect(); return
    await client.connect()
    if not await client.is_user_authorized():
        store.set('telegram_health',{'state':'awaiting_authorization','checked_at':time.time()})
        raise RuntimeError('Run --login interactively before starting the service')
    async def retry_flood(fn):
        while True:
            try:return await fn()
            except errors.FloodWaitError as exc:
                store.set('telegram_health',{'state':'rate_limited','checked_at':time.time(),'retry_at':time.time()+exc.seconds})
                await asyncio.sleep(exc.seconds+1)
    peers = {}
    entities = []
    admin_ids = {}
    for name in CHANNELS:
        entity = await retry_flood(lambda:client.get_entity(name))
        peers[entity.id] = name
        entities.append(entity)
        if name in ADMIN_GROUPS:
            try:admin_ids[name]={u.id for u in await retry_flood(lambda:client.get_participants(entity,filter=ChannelParticipantsAdmins))}
            except Exception:admin_ids[name]=set()
        if not store.get('joined:'+name):
            await retry_flood(lambda:client(JoinChannelRequest(entity)))
            store.set('joined:'+name, True)
    def process(message, baseline=False):
        chat = message.peer_id.channel_id if hasattr(message.peer_id, 'channel_id') else None
        if chat not in peers:
            return
        if not trusted_message(peers[chat],message.sender_id,admin_ids.get(peers[chat],set()),bool(message.fwd_from),chat):return
        urls = [e.url for e in message.entities or [] if getattr(e, 'url', None)]
        at = (message.edit_date or message.date).timestamp()
        accepted=accept(store, parse_message(peers[chat], message.id, message.message or '', at, urls), baseline, channels=('email',))
        if accepted and not baseline:store.set('last_channel_event',time.time())
    @client.on(events.NewMessage(chats=entities))
    @client.on(events.MessageEdited(chats=entities))
    async def received(event):
        process(event.message)
    # Read recent history, including edits, to recover interrupted sessions.
    first = not store.get('initialized')
    for peer in entities:
        name=peers[peer.id]
        baseline=first or (name not in ('hostmonit','vps_spiders','gcpcn') and not store.get('source_initialized:'+name))
        history = await retry_flood(lambda:client.get_messages(peer,limit=300))
        for message in sorted(history, key=lambda m: (m.edit_date or m.date, m.id)):
            process(message, baseline=baseline)
        store.set('source_initialized:'+name,True)
    if not store.get('targeted_baseline_v2'):
        # Busy channels can bury an exact plan beyond 300 posts. Bounded searches run once,
        # silently, rather than repeatedly rereading an entire channel or refreshing stock age.
        searches={'hostmonit':['JP.TKY.TRI.Basic','2213'],
                  'vps_spiders':['TYO.Pro.TINY'],'gcpcn':['JPN.Pulse.Nano']}
        for peer in entities:
            for query in searches.get(peers[peer.id],[]):
                history=await retry_flood(lambda:client.get_messages(peer,limit=15,search=query))
                for message in sorted(history,key=lambda m:(m.edit_date or m.date,m.id)):
                    process(message,baseline=True)
        store.set('targeted_baseline_v2',True)
    store.set('initialized', True)
    await retry_flood(client.catch_up)
    last_health = 0
    last_trim = 0
    delivery = None
    next_admin_refresh=time.time()+3600
    while True:
        if time.time()>=next_admin_refresh:
            for entity in entities:
                name=peers[entity.id]
                if name in ADMIN_GROUPS:
                    try:admin_ids[name]={u.id for u in await retry_flood(lambda:client.get_participants(entity,filter=ChannelParticipantsAdmins))}
                    except Exception:admin_ids[name]=set()
            next_admin_refresh=time.time()+3600
        if time.time()-last_health>=60:
            health = {'state': 'connected' if client.is_connected() else 'disconnected', 'checked_at': time.time(),
                      'last_event_at':store.get('last_channel_event'),'sources':CHANNELS}
            store.set('telegram_health', health);last_health=time.time()
        if delivery is None or delivery.done():
            if delivery: delivery.result()
            delivery = asyncio.create_task(drain_async(store, config))
        # The VPS API reads this database. No cloud publication or periodic merchant request.
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
