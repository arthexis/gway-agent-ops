"""Hermetic recurring execution acceptance: no node, GitHub or Codex I/O."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import pytest

from gway_agent_ops import recurring_runtime, recurring_store, scheduler
from gway_agent_ops.github_issues import IssueRejected
from gway_agent_ops.task_ledger import TaskLedger

NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
REF = "arthexis/ocpp-csms#346"
OTHER = "arthexis/ocpp-csms#347"


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "private" / "tasks.sqlite3"
    with TaskLedger(p) as db:
        scheduler._ensure(db.db)
    return p


def approve(path, issue=REF):
    with TaskLedger(path) as db:
        db.discover(issue)
        db.decide(issue, "approve", actor="operator:test")


def issue(ref=REF, title="[auto-fix] runtime", body="original"):
    return {"ref": ref, "state": "open", "creator": "arthexis",
            "title": title, "body": body, "repository": ref.split("#")[0],
            "number": int(ref.split("#")[1])}


def fixed(path, *, every="1h", max_runs=100, no_progress_limit=3):
    d = recurring_store.define(REF, every, path=path, now=NOW,
        max_runs=max_runs, no_progress_limit=no_progress_limit)
    assert d["state"] == "paused"
    return recurring_store.transition(d["id"], "resume", path=path, now=NOW)


def budget():
    return {"five_hour": {"available": True, "remaining_percent": 50},
            "weekly": {"available": True, "remaining_percent": 50}}


def run(path, *, now=NOW, resolver=None, submitter=None, observer=None,
        budget_reader=budget, discoverer=None):
    return recurring_runtime.tick(path=path, now=now,
        resolver=resolver or (lambda ref: issue(ref)),
        submitter=submitter or (lambda *a, **k: {"state": "requested"}),
        observer=observer or (lambda *a: {"state": "accepted"}),
        budget_reader=budget_reader,
        discoverer=discoverer or (lambda repo: []))


def state(path, did, ref=REF):
    return next(x for x in recurring_store.snapshot(path=path)["issues"]
                if x["definition_id"] == did and x["issue"] == ref)


def test_paused_by_default_does_not_submit(path):
    approve(path)
    d = recurring_store.define(REF, "1h", path=path, now=NOW)
    attempts = []
    assert run(path, submitter=lambda *a, **k: attempts.append(a)) == {
        "processed": 0, "results": []}
    assert attempts == []
    assert recurring_store.get(d["id"], path=path)["state"] == "paused"


def test_fixed_due_once_and_terminal_hourly_advance(path):
    approve(path)
    d = fixed(path)
    attempts = []
    def submit(ref, at, **kw):
        attempts.append((ref, at, kw))
        return {"state": "requested"}
    first = run(path, submitter=submit)
    assert first["processed"] == 1 and first["results"][0]["state"] == "pending"
    assert len(attempts) == 1
    assert attempts[0][2] == {"source": "scheduled", "trigger": "recurring-due"}
    assert run(path, now=NOW + timedelta(seconds=15), submitter=submit)["processed"] == 0
    assert len(attempts) == 1
    result = run(path, now=NOW + timedelta(minutes=2), submitter=submit,
        observer=lambda *a: {"state": "finished", "exit_code": 0})
    assert result["processed"] == 0
    row = state(path, d["id"])
    assert row["state"] == "pending" and row["last_invocation_at"] is None
    assert row["run_count"] == 1
    assert row["next_due"] == recurring_store._instant(NOW + timedelta(hours=1))
    run(path, now=NOW + timedelta(hours=1, seconds=1), submitter=submit)
    assert len(attempts) == 2 and attempts[0][1] != attempts[1][1]


def test_closed_issue_completes_fixed_schedule_no_auto_reopen(path):
    approve(path)
    d = fixed(path)
    def closed(_):
        raise IssueRejected("issue_not_open")
    result = run(path, resolver=closed)
    assert result["results"][0]["state"] == "completed"
    assert recurring_store.get(d["id"], path=path)["state"] == "completed"
    attempts = []
    assert run(path, submitter=lambda *a, **kw: attempts.append(1))["processed"] == 0
    assert attempts == []


def test_no_approval_never_dispatches(path):
    d = fixed(path)
    attempts = []
    result = run(path, submitter=lambda *a, **kw: attempts.append(1))
    assert result["processed"] == 0
    assert attempts == []
    assert state(path, d["id"])["state"] == "pending"
    approve(path)
    assert run(path)["processed"] == 1


def test_budget_missing_deferred_without_claim(path):
    approve(path)
    d = fixed(path)
    assert run(path, budget_reader=lambda: {"five_hour": {"available": False},
                                            "weekly": {"available": True,
                                                       "remaining_percent": 50}})["processed"] == 0
    row = state(path, d["id"])
    assert row["state"] == "pending" and row["claim_at"] is None
    assert row["run_count"] == 0


def test_busy_deferral_reuses_frozen_identity(path):
    approve(path)
    d = fixed(path)
    attempts = []
    def busy(ref, at, **kw):
        attempts.append(at)
        return {"state": "busy" if len(attempts) == 1 else "requested"}
    first = run(path, submitter=busy)
    assert first["results"][0]["code"] == "busy"
    first_at = state(path, d["id"])["claim_at"]
    assert run(path, now=NOW + timedelta(minutes=1), submitter=busy)["processed"] == 0
    run(path, now=NOW + timedelta(minutes=3), submitter=busy)
    assert attempts == [first_at, first_at]
    assert state(path, d["id"])["run_count"] == 1


def test_launch_uncertain_never_resubmitted_even_after_restart(path):
    approve(path)
    d = fixed(path)
    attempts = []
    def uncertain(*a, **kw):
        attempts.append(1)
        return {"state": "launch_unconfirmed"}
    first = run(path, submitter=uncertain)
    assert first["results"][0]["state"] == "uncertain"
    assert state(path, d["id"])["state"] == "uncertain"
    assert run(path, now=NOW + timedelta(days=2), submitter=uncertain)["processed"] == 0
    assert len(attempts) == 1


def test_crashed_durable_claim_is_not_retried(path):
    approve(path)
    d = fixed(path)
    recurring_store.record_issue(d["id"], REF, NOW, path=path, now=NOW)
    with TaskLedger(path) as db:
        db.db.execute("UPDATE recurring_issue_state SET state='claimed',claim_at=?",
                      (recurring_store._instant(NOW),))
    attempts = []
    run(path, now=NOW + timedelta(days=1), submitter=lambda *a, **kw: attempts.append(1))
    assert attempts == [] and state(path, d["id"])["state"] == "claimed"


def test_concurrent_ticks_one_request(path):
    approve(path)
    d = fixed(path)
    requests = []
    lock = threading.Lock()
    def submit(ref, at, **kw):
        with lock:
            requests.append(at)
        return {"state": "requested"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: run(path, submitter=submit), range(6)))
    assert len(requests) == 1 and state(path, d["id"])["run_count"] == 1


def test_selector_new_matches_need_each_approval(path):
    approve(path, REF)
    d = recurring_store.define("arthexis/ocpp-csms", "1h", title_prefix="[auto-fix]",
                               path=path, now=NOW)
    recurring_store.transition(d["id"], "resume", path=path, now=NOW)
    candidates = [issue(REF), issue(OTHER), issue("arthexis/ocpp-csms#348", title="not selected")]
    attempts = []
    def submit(ref, at, **kw):
        attempts.append(ref)
        return {"state": "requested"}
    def resolver(ref):
        return next(x for x in candidates if x["ref"] == ref)
    first = run(path, discoverer=lambda r: candidates, resolver=resolver, submitter=submit)
    assert first["processed"] == 1 and attempts == [REF]
    approve(path, OTHER)
    run(path, now=NOW + timedelta(minutes=1),
        discoverer=lambda r: candidates, resolver=resolver, submitter=submit)
    assert attempts == [REF, OTHER]
    assert state(path, d["id"], OTHER)["state"] == "pending"


def test_no_progress_threshold_pauses_only_that_issue(path):
    approve(path)
    d = fixed(path, no_progress_limit=1)
    run(path)
    run(path, now=NOW + timedelta(minutes=1),
        observer=lambda *a: {"state": "finished", "exit_code": 0})
    row = state(path, d["id"])
    assert row["state"] == "paused" and row["last_error"] == "no_progress"
    assert run(path, now=NOW + timedelta(days=1))["processed"] == 0


def test_max_runs_bound(path):
    approve(path)
    d = fixed(path, max_runs=1)
    run(path)
    run(path, now=NOW + timedelta(minutes=2),
        observer=lambda *a: {"state": "finished", "exit_code": 0})
    assert state(path, d["id"])["state"] == "paused"
    assert state(path, d["id"])["last_error"] == "max_runs"


def test_change_veto_before_dispatch_blocks(path):
    approve(path)
    d = fixed(path)
    def resolver(ref):
        with TaskLedger(path) as db:
            current = db.get(ref)
            if current["state"] == "approved":
                db.decide(ref, "withdraw_approved", actor="operator:test")
        return issue(ref)
    # The resolver runs after discovery and before submit; revoked approval
    # means no Codex request, even when the earlier tick snapshot was approved.
    attempts = []
    run(path, resolver=resolver, submitter=lambda *a, **kw: attempts.append(1))
    assert attempts == []
    assert state(path, d["id"])["state"] in ("pending", "uncertain")


def test_prefix_and_bounded_regex(path):
    regex = recurring_store.define("arthexis/ocpp-csms", "1h",
        title_regex=r"^\[auto-fix\]\s+", path=path)
    assert regex["state"] == "paused"
    policy = recurring_runtime._policy(regex)
    assert policy.matches(issue(REF))
    assert not policy.matches(issue(REF, title="other"))
    for pattern in (r"^(a+)+$", "foo", r"^a|b", r"^(?=a)b"):
        with pytest.raises(ValueError):
            recurring_store.define("arthexis/ocpp-csms", "1h",
                                   title_regex=pattern, path=path)


def test_discover_paginates_excludes_prs_and_fails_closed():
    class Result:
        def __init__(self, rows, rc=0):
            self.returncode, self.stdout = rc, __import__("json").dumps(rows)
    seen = []
    def runner(argv, **kw):
        seen.append(argv)
        if "page=1" in argv:
            return Result([{"number": 4, "state": "open", "title": "ok",
                "user": {"login": "arthexis"}},
                {"number": 5, "state": "open", "pull_request": {},
                 "title": "skip", "user": {"login": "arthexis"}}])
        raise AssertionError("unnecessary page")
    rows = recurring_runtime.discover("arthexis/ocpp-csms", runner=runner)
    assert [x["ref"] for x in rows] == ["arthexis/ocpp-csms#4"]
    assert len(seen) == 1
    with pytest.raises(ValueError, match="recurring_discovery_unavailable"):
        recurring_runtime.discover("arthexis/ocpp-csms",
            runner=lambda *a, **kw: Result([], rc=1))


def test_snapshot_has_no_writes_and_audit(path):
    d = recurring_store.define(REF, "1h", path=path)
    before = path.read_bytes()
    result = recurring_store.snapshot(path=path)
    assert result["definitions"][0]["id"] == d["id"]
    assert len(result["audit"]) == 1
    assert path.read_bytes() == before


def test_recurring_definition_is_idempotent_and_activation_explicit(path):
    a = recurring_store.define(REF, "1h", path=path)
    b = recurring_store.define(REF, "1h", path=path)
    assert a["id"] == b["id"] and b["state"] == "paused"
    assert recurring_store.transition(a["id"], "resume", path=path)["state"] == "active"
    assert recurring_store.transition(a["id"], "pause", path=path)["state"] == "paused"
    assert recurring_store.transition(a["id"], "cancel", path=path)["state"] == "cancelled"
    with pytest.raises(ValueError, match="recurring_cancelled"):
        recurring_store.transition(a["id"], "resume", path=path)
