"""Incremental relevant client logs; initialization starts at the existing file's end."""
from pathlib import Path
import re
from .probes import safe_text

class LogWatch:
    def __init__(self,root):
        self.root=Path(root)/'logs';self.path=None;self.offset=0;self.initial=True
    def poll(self):
        files=sorted(self.root.glob('core-*.log'),key=lambda p:p.stat().st_mtime,reverse=True)
        if not files:return []
        path=files[0]
        if self.path!=path:
            self.path=path;self.offset=path.stat().st_size if self.initial else 0;self.initial=False
        size=path.stat().st_size
        if size<self.offset:self.offset=0
        with path.open('rb') as stream:
            stream.seek(max(self.offset,size-100000));lines=stream.read().decode('utf-8','replace').splitlines();self.offset=stream.tell()
        return [safe_text(line)[:500] for line in lines if re.search(
            r'no recent network activity|received stateless reset|create.*TUN.*fail|TUN.*initial.*fail|Start TUN.*error',line,re.I)][-20:]
