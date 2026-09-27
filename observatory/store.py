"""Bounded local SQLite journal. Samples retain original capture times on replay."""
import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path, capture_evidence=True):
        self.capture_evidence = capture_evidence
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY, captured REAL, body TEXT, sent INTEGER DEFAULT 0);
          CREATE INDEX IF NOT EXISTS sample_time ON samples(captured);
          CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, body TEXT);
          CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY, body TEXT, updated REAL);
          CREATE TABLE IF NOT EXISTS outbox(id TEXT, channel TEXT, body TEXT, attempts INTEGER DEFAULT 0,
             next_try REAL DEFAULT 0, sent REAL, PRIMARY KEY(id,channel));
          CREATE TABLE IF NOT EXISTS messages(channel TEXT, id INTEGER, edited REAL, hash TEXT, body TEXT,
             PRIMARY KEY(channel,id));
          CREATE TABLE IF NOT EXISTS incident_evidence(incident TEXT,boot TEXT,seq INTEGER,body TEXT,
             PRIMARY KEY(incident,boot,seq));
        ''')
        if 'cancelled' not in {r[1] for r in self.db.execute('PRAGMA table_info(outbox)')}:
            self.db.execute('ALTER TABLE outbox ADD COLUMN cancelled REAL')
            self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute('SELECT body FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def add_sample(self, sample):
        self.db.execute('INSERT INTO samples(captured,body) VALUES(?,?)',
                        (sample['captured_at'], json.dumps(sample, ensure_ascii=False)))
        for incident in self.incidents() if self.capture_evidence else []:
            if incident['started_at']-600 <= sample['captured_at'] <= incident['end_at']:
                self.db.execute('INSERT OR IGNORE INTO incident_evidence VALUES(?,?,?,?)',
                    (incident['id'],sample['boot_id'],sample['seq'],json.dumps(sample,ensure_ascii=False)))
        self.db.commit()

    def pending(self, limit=12):
        newest=self.db.execute('SELECT id,body FROM samples WHERE sent=0 ORDER BY id DESC LIMIT 1').fetchone()
        if not newest:return []
        rows=[newest]+list(self.db.execute('SELECT id,body FROM samples WHERE sent=0 AND id<>? ORDER BY id LIMIT ?', (newest[0],max(0,limit-1))))
        return [(r[0],json.loads(r[1])) for r in rows]

    def acknowledge(self, ids):
        self.db.executemany('UPDATE samples SET sent=1 WHERE id=?', [(i,) for i in ids])
        self.db.commit()

    def window(self, start, end):
        return [json.loads(r[0]) for r in self.db.execute(
            'SELECT body FROM samples WHERE captured BETWEEN ? AND ? ORDER BY captured', (start, end))]

    def incident(self, incident):
        incident['updated_at'] = time.time()
        # Bound every cloud incident below its 64 KiB envelope. Never hide truncation.
        if len(json.dumps(incident,ensure_ascii=False).encode())>60000:
            evidence=incident.setdefault('evidence',{})
            for field in ('details','post_logs'):
                if field in evidence:
                    evidence[field]={'state':'size_limited','excerpt':str(evidence[field])[:6000]}
            incident.setdefault('report',{}).setdefault('missing',[]).append('诊断附件超过单条上限，已裁剪；前后采样独立保存。')
        self.db.execute('INSERT OR REPLACE INTO incidents VALUES(?,?,?)',
                        (incident['id'], json.dumps(incident, ensure_ascii=False), time.time()))
        for s in self.window(incident['started_at']-600, incident['end_at']) if self.capture_evidence else []:
            self.db.execute('INSERT OR IGNORE INTO incident_evidence VALUES(?,?,?,?)',
                (incident['id'],s['boot_id'],s['seq'],json.dumps(s,ensure_ascii=False)))
        self.db.commit()

    def incidents(self, since=0):
        return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM incidents WHERE updated>?', (since,))]

    def enqueue(self, event_id, body, channels=('telegram', 'email')):
        if event_id.startswith('link-'):
            from datetime import datetime, timezone, timedelta
            body+='\n记录时间：'+datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')+'\n网络恢复后可能延迟送达，请以页面最新状态为准。'
        for channel in channels:
            self.db.execute('INSERT OR IGNORE INTO outbox(id,channel,body) VALUES(?,?,?)', (event_id, channel, body))
        self.db.commit()

    def cancel_legacy_notifications(self):
        self.db.execute("UPDATE outbox SET cancelled=? WHERE sent IS NULL AND cancelled IS NULL AND (id LIKE 'link-%' OR channel='telegram')",(time.time(),))
        self.db.commit()

    def trim(self, now=None, max_bytes=100 * 1024 * 1024):
        now = now or time.time()
        self.db.execute('DELETE FROM samples WHERE captured<?', (now - 7*86400,))
        self.db.execute('DELETE FROM incidents WHERE updated<?', (now - 30*86400,))
        self.db.execute('DELETE FROM outbox WHERE sent IS NOT NULL AND sent<?', (now - 30*86400,))
        self.db.execute('DELETE FROM messages WHERE edited<?', (now - 30*86400,))
        self.db.execute('DELETE FROM incident_evidence WHERE incident NOT IN (SELECT id FROM incidents)')
        # Bound by encoded payload bytes rather than physical SQLite high-water mark.
        size = self.db.execute('SELECT COALESCE(SUM(length(body)),0) FROM samples').fetchone()[0]
        raw_limit=max_bytes*2//3
        if size > raw_limit:
            self.set('retention_warning', {'at': now, 'reason': 'local_buffer_limit'})
            while size > raw_limit:
                rows = list(self.db.execute('SELECT id,length(body) FROM samples ORDER BY id LIMIT 250'))
                if not rows:
                    break
                self.db.executemany('DELETE FROM samples WHERE id=?', [(r[0],) for r in rows])
                size -= sum(r[1] for r in rows)
        self.db.commit()
        evidence_size=self.db.execute('SELECT COALESCE(SUM(length(body)),0) FROM incident_evidence').fetchone()[0]
        while evidence_size>max_bytes//3:
            oldest=self.db.execute('SELECT id FROM incidents ORDER BY updated LIMIT 1').fetchone()
            if not oldest:break
            released=self.db.execute('SELECT COALESCE(SUM(length(body)),0) FROM incident_evidence WHERE incident=?',oldest).fetchone()[0]
            self.db.execute('DELETE FROM incident_evidence WHERE incident=?',oldest)
            self.db.execute('DELETE FROM incidents WHERE id=?',oldest)
            evidence_size-=released
            self.set('retention_warning',{'at':now,'reason':'local_evidence_limit'})
        self.db.commit()
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.db.execute('PRAGMA incremental_vacuum(2000)')
        path = Path(self.db.execute('PRAGMA database_list').fetchone()[2])
        if path.stat().st_size > max_bytes:
            self.db.execute('DELETE FROM messages WHERE rowid IN (SELECT rowid FROM messages ORDER BY edited LIMIT (SELECT MAX(0,COUNT(*)-1000) FROM messages))')
            self.db.execute('DELETE FROM outbox WHERE cancelled IS NOT NULL OR sent IS NOT NULL')
            self.db.commit()
            self.db.execute('VACUUM')
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
