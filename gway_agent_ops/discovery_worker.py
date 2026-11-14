"""Bounded, read-only discovery of opted-in issues independent of deploy targets."""
import sqlite3
import time

from .github_issues import IssueRejected
from .github_search import search_issues
from .task_ledger import TaskLedger


def list_opted_in(*, runner=None):
    """Search accessible GitHub repositories, not Remote's deploy registry."""
    options = {"label": "local-agent"}
    if runner is not None:
        options["runner"] = runner
    return [f'{item["repository"]}#{item["number"]}' for item in search_issues(**options)]


def tick(ledger: TaskLedger, *, listing=list_opted_in, resolver=None):
    """Synchronize trusted opt-ins and reconcile; never schedule execution."""
    report = {"discovered": 0, "reconciled": 0, "errors": []}
    try:
        refs = listing()
    except (OSError, ValueError, IssueRejected):
        report["errors"].append("discovery_unavailable")
        return report
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
    """Sequential discovery loop. Errors never dispatch or requeue tasks."""
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
