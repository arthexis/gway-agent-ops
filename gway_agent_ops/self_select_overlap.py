"""Avoid superseding explicitly tracked local work when choosing a target."""
from contextlib import closing
import sqlite3
from urllib.parse import quote

from .scheduler import ledger_path
from .self_select_store import inspect


def check(ref, *, path=None):
    # Apply the store's ownership/mode checks before opening the existing DB.
    inspect(path=path)
    target = ledger_path(path)
    with closing(sqlite3.connect("file:" + quote(str(target.absolute()), safe="/") + "?mode=ro",
                                 uri=True, timeout=5)) as db:
        task = db.execute("SELECT state FROM tasks WHERE issue=? COLLATE NOCASE", (ref,)).fetchone()
        if task and task[0] in ("approved", "vetoed", "running"):
            return "explicit_task"
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "schedules" in tables and db.execute("""SELECT 1 FROM schedules
                WHERE issue=? COLLATE NOCASE AND state NOT IN
                ('cancelled','finished','failed','interrupted','blocked') LIMIT 1""", (ref,)).fetchone():
            return "explicit_schedule"
        if "recurring_definitions" in tables and db.execute("""SELECT 1 FROM recurring_definitions
                WHERE state='active' AND repository=? COLLATE NOCASE
                AND (issue IS NULL OR issue=? COLLATE NOCASE) LIMIT 1""",
                (ref.split("#", 1)[0], ref)).fetchone():
            return "explicit_schedule"
    return None
