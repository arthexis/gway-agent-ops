from datetime import datetime, timedelta, timezone

import pytest

from gway_agent_ops.pickup_policy import assignment, budget_decision


NOW = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)


def issue(*labels):
    return {"state": "open", "creator": "arthexis", "creator_id": 780437, "labels": list(labels)}


def event(id=1, *, actor="arthexis", action="labeled", label="gway-001"):
    return {"id": id, "event": action, "actor": {"login": actor, "id": 780437 if actor == "arthexis" else 123},
            "label": {"name": label}, "created_at": NOW.isoformat()}


@pytest.mark.parametrize("labels,reason", [
    ([], "not_assigned"),
    (["local-agent"], "not_assigned"),
    (["gway-004"], "not_assigned"),
    (["gway-001", "gway-004"], "conflicting_assignment"),
    (["gway-001", "on-hold"], "on_hold"),
])
def test_node_labels(labels, reason):
    assert assignment(issue(*labels), "gway-001", [event()]) == (reason, None)


def test_trusted_assignment_generation_and_hold_removal():
    events = [event(), event(2, label="on-hold"),
              event(3, label="on-hold", action="unlabeled")]
    assert assignment(issue("gway-001"), "gway-001", events) == ("eligible", "1")
    assert assignment(issue("gway-001"), "gway-001",
                      events + [event(4, action="unlabeled"), event(5)]) == ("eligible", "5")


def test_latest_event_actor_and_uncertainty():
    assert assignment(issue("gway-001"), "gway-001", []) == ("unverified_assignment", None)
    assert assignment(issue("gway-001"), "gway-001",
                      [event(), event(2, actor="collaborator")]) == ("unauthorized_assignment", None)
    assert assignment(issue("gway-001"), "gway-001",
                      [event(), event(2, action="unlabeled")]) == ("unverified_assignment", None)
    assert assignment(issue("gway-001"), "gway-001", [event(), event()]) == ("unverified_assignment", None)


def test_untrusted_issue():
    data = issue("gway-001")
    data["creator"] = "other"
    assert assignment(data, "gway-001", [event()]) == ("untrusted_issue", None)


def budget(percent=50):
    return {window: {"remaining_percent": percent, "available": True,
                     "observed_at": NOW.isoformat(), "source": "codex_session_rate_limits",
                     "resets_at": (NOW + timedelta(hours=1)).isoformat()}
            for window in ("five_hour", "weekly")}


@pytest.mark.parametrize("window", ["five_hour", "weekly"])
def test_both_quota_windows_and_reset(window):
    data = budget()
    data[window]["remaining_percent"] = 0
    assert budget_decision(data, now=NOW) == "budget_exhausted"
    assert budget_decision(budget(), now=NOW) == "eligible"
    data[window]["resets_at"] = NOW.isoformat()
    assert budget_decision(data, now=NOW) == "budget_stale"


def test_stale_and_invalid_budget():
    assert budget_decision(budget(), now=NOW + timedelta(minutes=6)) == "budget_stale"
    assert budget_decision(None, now=NOW) == "budget_unknown"
    data = budget()
    data["weekly"]["remaining_percent"] = True
    assert budget_decision(data, now=NOW) == "budget_unknown"
