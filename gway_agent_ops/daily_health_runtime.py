"""Bounded daily audit and marker-based GitHub reconciliation."""
import json
import os
import re
import socket
import subprocess
from datetime import date, datetime, timedelta, timezone
from . import daily_health_store as store
from .daily_health_audit import collect_extended
from .scheduler import ledger_path
from .task_ledger import TaskLedger

REPO = "arthexis/gway-remote"

def gh(endpoint, *args):
    result = subprocess.run(["gh", "api", "--hostname", "github.com", endpoint, *args],
        capture_output=True, text=True, timeout=15, check=False,
        env={k:v for k,v in os.environ.items() if k not in ("GH_TOKEN","GITHUB_TOKEN")})
    if result.returncode:
        raise RuntimeError("github_unavailable")
    return json.loads(result.stdout)

def pages(endpoint, api=gh):
    rows = []
    for page in range(1, 21):
        batch = api(endpoint + ("&" if "?" in endpoint else "?") + f"per_page=100&page={page}")
        if not isinstance(batch, list):
            raise RuntimeError("github_invalid_response")
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise RuntimeError("github_pagination_limit")

def write_once(component, day, endpoint, args, *, api, path):
    """Persist intent before POST; never blindly retry an ambiguous write."""
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        db.execute("CREATE TABLE IF NOT EXISTS daily_health_writes (component TEXT, day TEXT, state TEXT, PRIMARY KEY(component,day))")
        db.execute("BEGIN IMMEDIATE")
        try:
            pending = db.execute("SELECT 1 FROM daily_health_writes WHERE component=? AND state='pending'", (component,)).fetchone()
            if pending:
                raise RuntimeError("github_write_unconfirmed")
            db.execute("INSERT INTO daily_health_writes VALUES (?,?,'pending')", (component,day))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        result = api(endpoint, *args)
        db.execute("UPDATE daily_health_writes SET state='confirmed' WHERE component=? AND day=?", (component,day))
        return result

def confirm_visible(component, day, *, path):
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        db.execute("CREATE TABLE IF NOT EXISTS daily_health_writes (component TEXT, day TEXT, state TEXT, PRIMARY KEY(component,day))")
        db.execute("UPDATE daily_health_writes SET state='confirmed' WHERE component=? AND day=?", (component,day))

def mapping(component, repo, *, path, number=None):
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        db.execute('CREATE TABLE IF NOT EXISTS daily_health_mapping (component TEXT PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL)')
        if number is not None:
            db.execute('INSERT OR REPLACE INTO daily_health_mapping VALUES (?,?,?)',(component,repo,number))
        row = db.execute('SELECT number FROM daily_health_mapping WHERE component=? AND repo=?',(component,repo)).fetchone()
        return row[0] if row else None

