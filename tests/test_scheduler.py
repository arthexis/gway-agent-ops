"""Hermetic one-shot scheduler regressions; never calls Codex, systemd or GitHub."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from gway_agent_ops import scheduler
from gway_agent_ops.task_ledger import TaskLedger

REF = "arthexis/ocpp-csms#355"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
DUE = (NOW + timedelta(hours=1)).isoformat()


def trusted(_):
    return {"labels": ["gway-001"], "state": "open", "creator": "arthexis"}



def _budget(percent=100):
    observed = datetime.now(timezone.utc)
    return {window: {"available": True, "remaining_percent": percent,
                     "source": "test", "observed_at": observed.isoformat(),
                     "resets_at": (observed + timedelta(hours=1)).isoformat()}
            for window in ("five_hour", "weekly")}


@pytest.fixture(autouse=True)
def live_quota_stub(monkeypatch):
    # Every legacy scheduler test remains hermetic after the live quota gate.
    monkeypatch.setattr(scheduler, "read_live_budgets", _budget)


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "private" / "tasks.sqlite3"
    with TaskLedger(path) as entry:
        entry.discover(REF)
        entry.decide(REF, "approve", actor="operator")
    return path


def test_time_validation():
    assert scheduler.parse_run_at("2026-11-15T07:00:00-06:00", now=NOW).startswith("2026-11-15T13:00")
    for value in ("2026-11-15T13:00:00", "yesterday", DUE + "junk"):
        with pytest.raises(ValueError):
            scheduler.parse_run_at(value, now=NOW)
    with pytest.raises(ValueError):
        scheduler.parse_run_at((NOW + timedelta(days=91)).isoformat(), now=NOW)
    with pytest.raises(ValueError):
        scheduler.parse_run_at(NOW.isoformat(), now=NOW)


def test_create_cancel_and_no_dispatch(ledger):
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    assert record["state"] == "scheduled"
    assert scheduler.get(record["schedule_id"], ledger_path=ledger)["issue"] == REF
    assert len(scheduler.list_schedules(ledger_path=ledger)) == 1
    assert scheduler.tick(ledger_path=ledger, now=NOW, resolver=trusted,
                          submitter=lambda *_a, **_k: pytest.fail("early dispatch"))["processed"] == 0
    assert scheduler.cancel(record["schedule_id"], ledger_path=ledger)["state"] == "cancelled"
    assert scheduler.tick(ledger_path=ledger, now=NOW + timedelta(days=1),
                          resolver=trusted)["processed"] == 0
    with pytest.raises(ValueError):
        scheduler.cancel(record["schedule_id"], ledger_path=ledger)


def test_at_most_once_across_restarts(ledger):
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    calls = []
    def fake_submit(ref, at, **kw):
        calls.append((ref, at, kw))
        return {"state": "requested"}
    first = scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2),
                           resolver=trusted, submitter=fake_submit)
    assert first["processed"] == 1
    assert first["results"][0]["state"] == "requested"
    assert len(calls) == 1
    assert calls[0][1] == record["dispatch_at"]
    second = scheduler.tick(ledger_path=ledger, now=NOW + timedelta(days=1),
                            resolver=trusted, submitter=fake_submit)
    assert second["processed"] == 0
    assert len(calls) == 1


def test_revoked_trust_never_dispatches(ledger):
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    def revoked(_):
        return {"labels": [], "state": "closed", "creator": "arthexis"}
    result = scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2),
                             resolver=revoked,
                             submitter=lambda *_a, **_k: pytest.fail("dispatch forbidden"))
    assert result["results"][0]["state"] == "blocked"
    assert scheduler.get(record["schedule_id"], ledger_path=ledger)["state"] == "blocked"


def test_unlabeled_trusted_issue_remains_eligible(ledger):
    unlabeled = lambda _ref: {"labels": [], "state": "open", "creator": "arthexis"}
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=unlabeled)
    assert record["state"] == "scheduled"
    outcomes = scheduler.tick(
        ledger_path=ledger, now=NOW + timedelta(hours=2), resolver=unlabeled,
        submitter=lambda *_a, **_k: {"state": "requested"})
    assert outcomes["results"][0]["state"] == "requested"


def test_untrusted_author_fails_closed(ledger):
    foreign = lambda _ref: {"labels": ["gway-001"], "state": "open", "creator": "other"}
    with pytest.raises(ValueError, match="untrusted_issue"):
        scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=foreign)


def test_ambiguous_and_busy_are_not_retried(ledger):
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    result = scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2),
                             resolver=trusted,
                             submitter=lambda *_a, **_k: {"state": "launch_unconfirmed"})
    assert result["results"][0]["state"] == "launch_unconfirmed"
    assert scheduler.tick(ledger_path=ledger, now=NOW + timedelta(days=1),
                          resolver=trusted)["processed"] == 0


def test_claimed_crash_is_not_replayed(ledger):
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    db = sqlite3.connect(ledger)
    db.execute("UPDATE schedules SET state='claimed' WHERE schedule_id=?",
               (record["schedule_id"],))
    db.commit()
    db.close()
    assert scheduler.tick(ledger_path=ledger, now=NOW + timedelta(days=1),
                          resolver=trusted,
                          submitter=lambda *_a, **_k: pytest.fail("replayed"))["processed"] == 0


def test_mcp_annotations():
    from gway_agent_ops.mcp_stdio import TOOLS
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert by_name["schedule"]["annotations"]["readOnlyHint"] is False
    assert by_name["schedule_cancel"]["annotations"]["readOnlyHint"] is False
    assert by_name["scheduled_list"]["annotations"]["readOnlyHint"] is True
    assert by_name["scheduled_get"]["annotations"]["readOnlyHint"] is True


def test_frozen_transport_identity_is_idempotent(ledger):
    request_at = NOW.isoformat()
    first = scheduler.create(REF, DUE, request_at=request_at,
                             ledger_path=ledger, now=NOW, resolver=trusted)
    second = scheduler.create(REF, DUE, request_at=request_at,
                              ledger_path=ledger, now=NOW, resolver=trusted)
    assert first == second
    assert len(scheduler.list_schedules(ledger_path=ledger)) == 1
    with pytest.raises(ValueError, match="schedule_identity_conflict"):
        scheduler.create(REF, (NOW + timedelta(hours=2)).isoformat(),
                         request_at=request_at, ledger_path=ledger, now=NOW,
                         resolver=trusted)


def test_cancel_claim_race_fails_closed(ledger):
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    def submit_during_claim(ref, at, **kwargs):
        # Submission keeps a SQLite write transaction open. A competing
        # cancellation cannot take its own writer lock until admission exits.
        db = sqlite3.connect(ledger, timeout=0.1, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                db.execute("BEGIN IMMEDIATE")
        finally:
            db.close()
        return {"state": "busy"}
    result = scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2),
                            resolver=trusted, submitter=submit_during_claim)
    assert result["results"][0]["state"] == "scheduled"
    assert result["results"][0]["receipt"]["code"] == "busy_deferred"
    assert scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2, minutes=1),
                          resolver=trusted,
                          submitter=lambda *_a, **_k: pytest.fail("premature retry"))["processed"] == 0


def test_mcp_rejects_unrecognized_schedule_args():
    from gway_agent_ops.mcp_stdio import call_tool
    with pytest.raises(ValueError, match="invalid_arguments"):
        call_tool("schedule", {"ref": REF, "run_at": DUE, "command": "rm -rf /"})


def test_read_only_terminal_observation(ledger):
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2),
                   resolver=trusted, submitter=lambda *_a, **_k: {"state": "requested"})
    result = scheduler.inspect(record["schedule_id"], ledger_path=ledger,
                               observer=lambda _ref, _at: {"state": "finished"})
    assert result["state"] == "requested"  # durable request state retained
    assert result["execution_state"] == "finished"
    assert result["execution_terminal"] is True
    assert scheduler.get(record["schedule_id"], ledger_path=ledger)["state"] == "requested"


def test_competing_ticks_claim_once(ledger):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock
    scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    calls = []
    lock = Lock()
    def submit(ref, at, **_):
        with lock:
            calls.append((ref, at))
        return {"state": "requested"}
    def run(_):
        return scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2),
                              resolver=trusted, submitter=submit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sum(r["processed"] for r in results) == 1
    assert len(calls) == 1


def test_unapproved_schedule_is_rejected(tmp_path):
    path = tmp_path / "private" / "tasks.sqlite3"
    with TaskLedger(path) as entry:
        entry.discover(REF)
    with pytest.raises(ValueError, match="schedule_approval_required"):
        scheduler.create(REF, DUE, ledger_path=path, now=NOW, resolver=trusted)


def test_approval_revocation_before_due(ledger):
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    with TaskLedger(ledger) as entry:
        entry.decide(REF, "veto_approved", actor="operator", reason="revoked")
    result = scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2),
                            resolver=trusted, submitter=lambda *_a, **_k:
                            pytest.fail("revoked approval dispatched"))
    assert result["results"][0]["state"] == "blocked"
    assert scheduler.get(record["schedule_id"], ledger_path=ledger)["state"] == "blocked"


def test_approval_replaced_never_reuses_old_schedule(ledger):
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    with TaskLedger(ledger) as entry:
        entry.decide(REF, "veto_approved", actor="operator", reason="revoked")
        entry.decide(REF, "lift_veto", actor="operator")
        entry.decide(REF, "approve", actor="operator")
    result = scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2),
                            resolver=trusted, submitter=lambda *_a, **_k:
                            pytest.fail("old authorization reused"))
    assert result["results"][0]["state"] == "blocked"
    assert scheduler.get(record["schedule_id"], ledger_path=ledger)["state"] == "blocked"


def test_ledger_environment_is_canonical(tmp_path, monkeypatch):
    path = tmp_path / "custom" / "tasks.sqlite3"
    monkeypatch.setenv("GWAY_AGENT_LEDGER_PATH", str(path))
    assert scheduler.ledger_path() == path


def test_pure_read_does_not_create_ledger(tmp_path):
    path = tmp_path / "not_created" / "tasks.sqlite3"
    with pytest.raises(OSError):
        scheduler.list_schedules(ledger_path=path)
    assert not path.exists()
    assert not path.parent.exists()


def test_private_db_symlink_rejected(ledger, tmp_path):
    link = ledger.parent / "link.sqlite"
    link.symlink_to(ledger)
    with pytest.raises((OSError, PermissionError, ValueError)):
        scheduler.list_schedules(ledger_path=link)


def test_busy_retry_happens_only_after_cooldown(ledger):
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    calls = []
    def busy(*args, **kwargs):
        calls.append(1)
        return {"state": "busy"}
    first = scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=1),
                           resolver=trusted, submitter=busy)
    assert first["results"][0]["receipt"]["code"] == "busy_deferred"
    assert scheduler.tick(ledger_path=ledger,
                          now=NOW + timedelta(hours=1, minutes=1),
                          resolver=trusted, submitter=busy)["processed"] == 0
    assert len(calls) == 1
    later = scheduler.tick(ledger_path=ledger,
                           now=NOW + timedelta(hours=1, minutes=3),
                           resolver=trusted, submitter=busy)
    assert later["processed"] == 1
    assert len(calls) == 2


def test_late_duplicate_delivery_does_not_create_second_schedule(ledger):
    original = scheduler.create(REF, DUE, request_at=NOW.isoformat(),
                                ledger_path=ledger, now=NOW, resolver=trusted)
    delivered_late = scheduler.create(REF, DUE, request_at=NOW.isoformat(),
                                     ledger_path=ledger,
                                     now=NOW + timedelta(days=2), resolver=trusted)
    assert original == delivered_late


def test_approval_recheck_after_issue_lookup(ledger):
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    def revoke_on_lookup(ref):
        with TaskLedger(ledger) as entry:
            entry.decide(REF, "veto_approved", actor="operator", reason="cancelled")
        return trusted(ref)
    outcome = scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2),
                             resolver=revoke_on_lookup,
                             submitter=lambda *_a, **_k:
                             pytest.fail("revoked during preflight"))
    assert outcome["results"][0]["state"] == "blocked"


def test_prior_unrelated_request_not_mistaken_for_our_run(ledger, monkeypatch):
    from gway_agent_ops import observation
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    monkeypatch.setattr(observation, "observe", lambda *_a, **_k: {
        "state": "requested", "source": "direct-cli", "trigger": "cli-submit"})
    outcome = scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2),
                             resolver=trusted,
                             submitter=lambda *_a, **_k: {"state": "already_requested"})
    assert outcome["results"][0]["state"] == "blocked"
    assert outcome["results"][0]["receipt"]["code"] == "identity_owned_by_other_submit"


def test_reject_symlink_on_open_before_sqlite(tmp_path):
    victim = tmp_path / "victim.sqlite"
    victim.write_text("untouched")
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    (path / "tasks.sqlite3").symlink_to(victim)
    with pytest.raises(PermissionError):
        with TaskLedger(path / "tasks.sqlite3"):
            pass
    assert victim.read_text() == "untouched"


def _process_submission(path, due_time, entered, release, results):
    """Child process holds real SQLite writer lock during fake submission."""
    from datetime import datetime
    def fake_submit(_ref, _at, **_kwargs):
        entered.set()
        if not release.wait(8):
            raise RuntimeError("test release timeout")
        return {"state": "requested"}
    try:
        outcome = scheduler.tick(
            ledger_path=path, now=due_time, resolver=trusted,
            submitter=fake_submit)
        results.put(outcome["results"][0]["state"])
    except BaseException as error:
        results.put("error:" + type(error).__name__)


def test_veto_linearizes_against_dispatch_across_processes(ledger):
    """A veto cannot *commit* between final approval check and submit."""
    import multiprocessing
    import sqlite3

    ctx = multiprocessing.get_context("fork")
    scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    entered, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    worker = ctx.Process(target=_process_submission, args=(
        ledger, NOW + timedelta(hours=2), entered, release, results))
    worker.start()
    try:
        assert entered.wait(8), "worker did not reach the submission boundary"
        # Simulate a separate veto writer with a short timeout. The concurrent
        # submit owns BEGIN IMMEDIATE across the full nonblocking launcher.
        db = sqlite3.connect(ledger, timeout=0.2, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                db.execute("BEGIN IMMEDIATE")
        finally:
            db.close()
        release.set()
        worker.join(12)
        assert worker.exitcode == 0
        assert results.get(timeout=2) == "requested"
        # The veto can happen *after* admission, never invisibly between
        # authorization validation and the submission linearization.
        with TaskLedger(ledger) as entry:
            entry.decide(REF, "veto_approved", actor="operator",
                         reason="post-admission veto")
    finally:
        release.set()
        if worker.is_alive():
            worker.terminate()
        worker.join(5)


def test_veto_committed_before_submission_fails_closed(ledger):
    scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    with TaskLedger(ledger) as entry:
        entry.decide(REF, "veto_approved", actor="operator", reason="before claim")
    result = scheduler.tick(
        ledger_path=ledger, now=NOW + timedelta(hours=2), resolver=trusted,
        submitter=lambda *_a, **_k: pytest.fail("revoked task was dispatched"))
    assert result["results"][0]["state"] == "blocked"


@pytest.mark.parametrize("execution_state,terminal", [("provisioning", False), ("preacceptance_failed", True)])
def test_workspace_provisioning_observation(ledger, execution_state, terminal):
    record = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2),
                   resolver=trusted, submitter=lambda *_a, **_k: {"state": "requested"})
    result = scheduler.inspect(record["schedule_id"], ledger_path=ledger,
                               observer=lambda *_a: {"state": execution_state})
    assert result["execution_state"] == execution_state
    assert result["execution_terminal"] is terminal


@pytest.mark.parametrize("budget", [{}, {"five_hour": {"available": False}}, None])
def test_unavailable_quota_persists_deferred_identity_and_recovers(ledger, budget):
    row = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    due = NOW + timedelta(hours=1)
    forbid = lambda *_a, **_k: pytest.fail("quota unavailable dispatched")
    first = scheduler.tick(ledger_path=ledger, now=due, resolver=trusted,
                           budget_reader=lambda: budget, submitter=forbid)
    assert first["results"][0]["receipt"] == {"code": "quota_deferred", "reason": "budget_unknown"}
    pending = scheduler.get(row["schedule_id"], ledger_path=ledger)
    assert pending["quota_attempts"] == 1
    assert pending["first_deferred_at"] and pending["last_attempt_at"]
    assert pending["dispatch_at"] == row["dispatch_at"]
    assert scheduler.tick(ledger_path=ledger, now=due + timedelta(minutes=4),
                          resolver=trusted, submitter=forbid)["processed"] == 0
    calls = []
    def submit(ref, at, **kwargs):
        calls.append((ref, at, kwargs))
        return {"state": "requested"}
    final = scheduler.tick(ledger_path=ledger, now=due + timedelta(minutes=5),
                           resolver=trusted, submitter=submit)
    assert final["results"][0]["state"] == "requested"
    assert calls == [(REF, row["dispatch_at"], {"source": "scheduled", "trigger": "scheduler-due"})]
    assert scheduler.get(row["schedule_id"], ledger_path=ledger)["next_attempt_at"] is None
    assert scheduler.tick(ledger_path=ledger, now=due + timedelta(minutes=10),
                          resolver=trusted, submitter=forbid)["processed"] == 0


def test_quota_deferral_expires_without_dispatch(ledger):
    row = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    due = NOW + timedelta(hours=1)
    forbid = lambda *_a, **_k: pytest.fail("expired schedule dispatched")
    first = scheduler.tick(ledger_path=ledger, now=due, resolver=trusted,
                           budget_reader=lambda: _budget(0), submitter=forbid)
    assert first["results"][0]["receipt"]["reason"] == "budget_exhausted"
    final = scheduler.tick(ledger_path=ledger, now=due + timedelta(hours=24),
                           resolver=trusted, submitter=forbid)
    assert final["results"][0]["receipt"]["code"] == "quota_retry_expired"
    assert scheduler.get(row["schedule_id"], ledger_path=ledger)["state"] == "blocked"


def test_revoked_deferred_schedule_never_launches(ledger):
    row = scheduler.create(REF, DUE, ledger_path=ledger, now=NOW, resolver=trusted)
    due = NOW + timedelta(hours=1)
    forbid = lambda *_a, **_k: pytest.fail("revoked schedule dispatched")
    scheduler.tick(ledger_path=ledger, now=due, resolver=trusted,
                   budget_reader=lambda: {}, submitter=forbid)
    with TaskLedger(ledger) as entry:
        entry.decide(REF, "veto_approved", actor="operator", reason="revoked")
    outcome = scheduler.tick(ledger_path=ledger, now=due + timedelta(minutes=5),
                             resolver=trusted, submitter=forbid)
    assert outcome["results"][0]["receipt"]["code"] == "schedule_approval_revoked"
