import json
import os
import pytest
from gway_agent_ops import mcp_health as h, mcp_recovery as r


def state(read='ok',execute='ok',stamp=1000,anonymous='denied',routes='blocked'):
    return dict(vantage='node_synthetic',cloud_client='unverified',read=read,execute=execute,sample_at=stamp,anonymous=anonymous,execute_sample_at=stamp,route_sample_at=stamp,alternate_routes=routes,failure_stage='ssh_transport' if execute=='exec_infra_failure' else None)


def test_projection_ownership_and_freshness(tmp_path):
    p=tmp_path/'status.json'; doc=dict(schema=1,sample_at=1000,anonymous='denied',read='ok',optional_failures=0,optional_checked=2)
    p.write_text(json.dumps(doc))
    assert h.read_observer(p,owner=os.geteuid(),now=1100)==doc
    with pytest.raises(ValueError): h.read_observer(p,owner=os.geteuid(),now=2500)
    p.chmod(0o666)
    with pytest.raises(ValueError): h.read_observer(p,owner=os.geteuid(),now=1100)


@pytest.mark.parametrize('report',[state(),state(execute='authorization'),state(read='ok',execute='execution_failed'),state(read='http_failure',execute='credential_unavailable'),state(read='http_failure',execute='execution_failed',anonymous='unexpected_access')])
def test_nonblocking_or_unverified_never_repairs(tmp_path,report):
    result=r.assess('2026-11-25',path=tmp_path/'private'/'db',probe=lambda:report,runner=lambda *a:pytest.fail('unexpected repair'),sleep=lambda _:None)
    assert result['decision']=='report_only'


def test_two_distinct_failures_and_independent_diagnosis(tmp_path):
    path=tmp_path/'private'/'db'
    samples=[state('http_failure','exec_infra_failure',1000),state('http_failure','exec_infra_failure',1200),state()]
    calls=[]
    def runner(*args):
        calls.append(args)
        return {'fault':'local_remote_transport','local_fault_confirmed':True,'independent_evidence':True} if args[0]=='diagnose' else {'outcome':'applied'}
    result=r.assess('2026-11-25',path=path,probe=lambda:samples.pop(0),runner=runner,sleep=lambda _:None,admission=lambda:True)
    assert result['decision']=='restored'
    assert calls==[('diagnose',),('repair','local_remote_transport')]


def test_no_replay_after_uncertain_repair(tmp_path):
    path=tmp_path/'private'/'db'; calls=[]
    def run(day):
        samples=iter([state('http_failure','exec_infra_failure',1000),state('http_failure','exec_infra_failure',1200),state('http_failure','exec_infra_failure',1300)])
        def runner(*args):
            calls.append(args[0])
            if args[0]=='repair': raise TimeoutError()
            return {'fault':'local_remote_transport','local_fault_confirmed':True,'independent_evidence':True}
        return r.assess(day,path=path,probe=lambda:next(samples),runner=runner,sleep=lambda _:None,admission=lambda:True)
    assert run('2026-11-25')['decision']=='repair_unconfirmed'
    assert run('2026-11-26')['decision']=='episode_already_attempted'
    assert calls.count('repair')==1


@pytest.mark.parametrize('second',[state(),state('http_failure','exec_infra_failure',1000),state('authorization','exec_infra_failure',1200)])
def test_transient_or_stale_sample_cannot_repair(tmp_path,second):
    samples=iter([state('http_failure','exec_infra_failure',1000),second])
    result=r.assess('2026-11-25',path=tmp_path/'private'/'db',probe=lambda:next(samples),runner=lambda *a:pytest.fail('unexpected runner'),sleep=lambda _:None)
    assert result['decision'] in ('report_only','transient_recovered')


def test_recovery_requires_independent_local_fault(tmp_path):
    samples=iter([state('http_failure','exec_infra_failure',1000),state('http_failure','exec_infra_failure',1200)])
    calls=[]
    result=r.assess('2026-11-25',path=tmp_path/'private'/'db',probe=lambda:next(samples),runner=lambda *a:(calls.append(a) or {'fault':'local_remote_transport','local_fault_confirmed':False,'independent_evidence':True}),sleep=lambda _:None)
    assert result['decision']=='report_only' and calls==[('diagnose',)]


@pytest.mark.parametrize('routes',['available','unverified'])
def test_alternate_routes_block_repair(tmp_path,routes):
    report=state('http_failure','exec_infra_failure',routes=routes)
    result=r.assess('2026-11-25',path=tmp_path/'private'/'db',probe=lambda:report,runner=lambda *a:pytest.fail('unexpected repair'),sleep=lambda _:None)
    assert result['decision']=='report_only'


def test_read_healthy_but_control_path_blocked(tmp_path):
    samples=iter([state('ok','exec_infra_failure',1000),state('ok','exec_infra_failure',2000),state()])
    def runner(*args):
        return {'fault':'local_remote_transport','local_fault_confirmed':True,'independent_evidence':True} if args[0]=='diagnose' else {'outcome':'applied'}
    assert r.assess('2026-11-25',path=tmp_path/'private'/'db',probe=lambda:next(samples),runner=runner,sleep=lambda _:None,admission=lambda:True)['decision']=='restored'


def test_sanitized_execution_projection(tmp_path):
    p=tmp_path/'status.json';doc=dict(schema=1,sample_at=1000,execute='exec_infra_failure',failure_stage='ssh_transport',alternate_routes='unverified');p.write_text(json.dumps(doc))
    assert h.read_execution(p,owner=os.geteuid(),now=1100)==doc
    doc['alternate_routes']='blocked';p.write_text(json.dumps(doc))
    with pytest.raises(ValueError):h.read_execution(p,owner=os.geteuid(),now=1100)


def test_independent_signed_routes(tmp_path,monkeypatch):
    import base64,subprocess
    key=tmp_path/'private.pem';pub=tmp_path/'public.pem';payload=tmp_path/'payload';sig=tmp_path/'sig';projection=tmp_path/'status.json'
    subprocess.run(['/usr/bin/openssl','genpkey','-algorithm','ED25519','-out',str(key)],check=True,capture_output=True)
    subprocess.run(['/usr/bin/openssl','pkey','-in',str(key),'-pubout','-out',str(pub)],check=True,capture_output=True)
    doc={'schema':1,'sample_at':1000,'source':'independent_cloud_monitor','complete':True,'routes':dict(watchtower_deployment='blocked',github_workflow='blocked',alternate_node_control='blocked')}
    payload.write_bytes(json.dumps(doc,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode())
    subprocess.run(['/usr/bin/openssl','pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(payload),'-out',str(sig)],check=True,capture_output=True)
    doc['signature']=base64.b64encode(sig.read_bytes()).decode();projection.write_text(json.dumps(doc))
    monkeypatch.setattr(r,'trusted_file',lambda path,limit:pub.read_bytes())
    assert h.read_routes(projection,owner=os.geteuid(),now=1100)['alternate_routes']=='blocked'
    with pytest.raises(ValueError):h.read_routes(projection,owner=os.geteuid(),now=1400)
    doc['routes']['github_workflow']='available';projection.write_text(json.dumps(doc))
    with pytest.raises(ValueError):h.read_routes(projection,owner=os.geteuid(),now=1100)
