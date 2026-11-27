from datetime import datetime, timezone

from gway_agent_ops.pickup_preview import preview


def test_read_only_preview_uses_live_budget_and_verified_assignment():
    ref = "arthexis/gway-agent-ops#126"
    now = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)
    data = preview(
        node_reader=lambda: "gway-001", candidate_reader=lambda node: [ref],
        resolver=lambda ref: {"state": "open", "creator": "arthexis", "labels": ["gway-001"]},
        events_reader=lambda ref: [{"event": "labeled", "id": 12, "label": {"name": "gway-001"},
                                   "actor": {"login": "arthexis", "id": 780437}, "created_at": now.isoformat()}],
        budget_reader=lambda: None, now=now)
    assert data["mode"] == "preview_only"
    assert data["candidates"] == [{"issue": ref, "assignment": "eligible", "generation": "12"}]
    assert data["budget_decision"] == "budget_unknown"


def test_preview_no_candidates_avoids_quota_rpc():
    def budget():
        raise AssertionError("quota query should not be needed")
    assert preview(node_reader=lambda: "gway-001", candidate_reader=lambda node: [],
                   budget_reader=budget)["candidates"] == []
