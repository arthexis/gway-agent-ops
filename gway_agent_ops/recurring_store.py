"""Private, transactional recurring definitions and per-issue claim ledger.

Only operator-activated definitions can be evaluated. No Codex or GitHub I/O
takes place in this module. Reads never migrate or modify a ledger.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from .recurring_policy import RecurringPolicy
from .scheduler import ledger_path as default_ledger_path
from .task_ledger import TaskLedger

SCHEMA_VERSION = 2
DEFINITION_STATES = ("paused", "active", "completed", "cancelled")
ISSUE_STATES = ("paused", "pending", "claimed", "uncertain", "completed")


def _schema(db):
    """Non-destructive upgrade of the pre-release V1 Draft tables."""
    db.execute("""CREATE TABLE IF NOT EXISTS recurring_definitions (
        id TEXT PRIMARY KEY,
        repository TEXT NOT NULL,
        issue TEXT,
        title_prefix TEXT,
        title_regex TEXT,
        every_seconds INTEGER NOT NULL CHECK(every_seconds BETWEEN 900 AND 2592000),
        state TEXT NOT NULL DEFAULT 'paused' CHECK(state IN
            ('paused','active','completed','cancelled')),
        max_runs INTEGER NOT NULL DEFAULT 100 CHECK(max_runs BETWEEN 1 AND 1000),
        no_progress_limit INTEGER NOT NULL DEFAULT 3 CHECK(no_progress_limit BETWEEN 1 AND 20),
        last_evaluated_at TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK ((issue IS NOT NULL) + (title_prefix IS NOT NULL) +
               (title_regex IS NOT NULL) = 1)
    )""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(recurring_definitions)")}
    if "title_regex" not in columns:
        # The foundation shipped only in a Draft; preserve its pre-release data
        # and references if a test/experimental node created it independently.
        db.execute("PRAGMA legacy_alter_table=ON")
        db.execute("ALTER TABLE recurring_definitions RENAME TO recurring_definitions_v1")
        db.execute("""CREATE TABLE recurring_definitions (
            id TEXT PRIMARY KEY, repository TEXT NOT NULL, issue TEXT,
            title_prefix TEXT, title_regex TEXT,
            every_seconds INTEGER NOT NULL CHECK(every_seconds BETWEEN 900 AND 2592000),
            state TEXT NOT NULL DEFAULT 'paused' CHECK(state IN
                ('paused','active','completed','cancelled')),
            max_runs INTEGER NOT NULL DEFAULT 100 CHECK(max_runs BETWEEN 1 AND 1000),
            no_progress_limit INTEGER NOT NULL DEFAULT 3 CHECK(no_progress_limit BETWEEN 1 AND 20),
            last_evaluated_at TEXT, last_error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            CHECK ((issue IS NOT NULL) + (title_prefix IS NOT NULL) +
                   (title_regex IS NOT NULL) = 1)
        )""")
        db.execute("""INSERT INTO recurring_definitions
            (id,repository,issue,title_prefix,every_seconds,state,created_at,updated_at)
            SELECT id,repository,issue,title_prefix,every_seconds,state,created_at,created_at
            FROM recurring_definitions_v1""")
        db.execute("DROP TABLE recurring_definitions_v1")
    db.execute("""CREATE TABLE IF NOT EXISTS recurring_issue_state (
        definition_id TEXT NOT NULL REFERENCES recurring_definitions(id),
        issue TEXT NOT NULL, next_due TEXT NOT NULL, last_invocation_at TEXT,
        last_receipt TEXT, state TEXT NOT NULL CHECK(state IN
            ('paused','pending','claimed','uncertain','completed')),
        updated_at TEXT NOT NULL,
        approval_event_id INTEGER, run_count INTEGER NOT NULL DEFAULT 0,
        no_progress_count INTEGER NOT NULL DEFAULT 0,
        progress_fingerprint TEXT, claim_at TEXT, next_attempt_at TEXT,
        last_error TEXT, PRIMARY KEY(definition_id,issue)
    )""")
    cols = {row[1] for row in db.execute("PRAGMA table_info(recurring_issue_state)")}
    if "claim_at" not in cols:
        db.execute("PRAGMA legacy_alter_table=ON")
        db.execute("ALTER TABLE recurring_issue_state RENAME TO recurring_issue_state_v1")
        db.execute("""CREATE TABLE recurring_issue_state (
            definition_id TEXT NOT NULL REFERENCES recurring_definitions(id),
            issue TEXT NOT NULL, next_due TEXT NOT NULL, last_invocation_at TEXT,
            last_receipt TEXT, state TEXT NOT NULL CHECK(state IN
                ('paused','pending','claimed','uncertain','completed')),
            updated_at TEXT NOT NULL, approval_event_id INTEGER,
            run_count INTEGER NOT NULL DEFAULT 0,
            no_progress_count INTEGER NOT NULL DEFAULT 0,
            progress_fingerprint TEXT, claim_at TEXT, next_attempt_at TEXT,
            last_error TEXT, PRIMARY KEY(definition_id,issue)
        )""")
        db.execute("""INSERT INTO recurring_issue_state
            (definition_id,issue,next_due,last_invocation_at,last_receipt,state,updated_at)
            SELECT definition_id,issue,next_due,last_invocation_at,last_receipt,state,updated_at
            FROM recurring_issue_state_v1""")
        db.execute("DROP TABLE recurring_issue_state_v1")
    db.execute("""CREATE TABLE IF NOT EXISTS recurring_audit (
        id INTEGER PRIMARY KEY, definition_id TEXT NOT NULL,
        issue TEXT, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS recurring_issue_due
                  ON recurring_issue_state(state,next_due)""")


