"""Opt-in, crash-safe one-shot scheduler; never run by issue discovery.

Claim is committed before any external action. An interrupted claim cannot
be dispatched again automatically: an operator must reconcile it explicitly.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import stat
import sqlite3
from contextlib import contextmanager
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .dispatch import ISSUE, timestamp
from .task_ledger import TaskLedger

HORIZON = timedelta(days=90)
STATES = ("scheduled", "cancelled", "claimed", "blocked", "launch_unconfirmed",
          "requested", "finished", "failed", "interrupted")
DEFAULT_LEDGER = Path.home() / ".local/state/gway-agent-ops/tasks.sqlite3"
MAX_BUSY_RETRIES = 6
RETRY_DELAY = timedelta(minutes=2)


def ledger_path(path=None):
    return Path(path if path is not None else os.environ.get(
        "GWAY_AGENT_LEDGER_PATH", str(DEFAULT_LEDGER)))


def _approved(db, issue, *, expected=None):
    task = db.execute("SELECT id,state FROM tasks WHERE issue=?", (issue,)).fetchone()
    if task is None or task["state"] != "approved":
        raise ValueError("schedule_approval_required")
    row = db.execute("SELECT MAX(id) FROM events WHERE task_id=? AND action='approve'",
                     (task["id"],)).fetchone()
    if row is None or row[0] is None or (expected is not None and row[0] != expected):
        raise ValueError("schedule_approval_revoked")
    return row[0]


@contextmanager
def _read_db(path):
    """Pure read: do not create files, change perms, migrate or enable WAL."""
    path = Path(path)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise PermissionError("unsafe ledger directory")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise PermissionError("unsafe ledger file")
    uri = "file:" + quote(str(path.absolute()), safe="/") + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schedules'").fetchone() is None:
            raise ValueError("schedule_schema_missing")
        yield db
    finally:
        db.close()


def parse_run_at(value, *, now=None, existing=False):
    if type(value) is not str or len(value) > 64:
        raise ValueError("invalid_run_at")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        raise ValueError("invalid_run_at") from None
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("timezone_required")
    instant = instant.astimezone(timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("timezone_required")
    current = current.astimezone(timezone.utc)
    if not existing and not current < instant <= current + HORIZON:
        raise ValueError("run_at_out_of_range")
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _ensure(db):
    # Reuse the existing private tasks.sqlite3 DB and its ownership checks.
    db.execute("""CREATE TABLE IF NOT EXISTS schedules (
        schedule_id TEXT PRIMARY KEY,
        issue TEXT NOT NULL,
        run_at TEXT NOT NULL,
        dispatch_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        approval_event_id INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        state TEXT NOT NULL CHECK(state IN
            ('scheduled','cancelled','claimed','blocked','launch_unconfirmed',
             'requested','finished','failed','interrupted')),
        receipt TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(issue,dispatch_at)
    )""")
    # Compatible with early Draft databases; migrations run only in write paths.
    names = {item[1] for item in db.execute("PRAGMA table_info(schedules)")}
    for name, ddl in (("approval_event_id", "INTEGER NOT NULL DEFAULT 0"),
                      ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                      ("next_attempt_at", "TEXT")):
        if name not in names:
            db.execute("ALTER TABLE schedules ADD COLUMN " + name + " " + ddl)
    db.execute("CREATE INDEX IF NOT EXISTS schedules_due ON schedules(state,run_at)")


def _row(db, schedule_id):
    record = db.execute("SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)).fetchone()
    return dict(record) if record else None


def _validate_issue(ref, resolver=None):
    from .github_issues import resolve
    if type(ref) is not str or not ISSUE.fullmatch(ref):
        raise ValueError("invalid_issue_ref")
    # Routing labels are optional. Keep the existing trusted-author/open-issue
    # contract even for injected test resolvers; production resolve rejects PRs.
    data = (resolver or resolve)(ref)
    if (type(data) is not dict or data.get("state") != "open"
            or type(data.get("creator")) is not str
            or data["creator"].casefold() != "arthexis"):
        raise ValueError("untrusted_issue")
    return data


def create(ref, run_at, *, request_at=None, ledger_path=None, actor=None, now=None, resolver=None):
    _validate_issue(ref, resolver)
    current = now or datetime.now(timezone.utc)
    due = parse_run_at(run_at, now=current, existing=True)
    # Dispatch identity is frozen at scheduling time, never generated by tick.
    dispatch_at = timestamp(request_at or current.isoformat(), now=current)
    identity = hashlib.sha256((ref + "\n" + dispatch_at).encode()).hexdigest()
    principal = actor or getpass.getuser()
    if type(principal) is not str or not principal or len(principal) > 128:
        raise ValueError("invalid_actor")
    path = globals()["ledger_path"](ledger_path)
    with TaskLedger(path) as ledger:
        db = ledger.db
        _ensure(db)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        try:
            approval = _approved(db, ref)
            record = _row(db, identity)
            if record is None:
                # Only a fresh schedule must be in the future. Exact transport
                # retries may arrive after the original due time.
                parse_run_at(run_at, now=current)
                stamp = _utc_now()
                db.execute("""INSERT INTO schedules
                    (schedule_id,issue,run_at,dispatch_at,actor,approval_event_id,state,created_at,updated_at)
                    VALUES(?,?,?,?,?, ?,'scheduled',?,?)""",
                    (identity, ref, due, dispatch_at, principal, approval, stamp, stamp))
            else:
                if (record["issue"] != ref or record["run_at"] != due
                        or record["approval_event_id"] != approval):
                    raise ValueError("schedule_identity_conflict")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return _row(db, identity)


def list_schedules(*, ledger_path=None):
    with _read_db(globals()["ledger_path"](ledger_path)) as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM schedules ORDER BY run_at,schedule_id")]


def get(schedule_id, *, ledger_path=None):
    if type(schedule_id) is not str or len(schedule_id) != 64 or not all(
            char in "0123456789abcdef" for char in schedule_id):
        raise ValueError("invalid_schedule_id")
    with _read_db(globals()["ledger_path"](ledger_path)) as db:
        record = _row(db, schedule_id)
    if record is None:
        raise ValueError("schedule_not_found")
    return record



def inspect(schedule_id, *, ledger_path=None, observer=None):
    """Read-only projection of execution evidence without changing the ledger."""
    record = get(schedule_id, ledger_path=ledger_path)
    if record["state"] not in ("requested", "launch_unconfirmed", "claimed"):
        return record
    from .observation import observe
    try:
        result = (observer or observe)(record["issue"], record["dispatch_at"])
        state = result.get("state")
        # The actual dispatch journal remains authoritative for execution.
        if state in ("requested", "launch_unconfirmed", "accepted", "finished",
                     "codex_failed", "interrupted", "released", "unknown"):
            return {**record, "execution_state": state,
                    "execution_terminal": state in
                    ("finished", "codex_failed", "interrupted", "released")}
    except (ValueError, OSError, KeyError, TypeError):
        pass
    return {**record, "execution_state": "unavailable",
            "execution_terminal": False}


def cancel(schedule_id, *, ledger_path=None):
    get(schedule_id, ledger_path=ledger_path)
    with TaskLedger(globals()["ledger_path"](ledger_path)) as ledger:
        db = ledger.db
        _ensure(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            changed = db.execute("""UPDATE schedules SET state='cancelled',updated_at=?
                WHERE schedule_id=? AND state='scheduled'""",
                (_utc_now(), schedule_id))
            if changed.rowcount != 1:
                raise ValueError("schedule_not_cancellable")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return _row(db, schedule_id)


def tick(*, ledger_path=None, now=None, resolver=None, submitter=None, limit=10):
    """Explicit scheduler-only tick. Claim-before-I/O enforces at-most-once.

    Definitive busy outcomes defer within a bounded retry budget; ambiguous
    launches and interrupted claims never retry automatically.
    """
    from .nonblocking import submit
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid_limit")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("timezone_required")
    cutoff = now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    path = globals()["ledger_path"](ledger_path)
    outcomes = []
    for _ in range(limit):
        with TaskLedger(path) as ledger:
            db = ledger.db
            _ensure(db)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            try:
                candidate = db.execute("""SELECT * FROM schedules
                    WHERE state='scheduled' AND run_at<=?
                      AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                    ORDER BY run_at,schedule_id LIMIT 1""", (cutoff, cutoff)).fetchone()
                if candidate is None:
                    db.execute("COMMIT")
                    break
                record = dict(candidate)
                try:
                    _approved(db, record["issue"], expected=record["approval_event_id"])
                except ValueError:
                    db.execute("""UPDATE schedules SET state='blocked',receipt=?,updated_at=?
                        WHERE schedule_id=? AND state='scheduled'""",
                        (json.dumps({"code": "schedule_approval_revoked"}), _utc_now(),
                         record["schedule_id"]))
                    db.execute("COMMIT")
                    outcomes.append({"schedule_id": record["schedule_id"], "state": "blocked",
                                     "receipt": {"code": "schedule_approval_revoked"}})
                    continue
                db.execute("""UPDATE schedules SET state='claimed',updated_at=?
                    WHERE schedule_id=? AND state='scheduled'""",
                    (_utc_now(), record["schedule_id"]))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        # No I/O can occur before the durable claim. A crash leaves 'claimed'.
        state, receipt = "blocked", {"code": "preflight_unavailable"}
        try:
            _validate_issue(record["issue"], resolver)
            # The authorization linearization point is this SQLite writer
            # transaction. Hold BEGIN IMMEDIATE through dispatch submission:
            # any concurrent veto must commit before this lock (and therefore
            # be rejected here), or wait until the attempt has been admitted.
            # A read-only recheck followed by submit leaves a real TOCTOU gap.
            # Keep this transaction short: submit is the existing bounded
            # *nonblocking* launcher, not the lifetime of the Codex job.
            with TaskLedger(path) as authority:
                authority.db.execute("BEGIN IMMEDIATE")
                try:
                    _approved(authority.db, record["issue"],
                              expected=record["approval_event_id"])
                    result = (submitter or submit)(
                        record["issue"], record["dispatch_at"],
                        source="scheduled", trigger="scheduler-due")
                    authority.db.execute("COMMIT")
                except BaseException:
                    authority.db.execute("ROLLBACK")
                    raise
            raw = result.get("state")
            if raw == "requested":
                state = "requested"
            elif raw == "already_requested":
                # A matching issue/timestamp is not sufficient: an unrelated
                # manual submission may already own that dispatch identity.
                from .observation import observe
                prior = observe(record["issue"], record["dispatch_at"])
                if prior.get("source") == "scheduled" and prior.get("trigger") == "scheduler-due":
                    state = "requested"
                else:
                    state = "blocked"
                    raw = "identity_owned_by_other_submit"
            elif raw == "launch_unconfirmed":
                state = "launch_unconfirmed"
            else:
                state = "blocked"
            receipt = {"code": raw if raw in
                ("requested","already_requested","launch_unconfirmed","busy",
                 "identity_owned_by_other_submit") else "blocked"}
        except (ValueError, OSError, RuntimeError) as exc:
            # No exception text in receipts: it can contain private data.
            receipt = {"code": "preflight_unavailable"}
        with TaskLedger(path) as ledger:
            db = ledger.db
            _ensure(db)
            db.execute("BEGIN IMMEDIATE")
            try:
                # A definitive busy response means submit wrote no durable request
                # marker; it can be deferred without re-launch ambiguity.
                if receipt["code"] == "busy" and record["attempts"] < MAX_BUSY_RETRIES:
                    next_at = (now.astimezone(timezone.utc) + RETRY_DELAY).isoformat(
                        timespec="microseconds").replace("+00:00", "Z")
                    db.execute("""UPDATE schedules SET state='scheduled',attempts=attempts+1,
                        next_attempt_at=?,receipt=?,updated_at=? WHERE schedule_id=? AND state='claimed'""",
                        (next_at, json.dumps({"code": "busy_deferred"}), _utc_now(), record["schedule_id"]))
                    state, receipt = "scheduled", {"code": "busy_deferred"}
                else:
                    db.execute("""UPDATE schedules SET state=?,receipt=?,updated_at=?
                    WHERE schedule_id=? AND state='claimed'""",
                        (state, json.dumps(receipt), _utc_now(), record["schedule_id"]))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        outcomes.append({"schedule_id": record["schedule_id"], "state": state,
                         "receipt": receipt})
    return {"processed": len(outcomes), "results": outcomes}
