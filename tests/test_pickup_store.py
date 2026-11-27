from datetime import datetime, timedelta, timezone

import pytest

from gway_agent_ops.pickup_store import claim, record_submission, reconcile


REF = "arthexis/gway-agent-ops#126"
NODE = "gway-001"
NOW = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)


def path(tmp_path):
    return tmp_path / "private" / "tasks.sqlite3"


def test_claim_survives_restart_and_blocks_new_generation(tmp_path):
    p = path(tmp_path)
    first = claim(REF, NODE, "1", path=p, now=NOW)
    assert first["state"] == "claimed"
    assert claim(REF, NODE, "1", path=p, now=NOW) is None
    assert claim(REF.upper(), NODE, "2", path=p, now=NOW) is None
    assert claim(REF, "gway-004", "1", path=p, now=NOW) is None
    row = record_submission(REF, NODE, "1", first["dispatch_at"],
                            "requested", path=p, now=NOW)
    assert row["state"] == "requested"
    assert claim(REF, NODE, "2", path=p, now=NOW) is None


def test_definitive_busy_reuses_identity(tmp_path):
    p = path(tmp_path)
    first = claim(REF, NODE, "1", path=p, now=NOW)
    row = record_submission(REF, NODE, "1", first["dispatch_at"],
                            "busy", path=p, now=NOW)
    assert row["state"] == "deferred"
    second = claim(REF, NODE, "1", path=p, now=NOW + timedelta(minutes=1))
    assert second["dispatch_at"] == first["dispatch_at"]


def test_uncertain_cannot_replay_terminal_allows_new_assignment(tmp_path):
    p = path(tmp_path)
    first = claim(REF, NODE, "1", path=p, now=NOW)
    at = first["dispatch_at"]
    assert record_submission(REF, NODE, "1", at, "launch_unconfirmed",
                             path=p, now=NOW)["state"] == "uncertain"
    assert claim(REF, NODE, "1", path=p, now=NOW) is None
    with pytest.raises(ValueError, match="pickup_receipt_unverified"):
        reconcile(REF, NODE, "1", at, {"issue": REF, "at": "wrong", "state": "finished"},
                  path=p, now=NOW)
    row = reconcile(REF, NODE, "1", at, {"issue": REF, "at": at, "state": "finished"},
                    path=p, now=NOW)
    assert row["state"] == "terminal"
    assert claim(REF, NODE, "1", path=p, now=NOW) is None
    assert claim(REF, NODE, "2", path=p, now=NOW + timedelta(minutes=1))["state"] == "claimed"
