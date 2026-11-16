"""Hermetic one-shot scheduler regressions; never calls Codex, systemd or GitHub."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from gway_agent_ops import scheduler

REF = "arthexis/ocpp-csms#355"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
DUE = (NOW + timedelta(hours=1)).isoformat()


def trusted(_):
    return {"labels": ["gway-001"], "state": "open", "creator": "arthexis"}


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "private" / "tasks.sqlite3"


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
