"""Local durable automatic assignment claims in the existing task ledger.

Cross-node coordination and live authorization are the runtime caller's
responsibility. A lost claim is never silently retried.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import re

from .dispatch import ISSUE, timestamp
from .pickup_policy import NODE
from .task_ledger import TaskLedger
from .scheduler import ledger_path

GENERATION = re.compile(r"[1-9][0-9]*", re.ASCII)
TERMINAL = frozenset({"finished", "codex_failed", "interrupted", "released",
                      "preacceptance_failed"})


@contextmanager
def _transaction(path):
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        db.execute("PRAGMA synchronous=FULL")
        db.execute("""CREATE TABLE IF NOT EXISTS pickup_assignments (
            issue TEXT COLLATE NOCASE NOT NULL,
            node TEXT NOT NULL,
            generation TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN
                ('claimed','deferred','requested','uncertain','terminal','handled')),
            dispatch_at TEXT NOT NULL,
            reason TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(issue,generation)
        )""")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def _identity(ref, node, generation):
    if (not isinstance(ref, str) or not ISSUE.fullmatch(ref)
            or not isinstance(node, str) or not NODE.fullmatch(node)
            or not isinstance(generation, str) or not GENERATION.fullmatch(generation)):
        raise ValueError("invalid_pickup_identity")


def _stamp(now=None):
    return timestamp(now.isoformat() if now else None, now=now)


def _row(db, ref, generation):
    row = db.execute("SELECT * FROM pickup_assignments WHERE issue=? AND generation=?",
                     (ref, generation)).fetchone()
    return dict(row) if row else None


def claim(ref, node, generation, *, path=None, now=None):
    """Freeze exactly one local identity; only definitive deferrals may retry."""
    _identity(ref, node, generation)
    stamp = _stamp(now)
    with _transaction(path) as db:
        current = _row(db, ref, generation)
        if current is not None and (current["node"] != node or current["state"] != "deferred"):
            return None
        occupied = db.execute("""SELECT 1 FROM pickup_assignments WHERE issue=?
            AND generation<>? AND state IN ('claimed','requested','uncertain') LIMIT 1""",
            (ref, generation)).fetchone()
        if occupied:
            return None
        if current is None:
            db.execute("""INSERT INTO pickup_assignments
                (issue,node,generation,state,dispatch_at,updated_at)
                VALUES(?,?,?,'claimed',?,?)""", (ref,node,generation,stamp,stamp))
        else:
            db.execute("""UPDATE pickup_assignments SET state='claimed',reason=NULL,updated_at=?
                WHERE issue=? AND generation=? AND state='deferred'""",
                (stamp,ref,generation))
        return _row(db, ref, generation)


def record_submission(ref, node, generation, at, outcome, *, path=None, now=None):
    """Only a definitive no-submit result can rearm the same frozen identity."""
    _identity(ref, node, generation)
    states = {"busy": "deferred", "budget_deferred": "deferred",
              "on_hold": "deferred", "maintenance": "deferred",
              "workspace_unavailable": "deferred", "explicit_schedule": "deferred",
              "explicit_approval": "deferred", "requested": "requested",
              "already_handled": "handled"}
    target = states.get(outcome, "uncertain")
    reason = outcome if outcome in states else "launch_unconfirmed"
    with _transaction(path) as db:
        row = _row(db, ref, generation)
        if not row or row["node"] != node or row["dispatch_at"] != at or row["state"] != "claimed":
            raise ValueError("pickup_claim_mismatch")
        db.execute("""UPDATE pickup_assignments SET state=?,reason=?,updated_at=?
            WHERE issue=? AND generation=?""",
            (target,reason,_stamp(now),ref,generation))
        return _row(db, ref, generation)


def reconcile(ref, node, generation, at, receipt, *, path=None, now=None):
    """Reconcile only matching, independently observed terminal evidence."""
    _identity(ref, node, generation)
    if (not isinstance(receipt, dict) or receipt.get("issue") != ref
            or receipt.get("at") != at or receipt.get("state") not in TERMINAL):
        raise ValueError("pickup_receipt_unverified")
    with _transaction(path) as db:
        row = _row(db, ref, generation)
        if not row or row["node"] != node or row["dispatch_at"] != at or row["state"] not in ("claimed","requested","uncertain","terminal"):
            raise ValueError("pickup_claim_mismatch")
        db.execute("""UPDATE pickup_assignments SET state='terminal',reason=?,updated_at=?
            WHERE issue=? AND generation=?""",
            (receipt["state"],_stamp(now),ref,generation))
        return _row(db, ref, generation)


def defer(ref, node, generation, reason, *, path=None, now=None):
    """Record known non-launch deferral without consuming an invocation."""
    _identity(ref, node, generation)
    if reason not in ("budget_unknown", "budget_stale", "budget_exhausted",
                      "busy", "on_hold", "maintenance", "workspace_unavailable",
                      "explicit_schedule", "explicit_approval"):
        raise ValueError("invalid_deferral")
    stamp = _stamp(now)
    with _transaction(path) as db:
        current = _row(db, ref, generation)
        if current is None:
            db.execute("""INSERT INTO pickup_assignments
                (issue,node,generation,state,dispatch_at,reason,updated_at)
                VALUES(?,?,?,'deferred',?,?,?)""",
                (ref,node,generation,stamp,reason,stamp))
        elif current["node"] == node and current["state"] == "deferred":
            db.execute("""UPDATE pickup_assignments SET reason=?,updated_at=?
                WHERE issue=? AND generation=?""",(reason,stamp,ref,generation))
        return _row(db, ref, generation)


def snapshot(*, path=None):
    """Read-only local projection; missing schema means no pickup records."""
    import sqlite3
    from urllib.parse import quote
    import stat
    p = ledger_path(path)
    if not p.exists() and not p.is_symlink():
        return []
    info = p.lstat()
    parent = p.parent.lstat()
    import os
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077 or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError("unsafe_pickup_ledger")
    db = sqlite3.connect("file:" + quote(str(p.absolute()), safe="/") + "?mode=ro",
                         uri=True, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pickup_assignments'").fetchone():
            return []
        return [dict(row) for row in db.execute(
            "SELECT * FROM pickup_assignments ORDER BY updated_at,issue,generation")]
    finally:
        db.close()
