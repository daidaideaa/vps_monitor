"""Public metrics are built from allowlists, never by redacting raw diagnostics."""
import math
import time

SOURCES = ('windows', 'vmiss')
METRICS = ('gateway.icmp', 'china.icmp',
           'vmiss.icmp', 'vmiss.tcp', 'vmiss.hy2', 'vmiss.vless', 'client.http', 'egress.https',
           'egress.dns', 'vmiss-hy2.service', 'vmiss-vless.service')
STATES = ('ok', 'fail', 'unknown', 'error')
TARGETS = ('vmiss.hy2', 'vmiss-hy2.service', 'vmiss.vless', 'vmiss-vless.service')


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def check(value):
    return {'state': value.get('state') if value.get('state') in STATES else 'unknown',
            'ms': number(value.get('ms'))}


def latest(hub):
    raw = hub.latest(); sources = {}; stats = {'regular_checks': {}, 'confirmed_incidents': {}}
    since = raw['server_at'] - 86400
    current = (raw['sources']['windows'].get('sample') or {}).get('checks', {})
    protocol = 'vless' if 'vmiss.vless' in current else 'hy2'
    if protocol == 'vless':
        first = hub.db.execute("SELECT MIN(minute) FROM rollups WHERE source='windows' AND metric='vmiss.vless' AND minute>=?", (since,)).fetchone()[0]
        if first is not None: since = max(since, first)
    active_stats = hub.statistics(since, raw['server_at'])
    events = [i for i in hub.incidents(since) if i.get('target') in TARGETS]
    for source in SOURCES:
        entry = raw['sources'][source]; sample = entry.get('sample')
        if not sample:
            sources[source] = {'state': 'no_data'}; continue
        host = sample.get('host') or {}; memory = host.get('memory') or {}
        sources[source] = {'state': entry['state'], 'age_seconds': entry['age_seconds'], 'sample': {
            'captured_at': number(sample.get('captured_at')), 'interval': number(sample.get('interval')),
            'client_node': next((n for n in ('VMISS-JP-VLESS','VMISS-JP-HY2') if (sample.get('client',{}).get('groups',{}).get('JP-Home')) == n), 'other_or_unknown'),
            'checks': {k: check(v) for k, v in sample.get('checks', {}).items() if k in METRICS},
            'host': {'cpu_percent': number(host.get('cpu_percent')),
                     'memory': {'available': number(memory.get('MemAvailable', memory.get('available')))}},
            'traffic': {k: number(sample.get('traffic', {}).get(k)) for k in ('rx_bytes', 'tx_bytes')},
            'budget': {k: number(sample.get('budget', {}).get(k)) for k in
                       ('monthly_estimate_bytes', 'measured_upload_payload_bytes', 'observation_seconds')},
            'upload': {'state': 'ok' if sample.get('upload', {}).get('state') == 'ok' else 'pending'}}}
        stats['regular_checks'][source] = {k: {f: number(v.get(f)) for f in ('good', 'bad', 'mean_ms')}
            for k, v in active_stats['regular_checks'].get(source, {}).items() if k in METRICS}
        stats['confirmed_incidents'][source] = sum(i.get('source') == source and bool(i.get('confirmed_at')) for i in events)
        row = hub.db.execute('''SELECT MIN(minute),MAX(minute),SUM(good+bad) FROM rollups
            WHERE source=? AND metric=? AND minute>=?''',
            (source, 'vmiss.'+protocol if source == 'windows' else 'vmiss-'+protocol+'.service', since)).fetchone()
        sources[source]['coverage'] = {'first_at': number(row[0]), 'last_at': number(row[1]), 'regular_samples': number(row[2]) or 0}
    return {'server_at': raw['server_at'], 'sources': sources, 'statistics': stats,
            'primary_protocol': protocol, 'window_start': since}


def history(hub):
    # Fixed 24-hour range and five-minute buckets: no arbitrary private query surface.
    rows = hub.db.execute('''SELECT CAST(minute/300 AS INTEGER)*300,metric,SUM(good),SUM(bad),SUM(total_ms)
        FROM rollups WHERE source='windows' AND minute>=? GROUP BY 1,2 ORDER BY 1''', (time.time()-86400,))
    samples = {}
    for at, metric, good, bad, total in rows:
        if metric not in ('vmiss.icmp','vmiss.tcp','vmiss.hy2','vmiss.vless'): continue
        s = samples.setdefault(at, {'source': 'windows', 'captured_at': at, 'interval': 300, 'checks': {}})
        # A bucket containing any failure is a gap rather than a misleading continuous line.
        s['checks'][metric] = {'state': 'fail' if bad else 'ok', 'ms': round(total/good,2) if good and not bad else None,
                              'good': good, 'bad': bad}
    return {'samples': list(samples.values()), 'bucket_seconds': 300, 'server_at': time.time()}


def incidents(hub):
    result = []
    for i in hub.incidents(time.time()-30*86400)[:100]:
        if i.get('source') not in SOURCES or i.get('target') not in TARGETS: continue
        result.append({'source': i['source'], 'target': i['target'], 'manual': i.get('manual') is True,
            **{k: number(i.get(k)) for k in ('started_at','confirmed_at','recovered_at','monitoring_ended_at')},
            'report': {'facts': ['手动标记故障时间。' if i.get('manual') else
                       '连续三次探测失败。' if i.get('confirmed_at') else '单次探测异常。'],
                       'inferences': ['原因待定位，详细诊断保存在本机与服务器。'],
                       'missing': ['低频采样下的起止时间为估计；缺测不计为丢包。']}})
    return {'incidents': result, 'server_at': time.time()}
