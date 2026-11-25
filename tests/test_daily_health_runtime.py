from datetime import datetime, timedelta, timezone
import pytest
from gway_agent_ops import daily_health_runtime as rt, daily_health_store as store

class Fake:
    def __init__(self):
        self.rows=[]
        self.comments={}
        self.ambiguous=False
    def __call__(self, endpoint,*args):
        if not args:
            if '/comments?' in endpoint:
                return self.comments.get(int(endpoint.split('/')[4]),[])
            state=endpoint.split('state=')[1].split('&')[0]
            return [r for r in self.rows if r['state']==state]
        body=next(a[5:] for a in args if a.startswith('body='))
        if endpoint.endswith('/comments'):
            number=int(endpoint.split('/')[4])
            self.comments.setdefault(number,[]).append({'body':body})
            result={'id':1}
        else:
            result={'number':len(self.rows)+1,'body':body,'state':'open'}
            self.rows.append(result)
        if self.ambiguous:
            self.ambiguous=False
            raise RuntimeError('uncertain')
        return result

F={'component':'disk_used_percent','severity':'warning','value':82}

def test_cross_day(tmp_path):
    path=tmp_path/'private'/'db'
    api=Fake()
    def run(day):
        return rt.reconcile(F,day,api=api,path=path)['action']
    assert run('2026-11-25')=='created'
    assert run('2026-11-25')=='already_reported'
    assert run('2026-11-26')=='updated'
    assert run('2026-11-26')=='already_reported'
    assert len(api.rows)==1 and len(api.comments[1])==1
    api.rows[0]['state']='closed'
    assert run('2026-11-27')=='created'
    assert '#1' in api.rows[1]['body']

def test_uncertain_visible(tmp_path):
    path=tmp_path/'private'/'db'
    api=Fake()
    api.ambiguous=True
    with pytest.raises(RuntimeError):
        rt.reconcile(F,'2026-11-25',api=api,path=path)
    assert rt.reconcile(F,'2026-11-26',api=api,path=path)['action']=='updated'
    assert len(api.rows)==1

def test_uncertain_invisible(tmp_path):
    path=tmp_path/'private'/'db'
    api=Fake()
    api.ambiguous=True
    with pytest.raises(RuntimeError):
        rt.reconcile(F,'2026-11-25',api=api,path=path)
    api.rows.clear()
    with pytest.raises(RuntimeError,match='unconfirmed'):
        rt.reconcile(F,'2026-11-26',api=api,path=path)
    assert not api.rows

def test_recovery(tmp_path):
    path=tmp_path/'private'/'db'
    start=datetime(2026,11,25,10,tzinfo=timezone.utc)
    store.create(path=path,now=start)
    store.transition('resume',path=path,now=start)
    due=start+timedelta(hours=1)
    c=store.claim(path=path,now=due)
    assert c
    assert not rt.recover_claims(path=path,now=due+timedelta(hours=1),probe=lambda _:False)
    assert rt.recover_claims(path=path,now=due+timedelta(hours=1),probe=lambda _:True)==[c['local_date']]
    assert store.claim(path=path,now=due+timedelta(days=1))

@pytest.mark.parametrize('samples,severity', [([90]*6,'critical'),([45]*6,'warning'),([90,90,90,10,90,90],None)])
def test_sustained_cpu(samples,severity,monkeypatch):
    from gway_agent_ops import daily_health_audit as audit
    from types import SimpleNamespace
    values=iter(samples)
    monkeypatch.setattr(audit.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=''))
    report=audit.collect_extended(operational=lambda:({},[]),collector=lambda **kw:{'metrics':{'cpu_busy_percent':next(values)},'errors':[],'findings':[]})
    findings=[f for f in report['findings'] if f['component']=='cpu_sustained_min_percent']
    assert ([f['severity'] for f in findings] or [None])==[severity]


def test_health_notifications(tmp_path, monkeypatch):
    from gway_agent_ops import daily_health_notify as n
    monkeypatch.setattr(n.activity_feed,'enabled',lambda:True)
    monkeypatch.setattr(n.lcd_notification,'enabled',lambda:True)
    calls=[]
    report={'findings':[{'severity':'critical'}],'errors':[]}
    for _ in range(2):
        n.notify('2026-11-25',report,path=tmp_path/'private'/'db',runner=lambda argv,**kw:calls.append(argv))
    assert len(calls)==3
    assert 'agentops.daily-health' in calls[0]
    assert 'CRITICAL_HEALTH' in calls[1][-1]
    assert 'codex_failed' not in calls[1][-1]


def test_semantic_candidate(tmp_path):
    api=Fake()
    api.rows=[{'number':1,'title':'gway-001 disk used capacity','body':'existing','state':'open'}]
    assert rt.reconcile(F,'2026-11-25',api=api,path=tmp_path/'private'/'db')['action']=='updated'
    assert len(api.rows)==1
