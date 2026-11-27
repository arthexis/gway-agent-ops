"""Dynamic, opt-in discovery does not use Remote deployment targets."""
from gway_agent_ops.discovery_worker import tick
import pytest

@pytest.fixture(autouse=True)
def node_identity(monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "gway-001")
from gway_agent_ops.task_ledger import TaskLedger

ISSUE = "arthexis/gway-agent-ops#42"
UNREGISTERED = "arthexis/ocpp-reports#211"


def valid(ref):
    return {"state": "open", "creator": "arthexis", "labels": ["gway-001"]}


def test_tick_discovers_without_approval_or_registry(tmp_path):
    with TaskLedger(tmp_path / "tasks.sqlite") as ledger:
        for _ in range(2):
            report = tick(ledger, listing=lambda: [ISSUE, UNREGISTERED], resolver=valid)
            assert report["errors"] == []
        assert ledger.get(UNREGISTERED)["state"] == "requested"
        assert ledger.get(ISSUE)["state"] == "requested"
        assert ledger.next_approved() is None
        assert ledger.db.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_search_failure_preserves_veto(tmp_path):
    with TaskLedger(tmp_path / "tasks.sqlite") as ledger:
        tick(ledger, listing=lambda: [ISSUE], resolver=valid)
        ledger.decide(ISSUE, "veto", actor="operator", reason="hold")

        def offline():
            raise OSError("offline")

        result = tick(ledger, listing=offline, resolver=valid)
        assert result["errors"] == ["discovery_unavailable"]
        assert ledger.get(ISSUE)["state"] == "vetoed"


def test_listing_failure_preserves_existing_tasks(tmp_path):
    with TaskLedger(tmp_path / "tasks.sqlite") as ledger:
        ledger.discover(ISSUE)

        def offline():
            raise OSError("offline")

        report = tick(ledger, listing=offline, resolver=valid)
        assert report["errors"] == ["discovery_unavailable"]
        assert ledger.get(ISSUE)["state"] == "requested"


def test_search_failure_reconciles_independently(tmp_path):
    from gway_agent_ops.github_issues import IssueRejected
    with TaskLedger(tmp_path / "tasks.sqlite") as ledger:
        for ref in (ISSUE, UNREGISTERED):
            ledger.discover(ref)
            ledger.decide(ref, "approve", actor="operator")
        def offline():
            raise OSError("offline")
        def resolve(ref):
            if ref == UNREGISTERED:
                raise IssueRejected("github_unavailable")
            return {"state": "closed", "labels": []}
        result = tick(ledger, listing=offline, resolver=resolve)
        assert result["reconciled"] == 1
        assert result["errors"] == ["discovery_unavailable", f"reconcile_unavailable:{UNREGISTERED}"]
        assert ledger.get(ISSUE)["state"] == "withdrawn"
        assert ledger.get(UNREGISTERED)["state"] == "approved"


def test_reconciliation_only(tmp_path):
    from gway_agent_ops.discovery_worker import reconcile
    with TaskLedger(tmp_path / "tasks.sqlite") as ledger:
        ledger.discover(ISSUE)
        result = reconcile(ledger, resolver=lambda ref: {"state": "open", "labels": []})
        assert result == {"discovered": 0, "reconciled": 1, "errors": []}
        assert ledger.get(ISSUE)["state"] == "requested"
