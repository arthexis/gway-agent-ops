"""Private durable state for the independent daily health schedule.

This store does not launch work. Claims are deliberately durable and never
replayed after an ambiguous interruption; an executor must reconcile them.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .recurring_policy import DailyCalendarPolicy
from .scheduler import ledger_path
from .task_ledger import TaskLedger

TASK_ID = "gway-001-daily-health"
POLICY = DailyCalendarPolicy(5, 0, "America/Monterrey")
ZONE = ZoneInfo(POLICY.timezone_name)
WINDOW = timedelta(minutes=30)


def _instant(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS daily_health_schedule (
        id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('paused','active','cancelled')),
        next_due TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS daily_health_claims (
        task_id TEXT NOT NULL, local_date TEXT NOT NULL, due_at TEXT NOT NULL,
        claimed_at TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('claimed','finished','failed')),
        PRIMARY KEY(task_id, local_date))""")
    db.execute("""CREATE TABLE IF NOT EXISTS daily_health_events (
        id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL)""")


def create(*, path=None, now=None):
    now = now or datetime.now(timezone.utc)
    stamp = _instant(now)
    due = _instant(POLICY.next_due(now=now))
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        _ensure(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            changed = db.execute("INSERT OR IGNORE INTO daily_health_schedule VALUES (?, 'paused', ?, ?, ?)",
                                 (TASK_ID, due, stamp, stamp))
            if changed.rowcount:
                db.execute("INSERT INTO daily_health_events(task_id,action,at) VALUES (?,?,?)", (TASK_ID, "created", stamp))
            row = dict(db.execute("SELECT * FROM daily_health_schedule WHERE id=?", (TASK_ID,)).fetchone())
            db.execute("COMMIT")
            return row
        except BaseException:
            db.execute("ROLLBACK")
            raise


def transition(action, *, path=None, now=None):
    if action not in ("pause", "resume", "cancel"):
        raise ValueError("invalid_action")
    now = now or datetime.now(timezone.utc)
    stamp = _instant(now)
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        _ensure(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM daily_health_schedule WHERE id=?", (TASK_ID,)).fetchone()
            if row is None:
                raise ValueError("daily_health_not_found")
            if row["state"] == "cancelled":
                raise ValueError("daily_health_cancelled")
            state = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]
            # Resuming does not replay slots that elapsed while paused.
            due = _instant(POLICY.next_due(now=now)) if action == "resume" else row["next_due"]
            db.execute("UPDATE daily_health_schedule SET state=?,next_due=?,updated_at=? WHERE id=?",
                       (state, due, stamp, TASK_ID))
            db.execute("INSERT INTO daily_health_events(task_id,action,at) VALUES (?,?,?)", (TASK_ID, action, stamp))
            result = dict(db.execute("SELECT * FROM daily_health_schedule WHERE id=?", (TASK_ID,)).fetchone())
            db.execute("COMMIT")
            return result
        except BaseException:
            db.execute("ROLLBACK")
            raise


def claim(*, path=None, now=None):
    """Claim a current slot atomically; skip late or overlapping slots.

    A claimed row remains occupied until explicitly reconciled. This function
    performs no admission or launch and is not wired to the scheduler tick.
    """
    now = now or datetime.now(timezone.utc)
    stamp = _instant(now)
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        _ensure(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM daily_health_schedule WHERE id=?", (TASK_ID,)).fetchone()
            if not row or row["state"] != "active" or row["next_due"] > stamp:
                db.execute("COMMIT")
                return None
            due = datetime.fromisoformat(row["next_due"].replace("Z", "+00:00"))
            # After downtime, consider only the current local slot, never a backlog.
            current_slot = POLICY.next_due(now=now - WINDOW)
            eligible = due <= current_slot <= now.astimezone(timezone.utc)
            next_due = _instant(POLICY.next_due(now=now))
            db.execute("UPDATE daily_health_schedule SET next_due=?,updated_at=? WHERE id=?", (next_due, stamp, TASK_ID))
            occupied = db.execute("SELECT 1 FROM daily_health_claims WHERE task_id=? AND state='claimed'", (TASK_ID,)).fetchone()
            if not eligible or occupied:
                db.execute("INSERT INTO daily_health_events(task_id,action,at) VALUES (?,?,?)", (TASK_ID, "skipped", stamp))
                db.execute("COMMIT")
                return None
            due_at = _instant(current_slot)
            local_date = current_slot.astimezone(ZONE).date().isoformat()
            changed = db.execute("INSERT OR IGNORE INTO daily_health_claims VALUES (?,?,?,?, 'claimed')", (TASK_ID, local_date, due_at, stamp))
            if changed.rowcount:
                db.execute("INSERT INTO daily_health_events(task_id,action,at) VALUES (?,?,?)", (TASK_ID, "claimed", stamp))
            db.execute("COMMIT")
            return {"task_id": TASK_ID, "local_date": local_date, "due_at": due_at} if changed.rowcount else None
        except BaseException:
            db.execute("ROLLBACK")
            raise


def finish(local_date, outcome, *, path=None, now=None):
    """Record a known terminal outcome; ambiguous launches must stay claimed."""
    if outcome not in ("finished", "failed"):
        raise ValueError("invalid_outcome")
    if type(local_date) is not str:
        raise ValueError("invalid_local_date")
    stamp = _instant(now or datetime.now(timezone.utc))
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        _ensure(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            changed = db.execute("UPDATE daily_health_claims SET state=? WHERE task_id=? AND local_date=? AND state='claimed'", (outcome, TASK_ID, local_date))
            if changed.rowcount != 1:
                raise ValueError("daily_health_claim_not_found")
            db.execute("INSERT INTO daily_health_events(task_id,action,at) VALUES (?,?,?)", (TASK_ID, outcome, stamp))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def inspect(*, path=None):
    """Read schedule without creating or migrating the ledger."""
    import sqlite3
    from contextlib import closing
    from urllib.parse import quote
    import os, stat
    target = ledger_path(path)
    try:
        parent, info = target.parent.lstat(), target.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise PermissionError("unsafe ledger directory")
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise PermissionError("unsafe ledger file")
        with closing(sqlite3.connect("file:"+quote(str(target.absolute()),safe="/")+"?mode=ro", uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='daily_health_schedule'").fetchone():
                return {"schedule": None, "claims": []}
            row = db.execute("SELECT * FROM daily_health_schedule WHERE id=?", (TASK_ID,)).fetchone()
            claims = db.execute("SELECT * FROM daily_health_claims WHERE task_id=? ORDER BY local_date DESC LIMIT 30", (TASK_ID,)).fetchall()
            return {"schedule": dict(row) if row else None, "claims": [dict(r) for r in claims]}
    except FileNotFoundError:
        return {"schedule": None, "claims": []}
    except ValueError as exc:
        if str(exc) != "schedule_schema_missing":
            raise
        return {"schedule": None, "claims": []}
