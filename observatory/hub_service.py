"""Loopback-only monitoring API behind nginx. No Cloudflare or browser-stored secrets."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import gzip
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit
import zlib
from .hub_store import Hub, CST
from .store import Store
from .stock import snapshot
from .notify import email, drain


class Login:
    def __init__(self, config, sender=email):
        self.config,self.sender=config,sender
        self.challenges={};self.tokens={};self.sent=[];self.lock=threading.Lock()
        self.secret=secrets.token_bytes(32)
        self.pool=ThreadPoolExecutor(max_workers=1)

    def digest(self, value):return hmac.new(self.secret,value.encode(),hashlib.sha256).hexdigest()

    def challenge(self, now=None):
        now=now or time.time()
        with self.lock:
            self.sent=[t for t in self.sent if now-t<3600]
            if self.sent and (now-self.sent[-1]<60 or len(self.sent)>=5):raise ValueError('rate_limited')
            self.challenges={k:v for k,v in self.challenges.items() if v['until']>now}
            code=f'{secrets.randbelow(1000000):06d}';ident=secrets.token_urlsafe(24)
            self.challenges[ident]={'digest':self.digest(ident+code),'until':now+600,'tries':0,'delivery':'pending'}
            self.sent.append(now)
        def send():
            try:self.sender(self.config['notify'],f'[监控登录验证码] {code}\n10 分钟有效，只用于你自己的 VPS 监控。')
            except Exception:
                with self.lock:
                    if ident in self.challenges:self.challenges[ident]['delivery']='failed'
            else:
                with self.lock:
                    if ident in self.challenges:self.challenges[ident]['delivery']='sent'
        self.pool.submit(send)
        return {'challenge':ident,'expires_in':600,'message':'验证码已提交发送至预设的本人邮箱。'}

    def verify(self, ident, code, now=None):
        now=now or time.time()
        with self.lock:
            c=self.challenges.get(ident)
            if not c or c['until']<now or c['tries']>=5:raise ValueError('invalid_code')
            c['tries']+=1
            if not hmac.compare_digest(c['digest'],self.digest(ident+str(code))):raise ValueError('invalid_code')
            del self.challenges[ident]
            self.tokens={k:v for k,v in self.tokens.items() if v>now}
            token=secrets.token_urlsafe(40);self.tokens[self.digest(token)]=now+28800
            return {'token':token,'expires_at':now+28800}

    def valid(self, token):
        with self.lock:return self.tokens.get(self.digest(token),0)>time.time()

    def logout(self, token):
        with self.lock:self.tokens.pop(self.digest(token),None)


def read_json(handler):
    n=int(handler.headers.get('Content-Length','0'))
    if n<0 or n>262144:raise ValueError('body_limit')
    raw=handler.rfile.read(n)
    if handler.headers.get('Content-Encoding')=='gzip':
        d=zlib.decompressobj(16+zlib.MAX_WBITS);raw=d.decompress(raw,1_048_577)
        if len(raw)>1_048_576 or d.unconsumed_tail or not d.eof:raise ValueError('body_limit')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('object_required')
    return value


def handler_for(config, login):
    root=Path(config['state_dir']);origins=set(config['origins'])
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def setup(self):super().setup();self.connection.settimeout(15)
        def reply(self,status,data):
            body=b'' if status==204 else json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
            etag='"'+hashlib.sha256(body).hexdigest()+'"'
            zipped='gzip' in self.headers.get('Accept-Encoding','') and len(body)>1024
            if zipped:body=gzip.compress(body)
            self.send_response(status)
            origin=self.headers.get('Origin')
            if origin in origins:self.send_header('Access-Control-Allow-Origin',origin)
            self.send_header('Vary','Origin, Accept-Encoding')
            self.send_header('Access-Control-Allow-Headers','Authorization, Content-Type, Content-Encoding, X-Probe-Source')
            self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
            self.send_header('Access-Control-Max-Age','600')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('ETag',etag)
            if zipped:self.send_header('Content-Encoding','gzip')
            self.send_header('Content-Length',str(len(body)));self.end_headers()
            if self.command!='HEAD':self.wfile.write(body)

        def do_OPTIONS(self):
            self.reply(204 if self.headers.get('Origin') in origins else 403,{})

        def do_GET(self):self.route()
        def do_POST(self):self.route()

        def route(self):
            hub=None
            try:
                url=urlsplit(self.path);path=url.path.removeprefix('/monitor');q=parse_qs(url.query)
                origin=self.headers.get('Origin')
                if origin and origin not in origins:return self.reply(403,{'error':'origin_denied'})
                token=self.headers.get('Authorization','').removeprefix('Bearer ')
                if path=='/status.json' and self.command=='GET':
                    store=Store(root/'telegram-state/stock.sqlite')
                    try:
                        data=snapshot(store)
                        health=data['collector'];at=health.get('checked_at',0)
                        if at and time.time()-at>360:health={**health,'state':'disconnected'}
                        data['collector']={k:v for k,v in health.items() if k in ('state','checked_at','retry_at','last_event_at')}
                        data['published_at']=datetime.fromtimestamp(at or config['started_at'],CST).isoformat()
                    finally:store.db.close()
                    return self.reply(200,data)
                if path=='/auth/request' and self.command=='POST':
                    if origin not in origins:return self.reply(403,{'error':'origin_required'})
                    read_json(self)
                    return self.reply(202,login.challenge())
                if path=='/auth/verify' and self.command=='POST':
                    if origin not in origins:return self.reply(403,{'error':'origin_required'})
                    body=read_json(self);return self.reply(200,login.verify(str(body.get('challenge',''))[:80],str(body.get('code',''))[:12]))
                if path=='/ingest' and self.command=='POST':
                    source=self.headers.get('X-Probe-Source','')
                    expected=config['probe_tokens'].get(source)
                    if not expected or not hmac.compare_digest(token,expected):return self.reply(401,{'error':'probe_identity_required'})
                    hub=Hub(root/'hub.sqlite');result=hub.ingest(source,read_json(self))
                    return self.reply(200,result)
                if not login.valid(token):return self.reply(401,{'error':'login_required'})
                if path=='/auth/logout' and self.command=='POST':
                    read_json(self);login.logout(token);return self.reply(200,{'logged_out':True})
                if self.command!='GET':return self.reply(405,{'error':'method_not_allowed'})
                hub=Hub(root/'hub.sqlite')
                now=time.time();since=max(now-30*86400,float(q.get('since',[now-86400])[0]))
                if path=='/api/latest':return self.reply(200,hub.latest())
                if path=='/api/history':return self.reply(200,hub.history(max(now-7*86400,since),max(0,int(q.get('cursor',[0])[0]))))
                if path=='/api/incidents':return self.reply(200,{'incidents':hub.incidents(since)})
                if path=='/api/evidence':
                    data=hub.evidence(q.get('id',[''])[0]);return self.reply(200 if data else 404,data or {'error':'not_found'})
                if path=='/api/reports':return self.reply(200,{'reports':[{'day':r[0],'body':r[1],'sent_at':r[2]} for r in hub.db.execute('SELECT day,body,sent FROM reports ORDER BY day DESC LIMIT 30')]})
                return self.reply(404,{'error':'not_found'})
            except (ValueError,TypeError,KeyError,zlib.error):
                self.close_connection=True;self.reply(429 if path=='/auth/request' else 400,{'error':'invalid_or_rate_limited'})
            except (BrokenPipeError,ConnectionResetError,TimeoutError):pass
            except Exception as exc:
                print('API failure:',type(exc).__name__,flush=True)
                self.close_connection=True;self.reply(503,{'error':'temporarily_unavailable'})
            finally:
                if hub:hub.close()
    return Handler


def maintenance(config):
    root=Path(config['state_dir']);now=time.time();local=datetime.fromtimestamp(now,CST)
    hub=Hub(root/'hub.sqlite');outbox=Store(root/'notifications.sqlite')
    try:
        first=datetime.fromtimestamp(config['started_at'],CST).date()
        last=local.date()-timedelta(days=1)
        if local.hour>=9:
            day=first
            while day<=last:
                key=day.isoformat();body=hub.daily(key)
                if not hub.db.execute('SELECT 1 FROM reports WHERE day=?',(key,)).fetchone():
                    if day<last:body+='\n该日报因服务离线延迟生成。'
                    stock=Store(root/'telegram-state/stock.sqlite')
                    pending=stock.db.execute('SELECT COUNT(*) FROM outbox WHERE sent IS NULL AND cancelled IS NULL').fetchone()[0];stock.db.close()
                    body+=f'\n补货邮件待发送/重试：{pending} 条。'
                    outbox.enqueue('daily:'+key,body,channels=('email',))
                    hub.db.execute('INSERT INTO reports VALUES(?,?,NULL)',(key,body));hub.db.commit()
                day+=timedelta(days=1)
        drain(outbox,config['notify'])
        for ident,sent in outbox.db.execute("SELECT id,sent FROM outbox WHERE id LIKE 'daily:%' AND sent IS NOT NULL"):
            hub.db.execute('UPDATE reports SET sent=? WHERE day=?',(sent,ident.removeprefix('daily:')))
        hub.db.commit();hub.trim()
        outbox.trim(max_bytes=10*1024*1024)
        # Each active data set has a physical budget; archives live outside the active root.
        for path in (root/'telegram-state/stock.sqlite',root/'probe.sqlite'):
            if path.exists():
                s=Store(path);s.trim(max_bytes=(20 if 'stock' in path.name else 20)*1024*1024);s.db.close()
        size=sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
        (root/'disk-health.json').write_text(json.dumps({'at':now,'bytes':size,'limit':150*1024*1024,'over_limit':size>150*1024*1024}))
        if size>150*1024*1024:
            # Trim only owned diagnostic logs; never the business proxy logs or secrets.
            for p in sorted(root.glob('*.log'),key=lambda p:p.stat().st_mtime):
                size-=p.stat().st_size;p.unlink()
                if size<=150*1024*1024:break
    finally:hub.close();outbox.db.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);args=p.parse_args()
    config=json.loads(Path(args.config).read_text());login=Login(config)
    server=ThreadingHTTPServer(('127.0.0.1',config.get('port',18790)),handler_for(config,login))
    def maintain():
        while True:
            try:maintenance(config)
            except Exception as exc:print('Maintenance:',type(exc).__name__,flush=True)
            time.sleep(60)
    threading.Thread(target=maintain,daemon=True).start()
    server.serve_forever()


if __name__=='__main__':main()
