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
        return {"labels": [], "state": "open", "creator": "arthexis"}
    result = scheduler.tick(ledger_path=ledger, now=NOW + timedelta(hours=2),
                             resolver=revoked,
                             submitter=lambda *_a, **_k: pytest.fail("dispatch forbidden"))
    assert result["results"][0]["state"] == "blocked"
    assert scheduler.get(record["schedule_id"], ledger_path=ledger)["state"] == "blocked"


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
        with pytest.raises(ValueError, match="schedule_not_cancellable"):
            scheduler.cancel(record["schedule_id"], ledger_path=ledger)
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
