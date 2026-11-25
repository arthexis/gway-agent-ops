"""Read sanitized projections only; never hold an MCP credential."""
import json
import base64
import subprocess
import tempfile
import os
import stat
import time
from pathlib import Path

PROJECTION=Path('/var/lib/gway-mcp-external-health/status.json')


EXECUTION=Path('/var/lib/gway-mcp-execution-health/status.json')
ROUTES=Path('/var/lib/gway-mcp-cloud-repair/status.json')
ROUTE_KEY=Path('/etc/gway-remote/cloud-repair-monitor.pub')


def _projection(path, *, now=None, owner=0, max_age=1200):
    now=time.time() if now is None else now
    parent=path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=owner or parent.st_mode&0o022:
        raise ValueError('unsafe_observer_directory')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(fd,'rb') as f:
        info=os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=owner or info.st_mode&0o022 or info.st_nlink!=1 or info.st_size>4096:
            raise ValueError('unsafe_observer_projection')
        d=json.loads(f.read(4097))
    if type(d) is not dict or d.get('schema')!=1 or type(d.get('sample_at')) is not int or not 0<=now-d['sample_at']<=max_age:
        raise ValueError('stale_observer_projection')
    return d


def read_observer(path=PROJECTION, *, now=None, owner=0):
    d=_projection(path,now=now,owner=owner)
    if set(d)!={'schema','sample_at','anonymous','read','optional_failures','optional_checked'}:
        raise ValueError('stale_observer_projection')
    if d['anonymous'] not in ('denied','unexpected_access','unavailable') or d['read'] not in ('ok','unavailable','authorization','http_failure','transport_or_protocol') or any(type(d[k]) is not int or not 0<=d[k]<=6 for k in ('optional_failures','optional_checked')) or d['optional_failures']>d['optional_checked']:
        raise ValueError('invalid_observer_projection')
    return d


def read_execution(path=EXECUTION, *, now=None, owner=0):
    d=_projection(path,now=now,owner=owner)
    if set(d)!={'schema','sample_at','execute','failure_stage','alternate_routes'} or d['alternate_routes']!='unverified' or d['execute'] not in ('ok','credential_unavailable','authorization','http_failure','transport_or_protocol','execution_unverified','exec_infra_failure'):
        raise ValueError('invalid_execution_projection')
    if (d['execute']=='exec_infra_failure' and d['failure_stage'] not in ('ssh_transport','worker_readiness')) or (d['execute']!='exec_infra_failure' and d['failure_stage'] is not None):
        raise ValueError('invalid_execution_stage')
    return d


def verify_routes_signature(d, key=ROUTE_KEY):
    from .mcp_recovery import trusted_file
    public_key=trusted_file(key,4096)
    signature=base64.b64decode(d['signature'],validate=True)
    if len(signature)!=64:raise ValueError('invalid_route_signature')
    payload=json.dumps({k:v for k,v in d.items() if k!='signature'},sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        for name,value in (('key',public_key),('payload',payload),('sig',signature)):(root/name).write_bytes(value)
        result=subprocess.run(['/usr/bin/openssl','pkeyutl','-verify','-pubin','-inkey',str(root/'key'),'-rawin','-in',str(root/'payload'),'-sigfile',str(root/'sig')],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3)
        if result.returncode:raise ValueError('invalid_route_signature')


def read_routes(path=ROUTES, *, now=None, owner=0, verify=verify_routes_signature):
    """Only an independently signed and complete cloud route sample can admit repair."""
    d=_projection(path,now=now,owner=owner,max_age=300)
    if set(d)!={'schema','sample_at','source','complete','routes','signature'} or d['source']!='independent_cloud_monitor' or d['complete'] is not True or type(d['routes']) is not dict or set(d['routes'])!={'watchtower_deployment','github_workflow','alternate_node_control'} or any(v not in ('available','blocked','unverified') for v in d['routes'].values()) or type(d['signature']) is not str:
        raise ValueError('invalid_cloud_route_projection')
    verify(d)
    return {'alternate_routes':'blocked' if all(v=='blocked' for v in d['routes'].values()) else 'available' if 'available' in d['routes'].values() else 'unverified','route_sample_at':d['sample_at']}


def observe(*, observer=read_observer, execution=read_execution, routes=read_routes):
    result={'vantage':'node_synthetic','cloud_client':'unverified','read':'observer_unavailable','execute':'unverified','anonymous':'unverified','alternate_routes':'unverified'}
    for source,keys in ((observer,('read','anonymous','sample_at','optional_failures','optional_checked')),(execution,('execute','failure_stage')),(routes,('alternate_routes','route_sample_at'))):
        try:
            d=source()
            result.update({k:d[k] for k in keys})
            if source is execution:result['execute_sample_at']=d['sample_at']
        except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError):
            pass
    return result
