from datetime import datetime, timezone

from gway_agent_ops import self_select_store as store
from gway_agent_ops.self_select_overlap import check
from gway_agent_ops.task_ledger import TaskLedger


def test_existing_approved_or_vetoed_work_is_not_taken_over(tmp_path):
    path = tmp_path / "private" / "db"
    store.create(path=path, now=datetime(2026, 11, 27, tzinfo=timezone.utc))
    with TaskLedger(path) as ledger:
        ledger.db.execute("INSERT INTO tasks(issue,state,created_at,updated_at) VALUES (?,?,?,?)",
                          ("arthexis/example#9", "approved", "2026-11-27", "2026-11-27"))
    assert check("arthexis/example#9", path=path) == "explicit_task"
    assert check("arthexis/example#10", path=path) is None