def reconcile(finding, local_date, *, api=gh, path=None):
    component = finding["component"]
    if not re.fullmatch(r"[a-z0-9_]{1,80}", component):
        raise ValueError("invalid_component")
    date.fromisoformat(local_date)
    marker = f"<!-- gway-health:gway-001/{component} -->"
    daily = f"<!-- gway-health-date:{local_date} -->"
    repo = "arthexis/gway-agent-ops" if component.startswith("agentops_") else REPO
    base = f"repos/{repo}/issues"
    canonical = mapping(component, repo, path=path)
    rows = pages(base+"?state=open", api)
    if not any(marker in (r.get("body") or "") for r in rows):
        rows += pages(base+"?state=closed", api)
    matches = [r for r in rows if "pull_request" not in r and (marker in (r.get("body") or "") or r.get("number") == canonical)]
    # A semantic candidate is not authorization to write to an unrelated issue.
    # Require a unique node-specific title with the same metric words.
    if canonical is not None and not matches:
        raise RuntimeError("canonical_issue_unavailable")
    if not matches:
        words = set(component.split('_')) - {'percent','bytes','min'}
        candidates = [r for r in rows if r.get('state') == 'open' and 'pull_request' not in r
                      and 'gway-001' in (r.get('title') or '').lower()
                      and words <= set(re.findall(r'[a-z0-9]+', (r.get('title') or '').lower()))]
        if len(candidates) > 1:
            raise RuntimeError('ambiguous_semantic_match')
        if candidates:
            matches = candidates
    opened = [r for r in matches if r.get("state") == "open"]
    if len(opened) > 1:
        raise RuntimeError("duplicate_existing_issues")
    body = f"{marker}\n{daily}\nObservation: {local_date}\nSeverity: {finding['severity']}\nMetric: {component} = {finding['value']}\nInvestigate the underlying cause and verify recovery against the daily baseline."
    # Recover prior uncertain writes only when their exact date marker is visible.
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        db.execute("CREATE TABLE IF NOT EXISTS daily_health_writes (component TEXT, day TEXT, state TEXT, PRIMARY KEY(component,day))")
        pending = [r[0] for r in db.execute("SELECT day FROM daily_health_writes WHERE component=? AND state='pending'", (component,))]
    for day in pending:
        token = f"<!-- gway-health-date:{day} -->"
        for row in matches:
            if token in (row.get("body") or "") or any(token in (r.get("body") or "") for r in pages(f"{base}/{int(row['number'])}/comments",api)):
                confirm_visible(component, day, path=path)
                break
    if opened:
        number = int(opened[0]["number"])
        mapping(component,repo,path=path,number=number)
        if daily in (opened[0].get("body") or "") or any(daily in (r.get("body") or "") for r in pages(f"{base}/{number}/comments", api)):
            return {"issue": number, "action": "already_reported"}
        write_once(component, local_date, f"{base}/{number}/comments", ("-X","POST","-f","body="+body), api=api,path=path)
        return {"issue": number, "action": "updated"}
    if finding["severity"] == "recovered":
        return {"action":"no_open_issue"}
    if matches:
        body += "\nPrevious occurrence: #" + str(max(int(r["number"]) for r in matches))
    row = write_once(component,local_date,base,("-X","POST","-f","title=[gway-001 health] "+component,"-f","body="+body),api=api,path=path)
    mapping(component,repo,path=path,number=int(row["number"]))
    return {"issue": int(row["number"]), "action": "created"}

def recovery_findings(day, report, *, path=None):
    # Missing measurements are never evidence of recovery.
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        db.execute('CREATE TABLE IF NOT EXISTS daily_health_baselines (local_date TEXT PRIMARY KEY, report TEXT NOT NULL)')
        rows = db.execute('SELECT report FROM daily_health_baselines WHERE local_date < ? ORDER BY local_date DESC LIMIT 30',(day,)).fetchall()
    active = {f['component'] for f in report['findings']}
    unresolved = set(report['metrics']) - active
    recovered = []
    for row in rows:
        previous = json.loads(row[0])
        findings = {f['component'] for f in previous['findings']}
        for component in sorted(unresolved & set(previous['metrics'])):
            if component in findings:
                recovered.append({'component':component,'severity':'recovered','value':report['metrics'][component]})
            unresolved.remove(component)
    return recovered


def persist(local_date, report, *, path=None):
    cutoff = (date.fromisoformat(local_date)-timedelta(days=29)).isoformat()
    with TaskLedger(ledger_path(path)) as ledger:
        ledger.db.execute("CREATE TABLE IF NOT EXISTS daily_health_baselines (local_date TEXT PRIMARY KEY, report TEXT NOT NULL)")
        ledger.db.execute("INSERT OR REPLACE INTO daily_health_baselines VALUES (?,?)", (local_date,json.dumps(report,sort_keys=True)))
        ledger.db.execute("DELETE FROM daily_health_baselines WHERE local_date < ?", (cutoff,))

