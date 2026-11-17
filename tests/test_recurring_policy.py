"""Offline recurring policy contract tests: no GitHub, systemd or Codex."""
from datetime import datetime, timedelta, timezone

import pytest

from gway_agent_ops.recurring_policy import RecurringPolicy


def test_fixed_issue_and_selector():
    fixed = RecurringPolicy.parse("arthexis/ocpp-csms#346", "1h")
    assert fixed.issue == "arthexis/ocpp-csms#346"
    assert fixed.matches({"ref": fixed.issue, "state": "open"})
    assert not fixed.matches({"ref": fixed.issue, "state": "closed"})
    assert not fixed.matches({"ref": fixed.issue, "state": "open", "is_pull_request": True})
    selector = RecurringPolicy.parse("arthexis/ocpp-csms", "6h", title_prefix="[auto-fix]")
    assert selector.matches({"ref": "arthexis/ocpp-csms#900", "state": "open", "title": "[auto-fix] timeout"})
    assert not selector.matches({"ref": "arthexis/other#900", "state": "open", "title": "[auto-fix] timeout"})
    assert not selector.matches({"ref": "arthexis/ocpp-csms#900", "state": "open", "title": "unrelated"})


@pytest.mark.parametrize("target,every,prefix", [
    ("arthexis/ocpp-csms#0", "1h", None),
    ("arthexis/ocpp-csms", "1h", None),
    ("arthexis/ocpp-csms#346", "1h", "[auto-fix]"),
    ("arthexis/ocpp-csms", "1h", ""),
    ("arthexis/ocpp-csms", "1h", "bad\nselector"),
    ("arthexis/ocpp-csms#346", "0h", None),
    ("arthexis/ocpp-csms#346", "14m", None),
    ("arthexis/ocpp-csms#346", "31d", None),
    ("arthexis/ocpp-csms#346", "1hour", None),
])
def test_invalid_policy_rejected(target, every, prefix):
    with pytest.raises(ValueError):
        RecurringPolicy.parse(target, every, title_prefix=prefix)


def test_next_due_skips_missed_slots_without_backlog():
    policy = RecurringPolicy.parse("arthexis/ocpp-csms#346", "1h")
    first = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    assert policy.next_due(first, now=first) == first + timedelta(hours=1)
    assert policy.next_due(first, now=first + timedelta(hours=5, minutes=30)) == first + timedelta(hours=6)
    assert policy.next_due(first, now=first - timedelta(minutes=1)) == first
    with pytest.raises(ValueError):
        policy.next_due(first.replace(tzinfo=None), now=first)
