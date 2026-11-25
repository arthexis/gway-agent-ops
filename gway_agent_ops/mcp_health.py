"""Bounded synthetic MCP probes. Never reuse a node-local service credential."""
import json
import os
import stat
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

URL = 'https://remote.arthexis.com/mcp'
PROTOCOL = '2025-03-26'
MAX = 1024*1024

class Failure(Exception):
    pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def token_file(path):
    fd = os.open(path, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
    with os.fdopen(fd,'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError('unsafe_probe_credential')
        token = stream.read(8193).decode('ascii').strip()
        if not token or len(token)>8192 or any(c.isspace() for c in token):
            raise ValueError('invalid_probe_credential')
        return token


def request(token, method, params=None):
    headers={'Accept':'application/json, text/event-stream','Content-Type':'application/json','MCP-Protocol-Version':PROTOCOL}
    if token:
        headers['Authorization']='Bearer '+token
    req=Request(URL,json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params or {}}).encode(),headers,method='POST')
    try:
        with build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=8) as r:
            raw=r.read(MAX+1)
            if len(raw)>MAX:
                raise Failure('response_limit')
            doc=json.loads(raw)
    except HTTPError as exc:
        code=exc.code
        exc.close()
        raise Failure('authorization' if code in (401,403) else 'http_failure' if 500<=code<600 else 'client_error') from None
    except (OSError,ValueError):
        raise Failure('transport_or_protocol') from None
    if not isinstance(doc,dict) or doc.get('id')!=1 or 'error' in doc or not isinstance(doc.get('result'),dict):
        raise Failure('protocol_failure')
    return doc['result']


def call(token,name,args,transport=request):
    r=transport(token,'tools/call',{'name':name,'arguments':args})
    if r.get('isError'):
        raise Failure('tool_rejected')
    try:
        data=json.loads(r['content'][0]['text'])
    except (KeyError,TypeError,IndexError,ValueError):
        raise Failure('invalid_tool_result') from None
    if not isinstance(data,dict):
        raise Failure('invalid_tool_result')
    return data


def probe_exec(token, transport=request, sleep=None):
    import time
    sleep=sleep or time.sleep
    rid=uuid.uuid4().hex
    try:
        result=call(token,'node_exec',{'node':'gway-001','command':'printf %s '+rid,'request_id':rid,'wait_seconds':0},transport)
        if result.get('status')=='uncertain':
            raise Failure('tool_rejected')
    except Failure as exc:
        if str(exc)!='tool_rejected':
            raise
        try:
            diagnostic=call(token,'node_exec_diagnostic',{'request_id':rid},transport)
        except Failure:
            raise Failure('execution_unverified') from None
        if diagnostic.get('request_id')==rid and diagnostic.get('status')=='failure' and diagnostic.get('stage') in ('pinned_transport','bridge_dispatch','ssh_transport','worker_readiness'):
            raise Failure('exec_infra_failure') from None
        raise Failure('execution_unverified') from None
    if result.get('request_id')!=rid or result.get('node')!='gway-001':
        raise Failure('execution_identity')
    # Always check receipt retrieval independently of the submission result.
    for _ in range(5):
        result=call(token,'node_result',{'request_id':rid},transport)
        if result.get('request_id')!=rid or result.get('node')!='gway-001':
            raise Failure('execution_identity')
        if result.get('status')=='succeeded' and result.get('exit_code')==0 and result.get('stdout')==rid:
            return True
        if result.get('status') not in ('dispatched','running','queued'):
            raise Failure('execution_failed')
        sleep(1)
    raise Failure('execution_timeout')


PROJECTION=Path('/var/lib/gway-mcp-external-health/status.json')


def read_observer(path=PROJECTION, *, now=None, owner=0):
    import time
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
    if set(d)!={'schema','sample_at','anonymous','read','optional_failures','optional_checked'} or d['schema']!=1 or type(d['sample_at']) is not int or not 0<=now-d['sample_at']<=1200:
        raise ValueError('stale_observer_projection')
    if d['anonymous'] not in ('denied','unexpected_access','unavailable') or d['read'] not in ('ok','unavailable','authorization','http_failure','transport_or_protocol') or any(type(d[k]) is not int or not 0<=d[k]<=6 for k in ('optional_failures','optional_checked')) or d['optional_failures']>d['optional_checked']:
        raise ValueError('invalid_observer_projection')
    return d


def observe(*, home=None, transport=request, sleep=None, observer=read_observer):
    """Use the existing root-owned observer, never its private OAuth credential."""
    home=Path.home() if home is None else Path(home)
    result={'vantage':'node_synthetic','cloud_client':'unverified','read':'observer_unavailable','execute':'unverified','anonymous':'unverified'}
    try:
        d=observer()
        result.update({k:d[k] for k in ('read','anonymous','sample_at','optional_failures','optional_checked')})
    except (OSError,ValueError,TypeError,KeyError):
        pass
    try:
        token=token_file(home/'.config/gway-agent-ops/mcp-health-execute.token')
    except (OSError,ValueError):
        result['execute']='credential_unavailable'
        return result
    try:
        probe_exec(token,transport,sleep=sleep)
    except Failure as e:
        result['execute']=str(e)
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        result['execute']='invalid_response'
    else:
        result['execute']='ok'
    return result