def run(claim, *, path=None, collector=collect_extended, publisher=None, mcp_check=None):
    day = claim["local_date"]
    if publisher is None:
        publisher = lambda f,d: reconcile(f,d,path=path)
    try:
        report = collector()
        from .mcp_recovery import assess
        mcp = (mcp_check or assess)(day,path=path)
        report['mcp_health'] = mcp
        observed = mcp.get('after',mcp.get('repeat',mcp['before']))
        for key,unverified in (('read',('observer_unavailable','unavailable','authorization')),('execute',('credential_unavailable','authorization','unverified','execution_unverified','client_error'))):
            name='mcp_'+key+'_path_failed'
            if observed[key] in unverified:
                report['errors'].append('mcp_'+key+'_probe_unverified')
            else:
                value=int(observed[key]!='ok')
                report['metrics'][name]=value
                if value:
                    report['findings'].append({'component':name,'severity':'warning','value':value})
        if observed['anonymous']=='unexpected_access':
            report['metrics']['mcp_anonymous_access']=1
            report['findings'].append({'component':'mcp_anonymous_access','severity':'critical','value':1})
        elif observed['anonymous']=='denied':
            report['metrics']['mcp_anonymous_access']=0
        if 'optional_failures' in observed:
            value=observed['optional_failures']
            report['metrics']['mcp_optional_provider_failures']=value
            if value:
                report['findings'].append({'component':'mcp_optional_provider_failures','severity':'warning','value':value})
        recovered = recovery_findings(day, report, path=path)
        persist(day, report, path=path)
        results = [publisher(f, day) for f in report["findings"] + recovered]
    except Exception:
        store.finish(day, "failed", path=path)
        raise
    store.finish(day, "finished" if not report["errors"] else "failed", path=path)
    from .daily_health_notify import notify
    notify(day, report, path=path)
    return {"report": report, "issues": results}

def stopped(day):
    """Require positive systemd evidence; errors are not proof of absence."""
    date.fromisoformat(day)
    try:
        r = subprocess.run(["systemctl","--user","show","gway-daily-health-"+day+".service",
            "--property=LoadState","--property=ActiveState","--property=SubState"],
            capture_output=True,text=True,timeout=5,check=False)
        fields = dict(line.split("=",1) for line in r.stdout.splitlines() if "=" in line)
        return r.returncode == 0 and fields.get("ActiveState") in ("inactive","failed") and fields.get("SubState") in ("dead","failed") and fields.get("LoadState") in ("loaded","not-found")
    except (OSError, subprocess.SubprocessError):
        return False

def recover_claims(*, path=None, now=None, probe=stopped):
    now = now or datetime.now(timezone.utc)
    recovered = []
    for row in store.inspect(path=path)["claims"]:
        if row["state"] != "claimed":
            continue
        at = datetime.fromisoformat(row["claimed_at"].replace("Z","+00:00"))
        if now-at >= timedelta(minutes=35) and probe(row["local_date"]):
            try:
                store.finish(row["local_date"],"failed",path=path,now=now)
                recovered.append(row["local_date"])
            except ValueError:
                pass
    return recovered

def tick(*, path=None, now=None, hostname=socket.gethostname, launch=None):
    if hostname() != "gway-001":
        return None
    if not store.inspect(path=path)["schedule"]:
        return None
    recover_claims(path=path,now=now)
    from .admission_status import status
    if status()["state"] != "idle":
        return None
    claim = store.claim(path=path, now=now)
    if claim is None:
        return None
    if launch is None:
        import sys
        launch = lambda c: subprocess.run(["systemd-run","--user","--quiet","--collect",
            "--unit=gway-daily-health-"+c["local_date"], "--property=RuntimeMaxSec=1800",
            "--property=Nice=10", sys.executable,"-m","gway_agent_ops.daily_health_runtime",
            c["local_date"], str(ledger_path(path))], timeout=15, check=True)
    launch(claim)
    return claim

if __name__ == "__main__":
    import sys
    from .environment import load_runtime_environment
    load_runtime_environment()
    run({"local_date":sys.argv[1]}, path=sys.argv[2])
