"""Interactive setup only. The user enters Telegram's codes in their own terminal."""
import argparse
import asyncio
from getpass import getpass
from pathlib import Path
import re
import secrets
from dotenv import dotenv_values, set_key

async def authorize(path,state):
    from telethon import TelegramClient
    path.parent.mkdir(parents=True,exist_ok=True)
    path.touch(exist_ok=True)
    values=dict(dotenv_values(path,interpolate=False))
    print('Telegram independent monitoring session setup')
    print('1. Create your API application at https://my.telegram.org/apps')
    print('2. Enter the API ID and API hash here. Do not send them in chat.')
    api_id=values.get('TELEGRAM_API_ID') or input('API ID: ').strip()
    api_hash=values.get('TELEGRAM_API_HASH') or getpass('API hash (hidden): ').strip()
    if not api_id.isdigit() or not re.fullmatch('[a-fA-F0-9]{32}',api_hash):
        raise SystemExit('Invalid API ID/hash format.')
    state.mkdir(parents=True,exist_ok=True)
    client=TelegramClient(str(state/'telegram'),int(api_id),api_hash)
    try:
        await client.start(phone=lambda:input('Phone number including country code: ').strip(),
            code_callback=lambda:getpass('Telegram login code (hidden): ').strip(),
            password=lambda:getpass('Telegram two-step verification password (hidden): '))
        me=await client.get_me()
        values.update(TELEGRAM_API_ID=api_id,TELEGRAM_API_HASH=api_hash,
            TELEGRAM_CHAT_ID=str(me.id),STATE_DIR=str(state))
        if not values.get('TELEGRAM_BOT_TOKEN'):
            print('Creating the notification bot through the official BotFather...')
            async with client.conversation('BotFather',timeout=60) as conversation:
                await conversation.send_message('/newbot')
                await conversation.get_response()
                await conversation.send_message('VPS Inventory and Link Monitor')
                await conversation.get_response()
                username='daidaide_vps_'+secrets.token_hex(4)+'_bot'
                await conversation.send_message(username)
                answer=await conversation.get_response()
                match=re.search(r'\b\d{6,12}:[A-Za-z0-9_-]{30,60}\b',answer.raw_text)
                if not match:
                    raise RuntimeError('BotFather did not issue a token. Create a bot manually and save TELEGRAM_BOT_TOKEN in the private config.')
                values['TELEGRAM_BOT_TOKEN']=match[0]
                values['TELEGRAM_BOT_USERNAME']=username
            await client.send_message(username,'/start')
        for key,value in values.items():
            if value is not None:set_key(str(path),key,str(value),quote_mode='always')
        print('Authorization and bot setup completed. Secrets saved only to the private local config.')
    finally:await client.disconnect()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--state',required=True)
    args=parser.parse_args();asyncio.run(authorize(Path(args.config),Path(args.state)))
if __name__=='__main__':main()
