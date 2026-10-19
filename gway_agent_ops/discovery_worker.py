"""Bounded, read-only discovery iteration. Never claims or executes tasks."""
import json
import os
import subprocess
import time
import sqlite3

from .github_issues import IssueRejected
from .task_ledger import TaskLedger

def remote_snapshot(runner=subprocess.run):
    result = runner(["gway-remote", "deploy-targets", "--json"],
                    capture_output=True, text=True, timeout=20, check=True)
    return result.stdout

def list_opted_in(repo, runner=subprocess.run):
    result = runner(["gh", "api", "--paginate", "--slurp",
                     f"repos/{repo}/issues?state=open&labels=local-agent&per_page=100"],
                    capture_output=True, text=True, timeout=30, check=True,
                    env={k: v for k, v in os.environ.items()
                         if k not in ("GITHUB_TOKEN", "GH_TOKEN")})
    pages = json.loads(result.stdout)
    if not isinstance(pages, list):
        raise ValueError("invalid issue listing")
    issues = []
    for page in pages:
        if not isinstance(page, list):
            raise ValueError("invalid issue page")
        for item in page:
            if not isinstance(item, dict) or type(item.get("number")) is not int:
                raise ValueError("invalid issue entry")
            if "pull_request" not in item:
                issues.append(f'{repo}#{item["number"]}')
    return issues

def tick(ledger: TaskLedger, *, snapshot=remote_snapshot, listing=list_opted_in,
         resolver=None):
    """Synchronize allowlist, discover verified issues, reconcile existing ones."""
    report = {"discovered": 0, "reconciled": 0, "errors": []}
    try:
        ledger.refresh_repositories(snapshot())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        ledger.invalidate_registry()
        report["errors"].append("registry_unavailable")
        return report
    for repo in ledger.registry_status()["repositories"]:
        try:
            refs = listing(repo)
        except (OSError, ValueError, subprocess.SubprocessError):
            report["errors"].append(f"listing_unavailable:{repo}")
            continue
        for ref in refs:
            try:
                previous = ledger.get(ref)
                ledger.discover_verified(ref, resolver=resolver)
                if previous is None:
                    report["discovered"] += 1
            except (IssueRejected, ValueError):
                report["errors"].append(f"verification_failed:{ref}")
    for task in ledger.list_tasks():
        if task["state"] not in ("requested", "approved"):
            continue
        try:
            ledger.reconcile_issue(task["issue"], resolver=resolver)
            report["reconciled"] += 1
        except (IssueRejected, ValueError):
            report["errors"].append(f"reconcile_unavailable:{task['issue']}")
    return report

def run_forever(*, ledger_path, interval=60, stop=None, sleep=time.sleep, wake=None,
                open_ledger=TaskLedger, iteration=tick, report=None):
    """Sequential discovery loop. Never runs a task; errors do not stop polling.

    A caller-owned stop predicate permits graceful shutdown without signal handlers.
    Each iteration closes its SQLite connection before sleeping.
    """
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 5 <= interval <= 3600:
        raise ValueError("interval must be between 5 and 3600 seconds")
    should_stop = stop or (lambda: False)
    emit = report or (lambda result: None)
    while not should_stop():
        try:
            with open_ledger(ledger_path) as ledger:
                result = iteration(ledger)
        except (OSError, ValueError, sqlite3.Error):
            result = {"discovered": 0, "reconciled": 0,
                      "errors": ["discovery_iteration_failed"]}
        emit(result)
        if should_stop():
            break
        if wake is None:
            sleep(interval)
        else:
            wake.wait(interval)
