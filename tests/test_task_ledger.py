import sqlite3

import pytest

from gway_agent_ops.task_ledger import TaskLedger


import json

SNAPSHOT = json.dumps({"schema": 1, "revision": "a" * 40, "targets": [
    {"name": "agent", "repository": "arthexis/gway-agent-ops"},
    {"name": "remote", "repository": "arthexis/gway-remote"}]})

def ready(ledger):
    ledger.refresh_repositories(SNAPSHOT)

ISSUE = "arthexis/gway-agent-ops#26"


def test_discovery_is_idempotent_and_never_approves(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ready(ledger)
        first = ledger.discover(ISSUE)
        assert first["state"] == "requested"
        assert ledger.discover(ISSUE)["id"] == first["id"]
        assert ledger.next_approved() is None
        assert ledger.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_operator_approval_and_priority(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ready(ledger)
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
        ready(ledger)
        ledger.discover(ISSUE)
        ledger.decide(ISSUE, "veto", actor="operator", reason="not ready")
        with pytest.raises(ValueError):
            ledger.decide(ISSUE, "approve", actor="operator")
    with TaskLedger(path) as ledger:
        ready(ledger)
        assert ledger.discover(ISSUE)["state"] == "vetoed"
        ledger.decide(ISSUE, "lift_veto", actor="operator")
        assert ledger.next_approved() is None
        ledger.decide(ISSUE, "approve", actor="operator")
        assert ledger.next_approved()["issue"] == ISSUE
        assert ledger.db.execute("SELECT count(*) FROM events WHERE action='veto'").fetchone()[0] == 1


def test_withdrawal_does_not_erase_history(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ready(ledger)
        ledger.discover(ISSUE)
        ledger.decide(ISSUE, "approve", actor="operator")
        ledger.decide(ISSUE, "withdraw_approved", actor="operator")
        assert ledger.discover(ISSUE)["state"] == "withdrawn"
        assert ledger.next_approved() is None


def test_recovery_never_requeues(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ready(ledger)
        ledger.discover(ISSUE)
        ledger.decide(ISSUE, "approve", actor="operator")
        ledger.db.execute("UPDATE tasks SET state='running' WHERE issue=?", (ISSUE,))
        assert ledger.recover() == 1
        assert ledger.recover() == 0
        assert ledger.get(ISSUE)["state"] == "interrupted"
        assert ledger.next_approved() is None


def test_reject_invalid_input(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ready(ledger)
        with pytest.raises(ValueError):
            ledger.discover("../invalid")
        with pytest.raises(ValueError):
            ledger.discover(ISSUE, priority=-1)
        ledger.discover(ISSUE)
        with pytest.raises(ValueError):
            ledger.decide(ISSUE, "veto", actor="operator", reason="")

def test_registry_fails_closed_and_preserves_veto(tmp_path):
    from gway_agent_ops.repository_registry import parse_snapshot
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        with pytest.raises(ValueError):
            ledger.discover(ISSUE)
        ready(ledger)
        ledger.discover(ISSUE)
        ledger.decide(ISSUE, "veto", actor="operator", reason="hold")
        with pytest.raises(ValueError):
            ledger.refresh_repositories('{"schema":1}')
        assert ledger.get(ISSUE)["state"] == "vetoed"
        assert ledger.registry_status()["healthy"] is False
        with pytest.raises(ValueError):
            ledger.discover("arthexis/gway-remote#2")
        ready(ledger)
        assert ledger.get(ISSUE)["state"] == "vetoed"


def test_removed_repo_blocks_approval(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ready(ledger)
        ledger.discover(ISSUE)
        ledger.refresh_repositories(json.dumps({"schema": 1, "revision": "b" * 40,
            "targets": [{"name": "remote", "repository": "arthexis/gway-remote"}]}))
        with pytest.raises(ValueError):
            ledger.decide(ISSUE, "approve", actor="operator")
        assert ledger.get(ISSUE)["state"] == "requested"


@pytest.mark.parametrize("targets", [[], [
    {"name": "a", "repository": "arthexis/a"},
    {"name": "b", "repository": "arthexis/a"}], [
    {"name": "a", "repository": "../unsafe"}]])
def test_invalid_registry_entries(targets):
    from gway_agent_ops.repository_registry import validate_snapshot
    with pytest.raises(ValueError):
        validate_snapshot({"schema": 1, "revision": "a" * 40, "targets": targets})

def test_discovery_requires_trusted_open_labeled_issue(tmp_path):
    from gway_agent_ops.github_issues import IssueRejected
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ready(ledger)
        def valid(issue, *, allowed_repositories):
            assert issue.split("#")[0] in allowed_repositories
            return {"state": "open", "creator": "arthexis", "labels": ["local-agent"]}
        assert ledger.discover_verified(ISSUE, resolver=valid)["state"] == "requested"
        def unlabeled(issue, *, allowed_repositories):
            return {"state": "open", "creator": "arthexis", "labels": []}
        with pytest.raises(IssueRejected):
            ledger.discover_verified("arthexis/gway-agent-ops#27", resolver=unlabeled)
        assert ledger.get("arthexis/gway-agent-ops#27") is None


def test_removed_repo_is_not_next_approved(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ready(ledger)
        ledger.discover(ISSUE)
        ledger.decide(ISSUE, "approve", actor="operator")
        ledger.refresh_repositories(json.dumps({"schema": 1, "revision": "b" * 40,
            "targets": [{"name": "remote", "repository": "arthexis/gway-remote"}]}))
        assert ledger.next_approved() is None
        assert ledger.get(ISSUE)["state"] == "approved"

def test_reconciliation_withdraws_only_on_verified_label_removal(tmp_path):
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ready(ledger)
        ledger.discover(ISSUE)
        ledger.decide(ISSUE, "approve", actor="operator")
        def removed(issue, *, allowed_repositories):
            return {"state": "open", "labels": []}
        assert ledger.reconcile_issue(ISSUE, resolver=removed)["state"] == "withdrawn"
        assert ledger.next_approved() is None
        assert ledger.db.execute(
            "SELECT count(*) FROM events WHERE action='withdraw_approved'").fetchone()[0] == 1


def test_reconciliation_network_failure_preserves_approval(tmp_path):
    from gway_agent_ops.github_issues import IssueRejected
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ready(ledger)
        ledger.discover(ISSUE)
        ledger.decide(ISSUE, "approve", actor="operator")
        def offline(issue, *, allowed_repositories):
            raise IssueRejected("github_unavailable")
        with pytest.raises(IssueRejected):
            ledger.reconcile_issue(ISSUE, resolver=offline)
        assert ledger.get(ISSUE)["state"] == "approved"


def test_reconciliation_closed_issue_and_veto_preservation(tmp_path):
    from gway_agent_ops.github_issues import IssueRejected
    with TaskLedger(tmp_path / "ledger.sqlite") as ledger:
        ready(ledger)
        ledger.discover(ISSUE)
        def closed(issue, *, allowed_repositories):
            raise IssueRejected("issue_not_open")
        assert ledger.reconcile_issue(ISSUE, resolver=closed)["state"] == "withdrawn"
        other = "arthexis/gway-remote#2"
        ledger.discover(other)
        ledger.decide(other, "veto", actor="operator", reason="hold")
        assert ledger.reconcile_issue(other, resolver=closed)["state"] == "vetoed"
