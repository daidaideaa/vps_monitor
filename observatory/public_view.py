"""Public metrics are built from allowlists, never by redacting raw diagnostics."""
import math
import time

SOURCES = ('windows', 'vmiss')
METRICS = ('gateway.icmp', 'china.icmp', 'home.icmp', 'home.tcp', 'home.hy2',
           'vmiss.icmp', 'vmiss.tcp', 'vmiss.hy2', 'client.http', 'egress.https',
           'egress.dns', 'vmiss-hy2.service')
STATES = ('ok', 'fail', 'unknown', 'error')


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def check(value):
    return {'state': value.get('state') if value.get('state') in STATES else 'unknown',
            'ms': number(value.get('ms'))}


def latest(hub):
    raw = hub.latest(); sources = {}; stats = {'regular_checks': {}, 'confirmed_incidents': {}}
    for source in SOURCES:
        entry = raw['sources'][source]; sample = entry.get('sample')
        if not sample:
            sources[source] = {'state': 'no_data'}; continue
        host = sample.get('host') or {}; memory = host.get('memory') or {}
        sources[source] = {'state': entry['state'], 'age_seconds': entry['age_seconds'], 'sample': {
            'captured_at': number(sample.get('captured_at')), 'interval': number(sample.get('interval')),
            'checks': {k: check(v) for k, v in sample.get('checks', {}).items() if k in METRICS},
            'host': {'cpu_percent': number(host.get('cpu_percent')),
                     'memory': {'available': number(memory.get('MemAvailable', memory.get('available')))}},
            'traffic': {k: number(sample.get('traffic', {}).get(k)) for k in ('rx_bytes', 'tx_bytes')},
            'budget': {k: number(sample.get('budget', {}).get(k)) for k in
                       ('monthly_estimate_bytes', 'measured_upload_payload_bytes')},
            'upload': {'state': 'ok' if sample.get('upload', {}).get('state') == 'ok' else 'pending'}}}
        stats['regular_checks'][source] = {k: {f: number(v.get(f)) for f in ('good', 'bad', 'mean_ms')}
            for k, v in raw['statistics']['regular_checks'].get(source, {}).items() if k in METRICS}
        stats['confirmed_incidents'][source] = number(raw['statistics']['confirmed_incidents'].get(source, 0))
    return {'server_at': raw['server_at'], 'sources': sources, 'statistics': stats}


def history(hub):
    # Fixed 24-hour range and five-minute buckets: no arbitrary private query surface.
    rows = hub.db.execute('''SELECT CAST(minute/300 AS INTEGER)*300,metric,SUM(good),SUM(bad),SUM(total_ms)
        FROM rollups WHERE source='windows' AND minute>=? GROUP BY 1,2 ORDER BY 1''', (time.time()-86400,))
    samples = {}
    for at, metric, good, bad, total in rows:
        if metric not in ('home.icmp','home.tcp','home.hy2','vmiss.icmp','vmiss.tcp','vmiss.hy2'): continue
        s = samples.setdefault(at, {'source': 'windows', 'captured_at': at, 'interval': 300, 'checks': {}})
        # A bucket containing any failure is a gap rather than a misleading continuous line.
        s['checks'][metric] = {'state': 'fail' if bad else 'ok', 'ms': round(total/good,2) if good and not bad else None}
    return {'samples': list(samples.values()), 'bucket_seconds': 300, 'server_at': time.time()}


def incidents(hub):
    result = []
    for i in hub.incidents(time.time()-30*86400)[:100]:
        if i.get('source') not in SOURCES or i.get('target') not in (*METRICS, 'manual.client'): continue
        result.append({'source': i['source'], 'target': i['target'], 'manual': i.get('manual') is True,
            **{k: number(i.get(k)) for k in ('started_at','confirmed_at','recovered_at')},
            'report': {'facts': ['手动标记故障时间。' if i.get('manual') else
                       '连续三次探测失败。' if i.get('confirmed_at') else '单次探测异常。'],
                       'inferences': ['原因待定位，详细诊断保存在本机与服务器。'],
                       'missing': ['低频采样下的起止时间为估计；缺测不计为丢包。']}})
    return {'incidents': result, 'server_at': time.time()}
