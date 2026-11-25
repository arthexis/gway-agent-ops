import json
import os
import pytest
from gway_agent_ops import mcp_health as h, mcp_recovery as r


def state(read='ok',execute='ok',stamp=1000,anonymous='denied'):
    return dict(vantage='node_synthetic',cloud_client='unverified',read=read,execute=execute,sample_at=stamp,anonymous=anonymous)


def receipt(x):
    return {'content':[{'text':json.dumps(x)}]}


def test_exec_nonce_and_receipt():
    calls=[]
    def transport(token,method,params):
        calls.append(params)
        args=params['arguments']; rid=args['request_id']
        if params['name']=='node_exec':
            assert args['command']=='printf %s '+rid and len(rid)==32
            return receipt(dict(request_id=rid,node='gway-001',status='dispatched'))
        return receipt(dict(request_id=rid,node='gway-001',schema=1,status='succeeded',exit_code=0,stdout=rid,stderr='',truncated=False,cursor=None))
    assert h.probe_exec('private',transport,sleep=lambda _:None)
    assert [x['name'] for x in calls]==['node_exec','node_result']


def test_bad_execution_receipt():
    with pytest.raises(h.Failure,match='execution_identity'):
        h.probe_exec('private',lambda *a:receipt({'node':'wrong'}))


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


@pytest.mark.parametrize('stage,expected',[('ssh_transport','exec_infra_failure'),('auth_scope','execution_unverified'),('current_eligibility','execution_unverified')])
def test_typed_exec_failure(stage,expected):
    def transport(token,method,params):
        if params['name']=='node_exec': return {'isError':True}
        return receipt({'request_id':params['arguments']['request_id'],'stage':stage,'status':'failure'})
    with pytest.raises(h.Failure,match=expected): h.probe_exec('private',transport)


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
