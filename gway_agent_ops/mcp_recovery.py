"""Fail-closed recovery admission; an independently reviewed runner owns effects."""
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path
from .scheduler import ledger_path
from .task_ledger import TaskLedger
from .mcp_health import observe

POLICY=Path('/etc/gway-remote/mcp-health-recovery.json')
RUNNER=Path('/usr/local/libexec/gway-mcp-health-recovery')
READ_BLOCKING=frozenset(('http_failure','transport_or_protocol'))
EXEC_BLOCKING=frozenset(('http_failure','transport_or_protocol','exec_infra_failure'))
FAULTS=frozenset(('local_remote_transport',))


def trusted_file(path, limit):
    for parent in path.parents:
        info=parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise ValueError('untrusted_recovery_path')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(fd,'rb') as f:
        info=os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or info.st_nlink!=1:
            raise ValueError('untrusted_recovery_file')
        data=f.read(limit+1)
        if len(data)>limit:
            raise ValueError('recovery_file_too_large')
        return data


def invoke(action, fault=None):
    policy=json.loads(trusted_file(POLICY,4096))
    if set(policy)!={'schema','enabled','runner_sha256','revision'} or policy['schema']!=1 or policy['enabled'] is not True or not re.fullmatch('[0-9a-f]{40}',policy['revision']):
        raise ValueError('recovery_not_authorized')
    if hashlib.sha256(trusted_file(RUNNER,1024*1024)).hexdigest()!=policy['runner_sha256']:
        raise ValueError('recovery_runner_mismatch')
    if action not in ('diagnose','repair') or (action=='repair' and fault not in FAULTS):
        raise ValueError('invalid_recovery_action')
    argv=[str(RUNNER),action,'--revision',policy['revision']]
    if fault:
        argv+=['--fault',fault]
    # Runner is required to supervise its own children and rollback within this budget.
    from .runtime import _same_user_runtime_dir
    bus=_same_user_runtime_dir()
    if bus is None:
        raise ValueError('recovery_user_bus_unavailable')
    limit=270 if action=='repair' else 20
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(Path.home()),'XDG_RUNTIME_DIR':bus,'DBUS_SESSION_BUS_ADDRESS':'unix:path='+bus+'/bus'}
    r=subprocess.run(['/usr/bin/systemd-run','--user','--wait','--pipe','--quiet','--collect','--property=RuntimeMaxSec='+str(limit),'--property=TimeoutStopSec=10','--property=KillMode=control-group','--property=NoNewPrivileges=yes',*argv],capture_output=True,text=True,timeout=limit+20,check=True,env=env,stdin=subprocess.DEVNULL)
    if len(r.stdout)>4096:
        raise ValueError('invalid_recovery_receipt')
    data=json.loads(r.stdout)
    if not isinstance(data,dict):
        raise ValueError('invalid_recovery_receipt')
    return data


def blocking(a,b):
    return all(r.get('anonymous')!='unexpected_access' and r.get('read') in READ_BLOCKING and r.get('execute') in EXEC_BLOCKING for r in (a,b)) and (a['read'],a['execute'])==(b['read'],b['execute'])


def healthy(report):
    return report.get('anonymous')=='denied' and report.get('read')==report.get('execute')=='ok'


def claim(path,day,reset=False,fault=None,evidence=None):
    with TaskLedger(ledger_path(path)) as ledger:
        db=ledger.db
        db.execute('CREATE TABLE IF NOT EXISTS mcp_recovery_episode (id INTEGER PRIMARY KEY CHECK(id=1), day TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS mcp_recovery_attempts (day TEXT PRIMARY KEY, fault TEXT NOT NULL, state TEXT NOT NULL, evidence TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        try:
            if reset:
                db.execute('DELETE FROM mcp_recovery_episode WHERE id=1')
                result=False
            elif db.execute('SELECT 1 FROM mcp_recovery_episode').fetchone() or db.execute('SELECT 1 FROM mcp_recovery_attempts WHERE day=?',(day,)).fetchone():
                result=False
            else:
                db.execute('INSERT INTO mcp_recovery_episode VALUES (1,?)',(day,))
                db.execute('INSERT INTO mcp_recovery_attempts VALUES (?,?,?,?)',(day,fault,'claimed',json.dumps(evidence,sort_keys=True)))
                result=True
            db.execute('COMMIT')
            return result
        except BaseException:
            db.execute('ROLLBACK')
            raise


def finish(path,day,result):
    with TaskLedger(ledger_path(path)) as ledger:
        ledger.db.execute('UPDATE mcp_recovery_attempts SET state=?,evidence=? WHERE day=?',(result['decision'],json.dumps(result,sort_keys=True),day))


def assess(day, *, path=None, probe=observe, runner=invoke, sleep=time.sleep, admission=None):
    """Never treat node-side synthetic evidence alone as authority to mutate."""
    from datetime import date
    date.fromisoformat(day)
    first=probe()
    result={'before':first,'decision':'report_only'}
    if healthy(first):
        claim(path,day,reset=True)
        return result
    if not blocking(first,first):
        return result
    sleep(135)
    second=probe()
    result['repeat']=second
    if healthy(second):
        claim(path,day,reset=True)
        result['decision']='transient_recovered'
        return result
    if not blocking(first,second) or not isinstance(first.get("sample_at"),int) or not isinstance(second.get("sample_at"),int) or second["sample_at"]<=first["sample_at"]:
        return result
    try:
        diagnosis=runner('diagnose')
    except (OSError,ValueError,TypeError,KeyError,AttributeError,subprocess.SubprocessError):
        result['decision']='recovery_unavailable'
        return result
    fault=diagnosis.get('fault')
    if fault not in FAULTS or diagnosis.get('local_fault_confirmed') is not True or diagnosis.get('independent_evidence') is not True:
        return result
    result['fault']=fault
    if admission is None:
        from .admission_status import status
        admission=lambda:status()['state']=='idle'
    if not admission():
        result['decision']='admission_busy'
        return result
    if not claim(path,day,fault=fault,evidence=result):
        result['decision']='episode_already_attempted'
        return result
    result['decision']='repair_unconfirmed'
    try:
        receipt=runner('repair',fault)
        outcome=receipt.get('outcome')
        if outcome in ('applied','rolled_back','refused'):
            result['runner_outcome']=outcome
    except (OSError,ValueError,TypeError,KeyError,AttributeError,subprocess.SubprocessError):
        pass
    after=probe()
    result['after']=after
    if healthy(after):
        result['decision']='restored'
    finish(path,day,result)
    if result['decision']=='restored':
        claim(path,day,reset=True)
    return result
