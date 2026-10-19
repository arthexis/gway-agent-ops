import sqlite3

import pytest

from gway_agent_ops.task_ledger import TaskLedger


ISSUE = "arthexis/gway-agent-ops#26"


def test_discovery_is_idempotent_and_never_approves(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        first = ledger.discover(ISSUE)
        assert first["state"] == "requested"
        assert ledger.discover(ISSUE)["id"] == first["id"]
        assert ledger.next_approved() is None
        assert ledger.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_operator_approval_and_priority(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ledger.discover(ISSUE)
        ledger.discover("arthexis/gway-remote#1", priority=0)
        with pytest.raises(ValueError):
            ledger.decide(ISSUE, "approve", actor="github")
        ledger.decide(ISSUE, "approve", actor="operator")
        assert ledger.next_approved()["issue"] == ISSUE
        ledger.decide("arthexis/gway-remote#1", "approve", actor="operator")
        assert ledger.next_approved()["issue"] == "arthexis/gway-remote#1"


def test_veto_persists_and_requires_fresh_approval(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with TaskLedger(path) as ledger:
        ledger.discover(ISSUE)
        ledger.decide(ISSUE, "veto", actor="operator", reason="not ready")
        with pytest.raises(ValueError):
            ledger.decide(ISSUE, "approve", actor="operator")
    with TaskLedger(path) as ledger:
        assert ledger.discover(ISSUE)["state"] == "vetoed"
        ledger.decide(ISSUE, "lift_veto", actor="operator")
        assert ledger.next_approved() is None
        ledger.decide(ISSUE, "approve", actor="operator")
        assert ledger.next_approved()["issue"] == ISSUE
        assert ledger.db.execute("SELECT count(*) FROM events WHERE action='veto'").fetchone()[0] == 1


def test_withdrawal_does_not_erase_history(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ledger.discover(ISSUE)
        ledger.decide(ISSUE, "approve", actor="operator")
        ledger.decide(ISSUE, "withdraw_approved", actor="operator")
        assert ledger.discover(ISSUE)["state"] == "withdrawn"
        assert ledger.next_approved() is None


def test_recovery_never_requeues(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ledger.discover(ISSUE)
        ledger.decide(ISSUE, "approve", actor="operator")
        ledger.db.execute("UPDATE tasks SET state='running' WHERE issue=?", (ISSUE,))
        assert ledger.recover() == 1
        assert ledger.recover() == 0
        assert ledger.get(ISSUE)["state"] == "interrupted"
        assert ledger.next_approved() is None


def test_reject_invalid_input(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        with pytest.raises(ValueError):
            ledger.discover("../invalid")
        with pytest.raises(ValueError):
            ledger.discover(ISSUE, priority=-1)
        ledger.discover(ISSUE)
        with pytest.raises(ValueError):
            ledger.decide(ISSUE, "veto", actor="operator", reason="")
