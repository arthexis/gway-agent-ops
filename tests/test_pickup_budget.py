from datetime import datetime, timezone

from gway_agent_ops.pickup_budget import parse_limits
from gway_agent_ops.pickup_policy import budget_decision


NOW = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)


def response():
    return {"rateLimits": {
        "primary": {"usedPercent": 100, "windowDurationMins": 300,
                    "resetsAt": NOW.timestamp() + 60},
        "secondary": {"usedPercent": 32, "windowDurationMins": 10080,
                      "resetsAt": NOW.timestamp() + 86400}}}


def test_live_budget_normalization_and_replenishment():
    data = response()
    limits = parse_limits(data, observed=NOW)
    assert limits["five_hour"]["remaining_percent"] == 0
    assert limits["weekly"]["remaining_percent"] == 68
    assert budget_decision(limits, now=NOW) == "budget_exhausted"
    data["rateLimits"]["primary"]["usedPercent"] = 0
    assert budget_decision(parse_limits(data, observed=NOW), now=NOW) == "eligible"


def test_wrong_window_and_missing_data_fail_closed():
    data = response()
    data["rateLimits"]["secondary"]["windowDurationMins"] = 60
    assert budget_decision(parse_limits(data, observed=NOW), now=NOW) == "budget_unknown"
    assert budget_decision(parse_limits({}, observed=NOW), now=NOW) == "budget_unknown"
