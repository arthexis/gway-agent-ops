from datetime import datetime, timedelta, timezone

import pytest

from gway_agent_ops import self_select_store as store

NOW = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)


def test_four_hour_claim_is_durable_and_unresolved_claim_blocks_overlap(tmp_path):
    path = tmp_path / "private" / "state.sqlite"
    assert store.create(path=path, now=NOW)["state"] == "paused"
    active = store.transition("resume", path=path, now=NOW)
    assert active["next_due"] == store.stamp(NOW + timedelta(hours=4))
    due = NOW + timedelta(hours=4)
    first = store.claim(path=path, now=due)
    assert first["due_at"] == store.stamp(due)
    assert store.claim(path=path, now=due) is None
    assert store.claim(path=path, now=due + timedelta(hours=4)) is None
    store.finish(first["due_at"], "finished", {"reason": "budget_unavailable"}, path=path)
    assert store.claim(path=path, now=due + timedelta(hours=8)) is not None


def test_late_slots_do_not_replay_and_cancel_is_terminal(tmp_path):
    path = tmp_path / "private" / "state.sqlite"
    store.create(path=path, now=NOW)
    store.transition("resume", path=path, now=NOW)
    assert store.claim(path=path, now=NOW + timedelta(hours=5)) is None
    assert store.claim(path=path, now=NOW + timedelta(hours=8)) is not None
    store.transition("cancel", path=path, now=NOW + timedelta(hours=8))
    with pytest.raises(ValueError):
        store.transition("resume", path=path, now=NOW + timedelta(hours=9))


def test_naive_times_fail_closed(tmp_path):
    with pytest.raises(ValueError):
        store.create(path=tmp_path / "private" / "db", now=datetime(2026, 1, 1))


def test_after_downtime_only_current_slot_is_claimed(tmp_path):
    path = tmp_path / "private" / "state.sqlite"
    store.create(path=path, now=NOW)
    store.transition("resume", path=path, now=NOW)
    current = NOW + timedelta(hours=12)
    row = store.claim(path=path, now=current + timedelta(minutes=5))
    assert row["due_at"] == store.stamp(current)
    assert len(store.inspect(path=path)["claims"]) == 1


def test_bound_identity_is_exclusive_and_revoked_when_paused(tmp_path):
    path = tmp_path / "private" / "state.sqlite"
    store.create(path=path, now=NOW)
    store.transition("resume", path=path, now=NOW)
    claim = store.claim(path=path, now=NOW + timedelta(hours=4))
    at = store.stamp(NOW + timedelta(hours=4))
    store.bind(claim["due_at"], "arthexis/example#9", at, path=path)
    assert store.authorized("arthexis/example#9", at, path=path)
    assert not store.authorized("arthexis/example#10", at, path=path)
    with pytest.raises(ValueError):
        store.bind(claim["due_at"], "arthexis/example#10", at, path=path)
    store.transition("pause", path=path, now=NOW + timedelta(hours=4))
    assert not store.authorized("arthexis/example#9", at, path=path)