def _instant(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _clock(now=None):
    return _instant(now or datetime.now(timezone.utc))


def _id(policy):
    canonical = json.dumps([policy.repository, policy.issue, policy.title_prefix,
                            policy.title_regex, int(policy.every.total_seconds())],
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _audit(db, definition_id, issue, action, stamp, detail=""):
    db.execute("""INSERT INTO recurring_audit(definition_id,issue,action,detail,at)
                  VALUES(?,?,?,?,?)""", (definition_id, issue, action, detail, stamp))


def _open(path):
    return TaskLedger(path or default_ledger_path())


def define(target, every, *, title_prefix=None, title_regex=None, max_runs=100,
           no_progress_limit=3, path=None, now=None):
    """Idempotently create a PAUSED definition; no recurring execution yet."""
    policy = RecurringPolicy.parse(target, every, title_prefix=title_prefix,
                                   title_regex=title_regex)
    if type(max_runs) is not int or not 1 <= max_runs <= 1000:
        raise ValueError("invalid_max_runs")
    if type(no_progress_limit) is not int or not 1 <= no_progress_limit <= 20:
        raise ValueError("invalid_no_progress_limit")
    identity, stamp = _id(policy), _clock(now)
    with _open(path) as ledger:
        db = ledger.db
        db.execute("BEGIN IMMEDIATE")
        try:
            _schema(db)
            db.execute("""INSERT OR IGNORE INTO recurring_definitions
                (id,repository,issue,title_prefix,title_regex,every_seconds,
                 max_runs,no_progress_limit,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (identity, policy.repository, policy.issue, policy.title_prefix,
                 policy.title_regex, int(policy.every.total_seconds()),
                 max_runs, no_progress_limit, stamp, stamp))
            row = db.execute("SELECT * FROM recurring_definitions WHERE id=?", (identity,)).fetchone()
            if (row["repository"] != policy.repository or row["issue"] != policy.issue
                    or row["title_prefix"] != policy.title_prefix
                    or row["title_regex"] != policy.title_regex
                    or row["every_seconds"] != int(policy.every.total_seconds())
                    or row["max_runs"] != max_runs
                    or row["no_progress_limit"] != no_progress_limit):
                raise ValueError("recurring_identity_conflict")
            _audit(db, identity, None, "created", stamp) if db.execute(
                "SELECT COUNT(*) FROM recurring_audit WHERE definition_id=?", (identity,)
            ).fetchone()[0] == 0 else None
            db.execute("COMMIT")
            return dict(row)
        except BaseException:
            db.execute("ROLLBACK")
            raise


def record_issue(definition_id, issue, due, *, title=None, path=None, now=None):
    stamp, due_stamp = _clock(now), _instant(due)
    if type(definition_id) is not str or len(definition_id) != 64:
        raise ValueError("invalid_definition_id")
    with _open(path) as ledger:
        db = ledger.db
        db.execute("BEGIN IMMEDIATE")
        try:
            _schema(db)
            definition = db.execute("SELECT * FROM recurring_definitions WHERE id=?",
                                    (definition_id,)).fetchone()
            if definition is None:
                raise ValueError("definition_not_found")
            policy = RecurringPolicy(definition["repository"],
                timedelta(seconds=definition["every_seconds"]),
                definition["issue"], definition["title_prefix"], definition["title_regex"])
            if not policy.matches({"ref": issue, "state": "open", "title": title}):
                raise ValueError("issue_not_selected")
            state = "pending" if definition["state"] == "active" else "paused"
            db.execute("""INSERT OR IGNORE INTO recurring_issue_state
                (definition_id,issue,next_due,state,updated_at)
                VALUES(?,?,?,?,?)""", (definition_id, issue, due_stamp, state, stamp))
            row = db.execute("""SELECT * FROM recurring_issue_state
                WHERE definition_id=? AND issue=?""", (definition_id, issue)).fetchone()
            db.execute("COMMIT")
            return dict(row)
        except BaseException:
            db.execute("ROLLBACK")
            raise


def transition(definition_id, action, *, path=None, now=None):
    if action not in ("pause", "resume", "cancel"):
        raise ValueError("invalid_recurring_action")
    stamp = _clock(now)
    with _open(path) as ledger:
        db = ledger.db
        db.execute("BEGIN IMMEDIATE")
        try:
            _schema(db)
            row = db.execute("SELECT * FROM recurring_definitions WHERE id=?",
                             (definition_id,)).fetchone()
            if row is None:
                raise ValueError("definition_not_found")
            if row["state"] == "cancelled":
                raise ValueError("recurring_cancelled")
            new_state = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]
            db.execute("""UPDATE recurring_definitions SET state=?,updated_at=?
                          WHERE id=?""", (new_state, stamp, definition_id))
            if action == "resume":
                db.execute("""UPDATE recurring_issue_state SET state='pending',updated_at=?
                    WHERE definition_id=? AND state='paused'""", (stamp, definition_id))
            else:
                db.execute("""UPDATE recurring_issue_state SET state='paused',updated_at=?
                    WHERE definition_id=? AND state='pending'""", (stamp, definition_id))
            _audit(db, definition_id, None, action, stamp)
            db.execute("COMMIT")
            return dict(db.execute("SELECT * FROM recurring_definitions WHERE id=?",
                                   (definition_id,)).fetchone())
        except BaseException:
            db.execute("ROLLBACK")
            raise


@contextmanager
def _readonly(path):
    """Read the private recurring tables without requiring a one-shot schedule."""
    path = Path(path)
    parent, info = path.parent.lstat(), path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077 or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077
            or info.st_nlink != 1):
        raise PermissionError("unsafe recurring ledger")
    uri = "file:" + quote(str(path.absolute()), safe="/") + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def snapshot(*, path):
    """Pure existing-ledger read, never migration/WAL mutation."""
    with _readonly(path) as db:
        if not db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
                             AND name='recurring_definitions'""").fetchone():
            return {"definitions": [], "issues": [], "audit": []}
        for table in ("recurring_issue_state", "recurring_audit"):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                              (table,)).fetchone():
                raise ValueError("recurring_schema_incomplete")
        return {
            "definitions": [dict(row) for row in db.execute(
                "SELECT * FROM recurring_definitions ORDER BY id")],
            "issues": [dict(row) for row in db.execute(
                "SELECT * FROM recurring_issue_state ORDER BY definition_id,issue")],
            "audit": [dict(row) for row in db.execute(
                "SELECT * FROM recurring_audit ORDER BY id")]
        }


def get(definition_id, *, path=None):
    if type(definition_id) is not str or len(definition_id) != 64 or any(
            char not in "0123456789abcdef" for char in definition_id):
        raise ValueError("invalid_definition_id")
    data = snapshot(path=path or default_ledger_path())
    for row in data["definitions"]:
        if row["id"] == definition_id:
            return {**row, "issues": [x for x in data["issues"] if x["definition_id"] == definition_id]}
    raise ValueError("definition_not_found")
