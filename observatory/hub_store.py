"""Private SQLite ingestion, ordering, aggregation and daily reports on the VPS."""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))


class Hub:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY,source TEXT,boot TEXT,seq INTEGER,
          captured REAL,received REAL,body TEXT,UNIQUE(source,boot,seq));
        CREATE INDEX IF NOT EXISTS time_source ON samples(source,captured);
        CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY,source TEXT,started REAL,updated REAL,body TEXT);
        CREATE TABLE IF NOT EXISTS rollups(source TEXT,minute INTEGER,metric TEXT,good INTEGER,bad INTEGER,
          total_ms REAL,max_ms REAL,PRIMARY KEY(source,minute,metric));
        CREATE TABLE IF NOT EXISTS reports(day TEXT PRIMARY KEY,body TEXT,sent REAL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS correlations(incident TEXT PRIMARY KEY,body TEXT,finalized INTEGER);
        ''')

    def ingest(self, source, data, now=None):
        now = now or time.time()
        samples, incidents = data.get('samples', []), data.get('incidents', [])
        if not isinstance(samples,list) or len(samples)>60 or not isinstance(incidents,list) or len(incidents)>20:
            raise ValueError('batch_limit')
        # Validate the full envelope before any write, including a forged nested source.
        for s in samples:
            if s.get('source') != source or not isinstance(s.get('boot_id'),str) or len(s['boot_id'])>80:
                raise ValueError('source_mismatch')
            if type(s.get('seq')) is not int or s['seq']<0:
                raise ValueError('sequence')
            if not isinstance(s.get('captured_at'),(int,float)) or not now-31*86400 <= s['captured_at'] <= now+120:
                raise ValueError('sample_time')
            if len(json.dumps(s).encode())>48000 or not isinstance(s.get('checks'),dict):
                raise ValueError('sample_limit')
        for i in incidents:
            if i.get('source') != source or not isinstance(i.get('id'),str) or len(i['id'])>80:
                raise ValueError('incident_source')
            if not isinstance(i.get('started_at'),(int,float)) or len(json.dumps(i).encode())>64000:
                raise ValueError('incident_limit')
            old=self.db.execute('SELECT source FROM incidents WHERE id=?',(i['id'],)).fetchone()
            if old and old[0]!=source: raise ValueError('incident_owner')
        with self.db:
            for s in samples:
                cur=self.db.execute('INSERT OR IGNORE INTO samples(source,boot,seq,captured,received,body) VALUES(?,?,?,?,?,?)',
                    (source,s['boot_id'],s['seq'],s['captured_at'],now,json.dumps(s,ensure_ascii=False)))
                if not cur.rowcount: continue
                for metric,v in s['checks'].items():
                    if v.get('new') is False or v.get('kind')!='regular' or v.get('state') not in ('ok','fail'): continue
                    good=int(v['state']=='ok'); ms=v.get('ms') if good else 0
                    ms=ms if isinstance(ms,(int,float)) else 0
                    minute=int(v.get('sampled_at',s['captured_at'])//60)*60
                    self.db.execute('''INSERT INTO rollups VALUES(?,?,?,?,?,?,?) ON CONFLICT(source,minute,metric)
                    DO UPDATE SET good=good+excluded.good,bad=bad+excluded.bad,
                    total_ms=total_ms+excluded.total_ms,max_ms=MAX(max_ms,excluded.max_ms)''',
                        (source,minute,metric[:100],good,1-good,ms,ms))
            for i in incidents:
                old=self.db.execute('SELECT body FROM incidents WHERE id=?',(i['id'],)).fetchone()
                if old and json.loads(old[0]).get('updated_at',0)>i.get('updated_at',0):continue
                self.db.execute('INSERT OR REPLACE INTO incidents VALUES(?,?,?,?,?)',
                    (i['id'],source,i['started_at'],now,json.dumps(i,ensure_ascii=False)))
        return {'server_at':now,'accepted':len(samples),'requests':[],'budget_level':self.budget_control(now)['level']}

    def budget_control(self, now=None):
        now=now or time.time()
        row=self.db.execute("SELECT body FROM settings WHERE key='budget_control'").fetchone()
        state=json.loads(row[0]) if row else {'level':0,'started_at':now,'last_change':now}
        total=0
        for source in ('windows','vmiss'):
            row=self.db.execute('SELECT body FROM samples WHERE source=? ORDER BY captured DESC LIMIT 1',(source,)).fetchone()
            if row:total+=json.loads(row[0]).get('budget',{}).get('monthly_estimate_bytes',0)
        state['monthly_estimate_bytes']=total
        if total>1_000_000_000 and now-state['last_change']>=86400 and state['level']<2:
            state['level']+=1;state['last_change']=now
        with self.db:self.db.execute("INSERT OR REPLACE INTO settings VALUES('budget_control',?)",(json.dumps(state),))
        return state

    def latest(self, now=None):
        now=now or time.time(); result={}
        for source in ('windows','vmiss'):
            row=self.db.execute('SELECT body,received,id FROM samples WHERE source=? ORDER BY captured DESC,seq DESC LIMIT 1',(source,)).fetchone()
            if row:
                s=json.loads(row[0]); age=max(0,now-s['captured_at'])
                result[source]={'sample':s,'received_at':row[1],'cursor':row[2], 'age_seconds':round(age),
                    'state':'fresh' if age <= max(360,s.get('interval',120)*3) else 'stale'}
            else:result[source]={'state':'no_data'}
        row=self.db.execute("SELECT body FROM settings WHERE key='budget_control'").fetchone()
        budget=json.loads(row[0]) if row else {}
        return {'server_at':now,'sources':result,'statistics':self.statistics(now-86400,now),'monitoring_budget':budget,
                'notice':'独立远端观测点已停用；缺测不代表丢包。'}

    def history(self, since, cursor=0, limit=1000):
        rows=list(self.db.execute('SELECT id,body,received FROM samples WHERE captured>=? AND id>? ORDER BY id LIMIT ?',
                                  (since,cursor,limit)))
        # Charts need checks and gaps, not every network inventory or log excerpt.
        return {'samples':[{'cursor':r[0],**{k:v for k,v in json.loads(r[1]).items() if k in
                   ('source','boot_id','seq','captured_at','checks','gap','interval')},'received_at':r[2]} for r in rows],
                'cursor':rows[-1][0] if rows else cursor,'has_more':len(rows)==limit}

    def incidents(self, since=0):
        return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM incidents WHERE started>=? ORDER BY started DESC LIMIT 300',(since,))]

    def evidence(self, ident):
        row=self.db.execute('SELECT body FROM incidents WHERE id=?',(ident,)).fetchone()
        if not row:return None
        i=json.loads(row[0]); rows=self.db.execute('SELECT body FROM samples WHERE source=? AND captured BETWEEN ? AND ? ORDER BY captured',
                                                (i['source'],i['started_at']-600,i['end_at']))
        samples=[json.loads(r[0]) for r in rows]
        correlation=self.db.execute('SELECT body FROM correlations WHERE incident=?',(ident,)).fetchone()
        related=[]
        if i['source']=='windows' and i['target'].startswith('vmiss.'):
            related=[json.loads(r[0]) for r in self.db.execute('SELECT body FROM samples WHERE source=\'vmiss\' AND captured BETWEEN ? AND ? ORDER BY captured',
                                                            (i['started_at']-600,i['end_at']))]
        return {'incident':i,'samples':samples,'related_server_samples':related,
                'server_log_excerpt':json.loads(correlation[0]) if correlation else {'state':'not_available'},
                'notice':'原始采样仅保留 7 天，窗口可能因缺测或裁剪而不完整。'}

    def correlate(self, reader, now=None):
        now=now or time.time()
        for i in self.incidents(now-1800):
            if i['source']!='windows' or not i['target'].startswith('vmiss.'):continue
            old=self.db.execute('SELECT body,finalized FROM correlations WHERE incident=?',(i['id'],)).fetchone()
            if old and (old[1] or now<i['end_at']):continue
            try:
                logs=reader(i)[-10:]
                value={'state':'complete' if now>=i['end_at'] else 'initial','source':('vmiss-vless.service' if i['target']=='vmiss.vless' else 'vmiss-hy2.service')+' journal',
                       'window_start':i['started_at']-600,'window_end':i['end_at'],
                       'excerpt':[str(line)[:400] for line in logs]}
            except Exception as exc:value={'state':'unavailable','reason':type(exc).__name__}
            with self.db:self.db.execute('INSERT OR REPLACE INTO correlations VALUES(?,?,?)',
                (i['id'],json.dumps(value,ensure_ascii=False),int(now>=i['end_at'])))

    def statistics(self, start, end):
        stats={}
        for source,metric,good,bad,total,max_ms in self.db.execute('''SELECT source,metric,SUM(good),SUM(bad),SUM(total_ms),MAX(max_ms)
                    FROM rollups WHERE minute>=? AND minute<? GROUP BY source,metric''',(start,end)):
            if metric.startswith('home.'):continue
            stats.setdefault(source,{})[metric]={'good':good,'bad':bad,'success_percent':round(100*good/(good+bad),2),
                                               'mean_ms':round(total/good,2) if good else None,'max_ms':max_ms}
        incidents=self.incidents(start-30*86400)
        counts={source:sum(i['source']==source and not i.get('target','').startswith('home.') and bool(i.get('confirmed_at')) and start<=i['started_at']<end for i in incidents)
                for source in ('windows','vmiss')}
        return {'regular_checks':stats,'confirmed_incidents':counts,'start':start,'end':end}

    def daily(self, day):
        start=datetime.fromisoformat(day).replace(tzinfo=CST).timestamp(); end=start+86400
        stats=self.statistics(start,end)
        lines=[f'[链路日报] {day}（北京时间）','常规采样间隔按样本记录；故障复核另计，缺测不计丢包。HY2 与 VLESS 分项统计。']
        for source in ('windows','vmiss'):
            count=self.db.execute('SELECT COUNT(*) FROM samples WHERE source=? AND captured>=? AND captured<? AND json_extract(body,\'$.kind\')=\'regular\'',(source,start,end)).fetchone()[0]
            # Sum expected coverage per actual schedule; unknown time remains uncovered.
            covered=self.db.execute('SELECT COALESCE(SUM(MIN(COALESCE(json_extract(body,\'$.interval\'),120),300)),0) FROM samples WHERE source=? AND captured>=? AND captured<? AND json_extract(body,\'$.kind\')=\'regular\'',(source,start,end)).fetchone()[0]
            lines.append(f"\n{source}: {count} 轮；采样覆盖估计 {min(100,covered/864):.1f}%；确认异常 {stats['confirmed_incidents'][source]} 次。")
            for metric,s in stats['regular_checks'].get(source,{}).items():
                lines.append(f"{metric}: 成功 {s['good']}，失败 {s['bad']}，平均 {s['mean_ms']} ms，最大 {s['max_ms']} ms")
            row=self.db.execute('SELECT body FROM samples WHERE source=? AND captured<? ORDER BY captured DESC LIMIT 1',(source,end)).fetchone()
            if row:
                s=json.loads(row[0]);lines.append('资源/监控预算/上传：'+json.dumps({k:s.get(k) for k in ('host','budget','upload')},ensure_ascii=False)[:2000])
        for i in self.incidents(start-30*86400):
            if i.get('target','').startswith('home.'):continue
            until=i.get('recovered_at') or i.get('monitoring_ended_at') or end
            if i.get('confirmed_at') and i['started_at']<end and until>start:
                duration=max(0,min(end,until)-max(start,i['started_at']))
                lines.append(f"异常 {i['source']}/{i['target']}: 本日观测区间估计 {duration:.0f} 秒；"+('已恢复' if i.get('recovered_at') else '已停止此项探测，未确认恢复' if i.get('monitoring_ended_at') else '未确认恢复'))
        lines += ['\n已确认事实：以上统计来自实际样本及日志。','推断：失败所属层面见故障记录，不能单凭超时判断运营商故障。',
                  '缺失证据：无第二台独立外部观测；电脑休眠/离线期间未知；未进行持续抓包。']
        return '\n'.join(lines)

    def trim(self, now=None, max_bytes=100*1024*1024):
        now=now or time.time()
        with self.db:
            self.db.execute('DELETE FROM samples WHERE captured<?',(now-7*86400,))
            self.db.execute('DELETE FROM rollups WHERE minute<?',(now-30*86400,))
            self.db.execute('DELETE FROM incidents WHERE started<?',(now-30*86400,))
            self.db.execute('DELETE FROM reports WHERE day<?',((datetime.fromtimestamp(now,CST)-timedelta(days=30)).date().isoformat(),))
            self.db.execute('DELETE FROM correlations WHERE incident NOT IN (SELECT id FROM incidents)')
            while self.db.execute('SELECT COALESCE(SUM(length(body)),0) FROM incidents').fetchone()[0]>10*1024*1024:
                self.db.execute('DELETE FROM incidents WHERE id IN (SELECT id FROM incidents ORDER BY started LIMIT 20)')
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        # Physical limit includes freelist/WAL; VACUUM returns removed pages to disk.
        if self.path.stat().st_size>max_bytes:
            while self.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0] and self.db.execute('SELECT COALESCE(SUM(length(body)),0) FROM samples').fetchone()[0]>max_bytes//2:
                self.db.execute('DELETE FROM samples WHERE id IN (SELECT id FROM samples ORDER BY captured LIMIT 500)');self.db.commit()
            self.db.execute('VACUUM');self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def close(self):self.db.close()
