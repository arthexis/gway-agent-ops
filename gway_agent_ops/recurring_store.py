"""Dormant recurring definitions and per-issue state in the private task ledger.

No discovery, scheduler tick, GitHub request, or Codex dispatch is performed.
All mutations are serialized with SQLite BEGIN IMMEDIATE.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from .recurring_policy import RecurringPolicy
from .scheduler import ledger_path as default_ledger_path
from .task_ledger import TaskLedger

SCHEMA_VERSION = 1


def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS recurring_definitions (
        id TEXT PRIMARY KEY,
        repository TEXT NOT NULL,
        issue TEXT,
        title_prefix TEXT,
        every_seconds INTEGER NOT NULL CHECK(every_seconds BETWEEN 900 AND 2592000),
        state TEXT NOT NULL DEFAULT 'paused' CHECK(state IN ('paused','cancelled')),
        created_at TEXT NOT NULL,
        CHECK ((issue IS NULL) != (title_prefix IS NULL))
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS recurring_issue_state (
        definition_id TEXT NOT NULL REFERENCES recurring_definitions(id),
        issue TEXT NOT NULL,
        next_due TEXT NOT NULL,
        last_invocation_at TEXT,
        last_receipt TEXT,
        state TEXT NOT NULL CHECK(state IN ('pending','paused','completed','uncertain')),
        updated_at TEXT NOT NULL,
        PRIMARY KEY(definition_id,issue)
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS recurring_issue_due
                  ON recurring_issue_state(state,next_due)""")
    db.execute("PRAGMA user_version")  # Do not take ownership of shared ledger version.


def _instant(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _id(policy):
    canonical = json.dumps([policy.repository, policy.issue, policy.title_prefix,
                            int(policy.every.total_seconds())], separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def define(target, every, *, title_prefix=None, path=None, now=None):
    """Idempotently register a *paused* policy. Does not authorize execution."""
    policy = RecurringPolicy.parse(target, every, title_prefix=title_prefix)
    identity = _id(policy)
    stamp = _instant(now or datetime.now(timezone.utc))
    with TaskLedger(path or default_ledger_path()) as ledger:
        db = ledger.db
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("""INSERT OR IGNORE INTO recurring_definitions
                (id,repository,issue,title_prefix,every_seconds,created_at)
                VALUES(?,?,?,?,?,?)""",
                (identity, policy.repository, policy.issue, policy.title_prefix,
                 int(policy.every.total_seconds()), stamp))
            row = db.execute("SELECT * FROM recurring_definitions WHERE id=?", (identity,)).fetchone()
            if (row["repository"] != policy.repository or row["issue"] != policy.issue
                    or row["title_prefix"] != policy.title_prefix
                    or row["every_seconds"] != int(policy.every.total_seconds())):
                raise ValueError("recurring_identity_conflict")
            db.execute("COMMIT")
            return dict(row)
        except BaseException:
            db.execute("ROLLBACK")
            raise


def record_issue(definition_id, issue, due, *, title=None, path=None, now=None):
    """Store a discovered issue as paused; cannot create an executable claim."""
    stamp = _instant(now or datetime.now(timezone.utc))
    due_stamp = _instant(due)
    if type(definition_id) is not str or len(definition_id) != 64:
        raise ValueError("invalid_definition_id")
    with TaskLedger(path or default_ledger_path()) as ledger:
        db = ledger.db
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            definition = db.execute("SELECT * FROM recurring_definitions WHERE id=?",
                                    (definition_id,)).fetchone()
            if definition is None:
                raise ValueError("definition_not_found")
            policy = RecurringPolicy(
                definition["repository"],
                timedelta(seconds=definition["every_seconds"]),
                definition["issue"], definition["title_prefix"])
            if not policy.matches({"ref": issue, "state": "open",
                                   "title": title}):
                raise ValueError("issue_not_selected")
            db.execute("""INSERT OR IGNORE INTO recurring_issue_state
                (definition_id,issue,next_due,state,updated_at)
                VALUES(?,?,?,'paused',?)""", (definition_id, issue, due_stamp, stamp))
            row = db.execute("""SELECT * FROM recurring_issue_state
                WHERE definition_id=? AND issue=?""", (definition_id, issue)).fetchone()
            db.execute("COMMIT")
            return dict(row)
        except BaseException:
            db.execute("ROLLBACK")
            raise


def snapshot(*, path):
    """Read existing definitions without migration or any write."""
    from .scheduler import _read_db
    # _read_db checks ownership and existing one-shot schema without migrating.
    with _read_db(path) as db:
        exists = db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
                               AND name='recurring_definitions'""").fetchone()
        if not exists:
            return {"definitions": [], "issues": []}
        return {
            "definitions": [dict(row) for row in db.execute(
                "SELECT * FROM recurring_definitions ORDER BY id")],
            "issues": [dict(row) for row in db.execute(
                "SELECT * FROM recurring_issue_state ORDER BY definition_id,issue")]
        }
