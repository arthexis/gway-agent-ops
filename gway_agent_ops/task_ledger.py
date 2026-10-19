"""Durable discovery and operator authorization ledger; never invokes Codex."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .dispatch import ISSUE, _private_directory


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class TaskLedger:
    """Independent M1 ledger. Existing dispatch admission remains authoritative."""

    def __init__(self, path: Path):
        self.path = Path(path)
        _private_directory(self.path.parent)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                issue TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN
                    ('requested','approved','vetoed','withdrawn','running',
                     'interrupted','finished','failed')),
                priority INTEGER NOT NULL DEFAULT 2 CHECK(priority BETWEEN 0 AND 3),
                source TEXT NOT NULL DEFAULT 'github',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                at TEXT NOT NULL
            );
        """)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def get(self, issue):
        row = self.db.execute("SELECT * FROM tasks WHERE issue=?", (issue,)).fetchone()
        return dict(row) if row else None

    def _event(self, task_id, action, actor, detail=""):
        self.db.execute(
            "INSERT INTO events(task_id,action,actor,detail,at) VALUES(?,?,?,?,?)",
            (task_id, action, actor, detail, utc_now()))

    def discover(self, issue, *, priority=2):
        if not isinstance(issue, str) or not ISSUE.fullmatch(issue):
            raise ValueError("invalid issue")
        if type(priority) is not int or priority not in range(4):
            raise ValueError("invalid priority")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            stamp = utc_now()
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO tasks(issue,state,priority,created_at,updated_at) "
                "VALUES(?,'requested',?,?,?)", (issue, priority, stamp, stamp))
            task = self.get(issue)
            if cursor.rowcount:
                self._event(task["id"], "discovered", "github")
            self.db.execute("COMMIT")
            return task
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def decide(self, issue, action, *, actor, reason=""):
        if not isinstance(actor, str) or not actor.strip() or actor == "github":
            raise ValueError("explicit operator identity required")
        transitions = {
            "approve": ("requested", "approved"),
            "veto": ("requested", "vetoed"),
            "veto_approved": ("approved", "vetoed"),
            "lift_veto": ("vetoed", "requested"),
            "withdraw": ("requested", "withdrawn"),
            "withdraw_approved": ("approved", "withdrawn"),
        }
        if action not in transitions:
            raise ValueError("invalid action")
        if action.startswith("veto") and not reason.strip():
            raise ValueError("veto requires reason")
        expected, target = transitions[action]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            updated = self.db.execute(
                "UPDATE tasks SET state=?,updated_at=? WHERE issue=? AND state=?",
                (target, utc_now(), issue, expected))
            if updated.rowcount != 1:
                raise ValueError("task missing or invalid state transition")
            task = self.get(issue)
            self._event(task["id"], action, actor, reason)
            self.db.execute("COMMIT")
            return task
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def next_approved(self):
        row = self.db.execute(
            "SELECT * FROM tasks WHERE state='approved' "
            "ORDER BY priority,created_at,id LIMIT 1").fetchone()
        return dict(row) if row else None

    def recover(self):
        """Fail closed after interruption; never return running work to approved."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute("SELECT id FROM tasks WHERE state='running'").fetchall()
            for row in rows:
                self.db.execute("UPDATE tasks SET state='interrupted',updated_at=? WHERE id=?",
                                (utc_now(), row["id"]))
                self._event(row["id"], "interrupted", "recovery")
            self.db.execute("COMMIT")
            return len(rows)
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
