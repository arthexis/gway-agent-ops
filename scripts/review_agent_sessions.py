#!/usr/bin/env python3
"""Read-only inventory of local AgentOps Codex sessions; never archives."""
import argparse
import collections
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

PATTERN = re.compile(r'^Work on the trusted GitHub issue ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+)\.')

def command(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=25, check=False)
        return json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--database', type=Path, default=Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))) / 'state_5.sqlite')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    try:
        db = sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT id,cwd,first_user_message,created_at,updated_at,attached_thread_parent_id FROM threads WHERE archived=0 ORDER BY created_at').fetchall()
        children = collections.Counter(r[0] for r in db.execute('SELECT attached_thread_parent_id FROM threads WHERE archived=0 AND attached_thread_parent_id IS NOT NULL'))
        db.close()
    except (sqlite3.Error, OSError) as exc:
        print('Cannot read Codex session index: ' + str(exc), file=sys.stderr); return 1
    history = command(['agent','history','--limit','1000','--json'])
    status = command(['agent','status','--json'])
    if not isinstance(history, dict) or history.get('status') != 'ok': history = None
    if not isinstance(status, dict) or status.get('status') != 'ok': status = None
    executions = history.get('data',{}).get('executions',[]) if history else []
    reservations = status.get('data',{}).get('admission',{}).get('reservations',[]) if status else []
    issues = {}
    output = []
    for row in rows:
        match = PATTERN.match(row['first_user_message'] or '')
        if not match: continue
        ref = match.group(1)
        if ref not in issues:
            repo, number = ref.split('#')
            data = command(['gh','issue','view',number,'--repo',repo,'--json','state,title,url'])
            issues[ref] = data if isinstance(data,dict) and data.get('state') in ('OPEN','CLOSED') else None
        # A close timestamp is only a candidate match, not a proven thread identity.
        nearby=[]
        for e in executions:
            if e.get('issue') != ref: continue
            try: delta = abs(datetime.fromisoformat(e['at'].replace('Z','+00:00')).timestamp()-row['created_at'])
            except (KeyError,ValueError,TypeError): continue
            if delta <= 120: nearby.append({'at':e['at'],'state':e.get('state'),'delta_seconds':round(delta)})
        active = [r.get('at') for r in reservations if r.get('issue') == ref]
        issue=issues[ref]
        verdict='review'
        if not history or not status or not issue: verdict='unknown'
        elif active: verdict='active_issue'
        elif issue['state']=='OPEN': verdict='open_issue'
        elif children[row['id']]: verdict='has_unarchived_descendants'
        elif len(nearby)==1 and nearby[0]['state'] in ('finished','released','failed','interrupted'): verdict='candidate_verify_thread'
        output.append({'thread_id':row['id'],'issue':ref,'issue_state':issue['state'] if issue else 'unknown','title':issue.get('title') if issue else None,'url':issue.get('url') if issue else None,'created_at':datetime.fromtimestamp(row['created_at'],timezone.utc).isoformat(),'cwd':row['cwd'],'unarchived_children':children[row['id']],'nearby_executions':nearby,'active_issue_reservations':active,'assessment':verdict})
    result={'source':str(args.database),'history_available':bool(history),'status_available':bool(status),'history_limit':1000,'sessions':output,'counts':dict(collections.Counter(x['assessment'] for x in output)),'note':'Candidate execution matches use issue and time proximity, not proven identity. Check thread completion, descendants and current status before archiving.'}
    if args.json: print(json.dumps(result,indent=2))
    else:
        print('AgentOps sessions:',len(output),' assessments:',result['counts'])
        for x in output: print(f"{x['issue']:35} {x['issue_state']:7} {x['assessment']:28} {x['thread_id']} {x['created_at']}")
        print(result['note'])
    return 0
if __name__=='__main__': sys.exit(main())
