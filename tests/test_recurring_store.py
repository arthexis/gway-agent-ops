"""Hermetic recurring ledger tests; never starts AgentOps or systemd."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from gway_agent_ops import recurring_store, scheduler
from gway_agent_ops.task_ledger import TaskLedger

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
REF = "arthexis/ocpp-csms#346"


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "private" / "tasks.sqlite3"
    with TaskLedger(p):
        pass
    # One-shot scheduler owns its existing schema; preserve it unmodified.
    with TaskLedger(p) as ledger:
        scheduler._ensure(ledger.db)
    return p


def test_migration_and_paused_default(path):
    first = recurring_store.define(REF, "1h", path=path, now=NOW)
    assert first["state"] == "paused"
    snap = recurring_store.snapshot(path=path)
    assert len(snap["definitions"]) == 1
    assert snap["issues"] == []
    assert recurring_store.define(REF, "1h", path=path, now=NOW) == first


def test_issue_state_is_idempotent_and_non_executable(path):
    policy = recurring_store.define(REF, "1h", path=path, now=NOW)
    due = NOW + timedelta(hours=1)
    first = recurring_store.record_issue(policy["id"], REF, due, path=path, now=NOW)
    assert first["state"] == "paused"
    again = recurring_store.record_issue(policy["id"], REF, due + timedelta(hours=2),
                                         path=path, now=NOW)
    assert first == again
    assert len(recurring_store.snapshot(path=path)["issues"]) == 1
    with pytest.raises(ValueError, match="issue_not_selected"):
        recurring_store.record_issue(policy["id"], "arthexis/ocpp-csms#347", due, path=path)


def test_selector_requires_matching_title(path):
    policy = recurring_store.define("arthexis/ocpp-csms", "6h",
                                    title_prefix="[auto-fix]", path=path, now=NOW)
    with pytest.raises(ValueError, match="issue_not_selected"):
        recurring_store.record_issue(policy["id"], REF, NOW, title="unrelated", path=path)
    row = recurring_store.record_issue(policy["id"], REF, NOW,
                                       title="[auto-fix] timeout", path=path)
    assert row["state"] == "paused"


def test_competing_writers_and_reopen(path):
    def create(_):
        return recurring_store.define(REF, "1h", path=path, now=NOW)["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(create, range(12)))
    assert len(set(ids)) == 1
    assert len(recurring_store.snapshot(path=path)["definitions"]) == 1
    # Fresh process-equivalent connection must observe the same durable state.
    assert recurring_store.define(REF, "1h", path=path, now=NOW)["id"] == ids[0]


def test_reject_naive_time_without_partial_state(path):
    with pytest.raises(ValueError, match="timezone_required"):
        recurring_store.define(REF, "1h", path=path, now=NOW.replace(tzinfo=None))
    assert recurring_store.snapshot(path=path)["definitions"] == []
