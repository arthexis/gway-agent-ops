"""Durable discovery and operator authorization ledger; never invokes Codex."""
from __future__ import annotations

import sqlite3
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from .dispatch import ISSUE, _private_directory
from .repository_registry import parse_snapshot


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class TaskLedger:
    """Independent M1 ledger. Existing dispatch admission remains authoritative."""

    def __init__(self, path: Path):
        self.path = Path(path)
        _private_directory(self.path.parent)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            self.db.close()
            raise PermissionError("ledger must be an owned regular file")
        os.chmod(self.path, 0o600)
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

        self.db.execute("""CREATE TABLE IF NOT EXISTS repository_registry (
            id INTEGER PRIMARY KEY CHECK(id=1),
            revision TEXT NOT NULL,
            refreshed_at TEXT NOT NULL,
            repositories TEXT NOT NULL,
            healthy INTEGER NOT NULL
        )""")

    def refresh_repositories(self, raw):
        """Apply validated Remote export, or fail closed on invalid refresh."""
        import json
        try:
            revision, repos = parse_snapshot(raw)
        except (ValueError, TypeError):
            self.invalidate_registry()
            raise ValueError("repository registry unavailable or invalid")
        self.db.execute("""INSERT INTO repository_registry VALUES(1,?,?,?,1)
            ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,
            refreshed_at=excluded.refreshed_at,repositories=excluded.repositories,healthy=1""",
            (revision, utc_now(), json.dumps(repos)))
        return repos

    def invalidate_registry(self):
        self.db.execute("UPDATE repository_registry SET healthy=0 WHERE id=1")

    def registry_status(self):
        import json
        row = self.db.execute("SELECT * FROM repository_registry WHERE id=1").fetchone()
        return {"healthy": bool(row["healthy"]) if row else False,
                "revision": row["revision"] if row else None,
                "refreshed_at": row["refreshed_at"] if row else None,
                "repositories": json.loads(row["repositories"]) if row else []}

    def require_eligible(self, issue):
        status = self.registry_status()
        if not status["healthy"] or issue.split("#", 1)[0] not in status["repositories"]:
            raise ValueError("repository not eligible or registry unavailable")

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
        self.require_eligible(issue)
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
        if action == "approve":
            self.require_eligible(issue)
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
