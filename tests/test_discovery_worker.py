"""Dynamic, opt-in discovery does not use Remote deployment targets."""
from gway_agent_ops.discovery_worker import tick
from gway_agent_ops.task_ledger import TaskLedger

ISSUE = "arthexis/gway-agent-ops#42"
UNREGISTERED = "arthexis/ocpp-reports#211"


def valid(ref):
    return {"state": "open", "creator": "arthexis", "labels": ["local-agent"]}


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
