"""Low-frequency incident state machine; retries never inflate regular statistics."""
import time
import uuid
from .incidents import classify


class LowIncidents:
    def __init__(self, store, source):
        self.store, self.source = store, source
        self.states = store.get('low_incident_state', {})

    def observe(self, metric, result, now, kind='regular'):
        """Return a next verification deadline, or None. Only real results count."""
        s = self.states.setdefault(metric, {'fails': 0, 'successes': 0, 'last_fast': -1e12,
                                           'active': None, 'retry_left': 0})
        deadline = None
        state = result.get('state')
        if state not in ('ok', 'fail'):
            # Unknown coverage breaks consecutive evidence, but does not imply recovery.
            s.update(fails=0, successes=0, retry_left=0)
            self.save(); return None
        if state == 'fail':
            s['successes'] = 0
            s['fails'] += 1
            if not s['active']:
                i = {'id': str(uuid.uuid4()), 'source': self.source, 'target': metric,
                     'started_at': now, 'end_at': now + 300, 'status': 'collecting',
                     'confirmed_at': None, 'recovered_at': None,
                     'evidence': {'pre_seconds': 600, 'post_seconds': 300, 'mode': 'low_frequency'},
                     'report': {'facts': ['首次实际探测失败。'], 'inferences': ['待定位。'], 'missing': []}}
                history = self.store.window(now-600, now)
                i['evidence']['available_pre_seconds'] = min(600, now-history[0]['captured_at']) if history else 0
                self.store.incident(i); s['active'] = i['id']
            if s['fails'] >= 3:
                i = self.active(s)
                if i and not i.get('confirmed_at'):
                    i['confirmed_at'] = now; self.store.incident(i)
                s['retry_left'] = 0
            elif now-s['last_fast'] >= 600 and (kind != 'retry' or s['fails']==1):
                s.update(last_fast=now, retry_left=2)
            elif kind == 'retry':
                s['retry_left'] = max(0, s['retry_left']-1)
            if s['retry_left'] and s['fails'] < 3:
                deadline = now+5
        else:
            s['fails'] = 0; s['retry_left'] = 0
            s['successes'] += 1
            if s['active']:
                if s['successes'] == 1:
                    deadline = now+5
                else:
                    i = self.active(s)
                    if i:
                        i['recovered_at'] = now
                        i['duration_is_estimate'] = True
                        self.store.incident(i)
                    s['active'] = None
        self.save()
        return deadline

    def active(self, s):
        return next((i for i in self.store.incidents() if i['id'] == s['active']), None)

    def gap(self):
        for s in self.states.values():
            s.update(fails=0, successes=0, retry_left=0)
        self.save()

    def finish_evidence(self, now):
        for i in self.store.incidents():
            if i.get('status') == 'collecting' and now >= i['end_at']:
                i['status'] = 'complete'
                i['report'] = classify(self.store.window(i['started_at']-600, i['end_at']))
                i['report'].pop('events',None)
                i['report']['missing'].append('120 秒常规采样；持续时间为估计，缺测不算丢包。无持续包捕获。')
                # A small transition digest survives raw retention, without copying inventories.
                rows=self.store.window(i['started_at']-600,i['end_at']); timeline=[];previous={}
                for row in rows:
                    for metric,v in row.get('checks',{}).items():
                        if v.get('new') is False:continue
                        if previous.get(metric)!=v.get('state'):
                            timeline.append({'at':v.get('sampled_at',row['captured_at']),'metric':metric,
                                             'state':v.get('state'),'reason':v.get('reason'),'ms':v.get('ms')})
                            previous[metric]=v.get('state')
                i['evidence']['transitions']=timeline[-40:]
                self.store.incident(i)

    def save(self):
        self.store.set('low_incident_state', self.states)


def budget_level(monthly_estimate, current=0):
    """Ratchet down rather than oscillating. A restart retains the current level."""
    return min(2, current+1) if monthly_estimate > 1_000_000_000 else current
