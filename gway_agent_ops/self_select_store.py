"""Durable schedule and one-at-a-time claims for self-selected agent work."""
from datetime import datetime, timedelta, timezone
import json

from .scheduler import ledger_path
from .task_ledger import TaskLedger
from .pickup_policy import _instant

TASK = "gway-001-self-select"
INTERVAL = timedelta(hours=4)
WINDOW = timedelta(minutes=30)


def stamp(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone_required")
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS self_select_schedule (
        id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('paused','active','cancelled')),
        next_due TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS self_select_claims (
        due_at TEXT PRIMARY KEY, claimed_at TEXT NOT NULL, state TEXT NOT NULL
        CHECK(state IN ('claimed','finished','failed')), result TEXT,
        target TEXT, dispatch_at TEXT)""")


def create(*, path=None, now=None):
    now = now or datetime.now(timezone.utc)
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        schema(db)
        db.execute("INSERT OR IGNORE INTO self_select_schedule VALUES (?,?,?,?)",
                   (TASK, "paused", stamp(now + INTERVAL), stamp(now)))
        return dict(db.execute("SELECT * FROM self_select_schedule WHERE id=?", (TASK,)).fetchone())


def transition(action, *, path=None, now=None):
    if action not in ("resume", "pause", "cancel"):
        raise ValueError("invalid_action")
    now = now or datetime.now(timezone.utc)
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM self_select_schedule WHERE id=?", (TASK,)).fetchone()
            if not row or row["state"] == "cancelled":
                raise ValueError("schedule_unavailable")
            due = stamp(now + INTERVAL) if action == "resume" else row["next_due"]
            db.execute("UPDATE self_select_schedule SET state=?,next_due=?,updated_at=? WHERE id=?",
                       ({"resume": "active", "pause": "paused", "cancel": "cancelled"}[action], due, stamp(now), TASK))
            result = dict(db.execute("SELECT * FROM self_select_schedule WHERE id=?", (TASK,)).fetchone())
            db.execute("COMMIT")
            return result
        except BaseException:
            db.execute("ROLLBACK")
            raise


def claim(*, path=None, now=None):
    """Atomically claim one due slot; interrupted claims never auto-replay."""
    now = now or datetime.now(timezone.utc)
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM self_select_schedule WHERE id=?", (TASK,)).fetchone()
            if not row or row["state"] != "active" or _instant(row["next_due"]) > now:
                db.execute("COMMIT")
                return None
            due = _instant(row["next_due"])
            steps = int((now - due) // INTERVAL) + 1
            current_due = due + (steps - 1) * INTERVAL
            db.execute("UPDATE self_select_schedule SET next_due=?,updated_at=? WHERE id=?",
                       (stamp(due + steps * INTERVAL), stamp(now), TASK))
            occupied = db.execute("SELECT 1 FROM self_select_claims WHERE state='claimed'").fetchone()
            if occupied or now - current_due > WINDOW:
                db.execute("COMMIT")
                return None
            db.execute("INSERT INTO self_select_claims VALUES (?,?,?,NULL,NULL,NULL)", (stamp(current_due), stamp(now), "claimed"))
            db.execute("COMMIT")
            return {"due_at": stamp(current_due), "claimed_at": stamp(now)}
        except BaseException:
            db.execute("ROLLBACK")
            raise


def finish(due_at, state, result, *, path=None):
    if state not in ("finished", "failed") or not isinstance(result, dict):
        raise ValueError("invalid_result")
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        schema(db)
        changed = db.execute("UPDATE self_select_claims SET state=?,result=? WHERE due_at=? AND state='claimed'",
                             (state, json.dumps(result, sort_keys=True), due_at))
        if changed.rowcount != 1:
            raise ValueError("claim_unavailable")


def bind(due_at, target, dispatch_at, *, path=None):
    """Freeze the only identity that may be submitted for a claimed slot."""
    from .dispatch import ISSUE, timestamp
    if not isinstance(target, str) or not ISSUE.fullmatch(target):
        raise ValueError("invalid_target")
    dispatch_at = timestamp(dispatch_at)
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            active = db.execute("SELECT state FROM self_select_schedule WHERE id=?", (TASK,)).fetchone()
            if not active or active["state"] != "active":
                raise ValueError("schedule_not_active")
            changed = db.execute("""UPDATE self_select_claims SET target=?,dispatch_at=?
                WHERE due_at=? AND state='claimed' AND target IS NULL AND dispatch_at IS NULL""",
                (target, dispatch_at, due_at))
            if changed.rowcount != 1:
                raise ValueError("claim_unavailable")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def authorized(target, dispatch_at, *, path=None):
    """The caller must additionally authenticate the request journal source."""
    snapshot = inspect(path=path)
    schedule = snapshot["schedule"]
    if not schedule or schedule["state"] != "active":
        return False
    return any(row["state"] == "claimed" and row["target"] == target
               and row["dispatch_at"] == dispatch_at for row in snapshot["claims"])


def note(due_at, result, *, path=None):
    if not isinstance(result, dict):
        raise ValueError("invalid_result")
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        schema(db)
        changed = db.execute("UPDATE self_select_claims SET result=? WHERE due_at=? AND state='claimed'",
                             (json.dumps(result, sort_keys=True), due_at))
        if changed.rowcount != 1:
            raise ValueError("claim_unavailable")


def inspect(*, path=None):
    """Read status without creating a ledger or changing its schema."""
    import os
    import sqlite3
    import stat
    from contextlib import closing
    from urllib.parse import quote

    target = ledger_path(path)
    try:
        parent, info = target.parent.lstat(), target.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077
                or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise PermissionError("unsafe ledger")
        with closing(sqlite3.connect("file:" + quote(str(target.absolute()), safe="/") + "?mode=ro",
                                     uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='self_select_schedule'").fetchone():
                return {"schedule": None, "claims": []}
            row = db.execute("SELECT * FROM self_select_schedule WHERE id=?", (TASK,)).fetchone()
            claims = db.execute("SELECT * FROM self_select_claims ORDER BY due_at DESC LIMIT 30").fetchall()
            return {"schedule": dict(row) if row else None, "claims": [dict(r) for r in claims]}
    except FileNotFoundError:
        return {"schedule": None, "claims": []}
