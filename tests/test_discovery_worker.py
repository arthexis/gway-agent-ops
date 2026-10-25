"""M2.1 discovery tests: authorization and failures remain fail-closed."""
import json

from gway_agent_ops.discovery_worker import tick
from gway_agent_ops.task_ledger import TaskLedger

ISSUE = "arthexis/gway-agent-ops#42"
SNAPSHOT = json.dumps({"schema": 1, "revision": "a" * 40, "targets": [
    {"name": "agent", "repository": "arthexis/gway-agent-ops"}]})

def valid(ref, *, allowed_repositories):
    assert ref.split("#")[0] in allowed_repositories
    return {"state": "open", "creator": "arthexis", "labels": ["local-agent"]}

def test_tick_discovers_without_approval(tmp_path):
    with TaskLedger(tmp_path / "tasks.sqlite") as ledger:
        for _ in range(2):
            report = tick(ledger, snapshot=lambda: SNAPSHOT,
                          listing=lambda repo: [ISSUE], resolver=valid)
            assert report["errors"] == []
        assert ledger.get(ISSUE)["state"] == "requested"
        assert ledger.next_approved() is None
        assert ledger.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1

def test_registry_failure_preserves_veto_and_blocks_discovery(tmp_path):
    with TaskLedger(tmp_path / "tasks.sqlite") as ledger:
        tick(ledger, snapshot=lambda: SNAPSHOT,
             listing=lambda repo: [ISSUE], resolver=valid)
        ledger.decide(ISSUE, "veto", actor="operator", reason="hold")
        def offline():
            raise OSError("offline")
        result = tick(ledger, snapshot=offline, listing=lambda repo: [ISSUE],
                      resolver=valid)
        assert result["errors"] == ["registry_unavailable"]
        assert not ledger.registry_status()["healthy"]
        assert ledger.get(ISSUE)["state"] == "vetoed"

def test_listing_failure_preserves_existing_tasks(tmp_path):
    with TaskLedger(tmp_path / "tasks.sqlite") as ledger:
        ledger.refresh_repositories(SNAPSHOT)
        ledger.discover(ISSUE)
        def offline(repo):
            raise OSError("offline")
        report = tick(ledger, snapshot=lambda: SNAPSHOT,
                      listing=offline, resolver=valid)
        assert report["errors"] == ["listing_unavailable:arthexis/gway-agent-ops", "listing_unavailable:arthexis/gway-remote"]
        assert ledger.get(ISSUE)["state"] == "requested"
