"""Loopback-only HY2 request bridge for Komari's native HTTP latency tasks."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from .probes import https

def start(config):
    targets={t['id']:t for t in config['targets'] if t.get('proxy_port')}
    gate=threading.BoundedSemaphore(2)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            target=targets.get(self.path.removeprefix('/hy2/')) if self.path.startswith('/hy2/') else None
            if not target:self.send_error(404);return
            if not gate.acquire(blocking=False):self.send_error(429);return
            try:
                value=https(config['test_url'],'http://127.0.0.1:'+str(target['proxy_port']))
                self.send_response(204 if value['state']=='ok' else 503);self.send_header('Cache-Control','no-store');self.end_headers()
            finally:gate.release()
    server=ThreadingHTTPServer(('127.0.0.1',18780),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server
