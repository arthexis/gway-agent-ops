"""Hermetic recurring ledger tests; never starts AgentOps or systemd."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3

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


def test_upgrade_from_task_ledger_without_one_shot_schema(tmp_path):
    legacy = tmp_path / "private" / "tasks.sqlite3"
    with TaskLedger(legacy):
        pass
    row = recurring_store.define(REF, "1h", path=legacy, now=NOW)
    assert row["state"] == "paused"
    with sqlite3.connect(legacy) as db:
        assert db.execute("SELECT count(*) FROM recurring_definitions").fetchone()[0] == 1
        assert db.execute("SELECT name FROM sqlite_master WHERE name='schedules'").fetchone() is None


def test_snapshot_read_does_not_mutate_existing_database(path):
    recurring_store.define(REF, "1h", path=path, now=NOW)
    with sqlite3.connect(path) as db:
        before = db.total_changes
        db.execute("PRAGMA query_only=ON")
        expected = db.execute("SELECT id,created_at FROM recurring_definitions").fetchall()
        assert db.total_changes == before
    snap = recurring_store.snapshot(path=path)
    assert [(row["id"], row["created_at"]) for row in snap["definitions"]] == expected
    assert recurring_store.snapshot(path=path) == snap


def test_schema_creation_rollback_on_failure(path, monkeypatch):
    original = recurring_store._schema

    def broken(db):
        original(db)
        raise RuntimeError("simulated migration crash")

    monkeypatch.setattr(recurring_store, "_schema", broken)
    with pytest.raises(RuntimeError, match="simulated migration crash"):
        recurring_store.define(REF, "1h", path=path, now=NOW)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='recurring_definitions'").fetchone() is None
    monkeypatch.setattr(recurring_store, "_schema", original)
    assert recurring_store.define(REF, "1h", path=path, now=NOW)["state"] == "paused"


def test_competing_issue_writers_and_reopen(path):
    policy = recurring_store.define(REF, "1h", path=path, now=NOW)
    def create(_):
        return recurring_store.record_issue(policy["id"], REF, NOW, path=path)["issue"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(create, range(12))) == {REF}
    assert len(recurring_store.snapshot(path=path)["issues"]) == 1


def test_v1_draft_schema_is_migrated_without_losing_paused_issues(path):
    """Pre-release stores are upgraded transactionally, not discarded."""
    with TaskLedger(path) as ledger:
        ledger.db.executescript("""
            CREATE TABLE recurring_definitions (
                id TEXT PRIMARY KEY, repository TEXT NOT NULL, issue TEXT,
                title_prefix TEXT, every_seconds INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('paused','cancelled')),
                created_at TEXT NOT NULL,
                CHECK ((issue IS NULL) != (title_prefix IS NULL)));
            CREATE TABLE recurring_issue_state (
                definition_id TEXT NOT NULL REFERENCES recurring_definitions(id),
                issue TEXT NOT NULL, next_due TEXT NOT NULL, last_invocation_at TEXT,
                last_receipt TEXT,
                state TEXT NOT NULL CHECK(state IN
                    ('pending','paused','completed','uncertain')),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(definition_id,issue));
        """)
        ledger.db.execute("""INSERT INTO recurring_definitions
            VALUES (?,?,?,?,?,?,?)""",
            ("a" * 64, "arthexis/ocpp-csms", REF, None, 3600,
             "paused", "2026-11-16T12:00:00.000000Z"))
        ledger.db.execute("""INSERT INTO recurring_issue_state
            VALUES(?,?,?,?,?,?,?)""", ("a" * 64, REF,
            "2026-11-16T13:00:00.000000Z", None, None,
            "paused", "2026-11-16T12:00:00.000000Z"))
    new = recurring_store.define(REF, "1h", path=path, now=NOW)
    assert new["state"] == "paused"
    snap = recurring_store.snapshot(path=path)
    assert any(row["id"] == "a" * 64 and row["state"] == "paused"
               for row in snap["definitions"])
    assert any(row["definition_id"] == "a" * 64 and row["state"] == "paused"
               for row in snap["issues"])
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert recurring_store.get(new["id"], path=path)["id"] == new["id"]
