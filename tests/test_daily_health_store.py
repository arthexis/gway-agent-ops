from datetime import datetime, timedelta, timezone

import pytest

from gway_agent_ops import daily_health_store as store

UTC = timezone.utc
START = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


def test_durable_claim_and_overlap(tmp_path):
    path = tmp_path / 'private' / 'tasks.sqlite3'
    assert store.create(path=path, now=START)['state'] == 'paused'
    active = store.transition('resume', path=path, now=START)
    due = datetime.fromisoformat(active['next_due'].replace('Z', '+00:00'))
    assert store.claim(path=path, now=due)['local_date'] == '2026-01-01'
    assert store.claim(path=path, now=due) is None
    assert store.claim(path=path, now=due + timedelta(days=1)) is None
    assert store.create(path=path, now=due)['state'] == 'active'


def test_missed_slot_and_cancel(tmp_path):
    path = tmp_path / 'private' / 'tasks.sqlite3'
    store.create(path=path, now=START)
    active = store.transition('resume', path=path, now=START)
    due = datetime.fromisoformat(active['next_due'].replace('Z', '+00:00'))
    assert store.claim(path=path, now=due + timedelta(minutes=30)) is None
    assert store.claim(path=path, now=due + timedelta(days=1)) is not None
    store.transition('cancel', path=path, now=due + timedelta(days=1))
    with pytest.raises(ValueError, match='daily_health_cancelled'):
        store.transition('resume', path=path, now=due + timedelta(days=2))


def test_naive_time_rejected(tmp_path):
    with pytest.raises(ValueError, match='timezone_required'):
        store.create(path=tmp_path / 'private' / 'db', now=datetime(2026, 1, 1))


def test_healthy_runs_remain_active_across_days(tmp_path):
    path = tmp_path / 'private' / 'tasks.sqlite3'
    store.create(path=path, now=START)
    active = store.transition('resume', path=path, now=START)
    due = datetime.fromisoformat(active['next_due'].replace('Z', '+00:00'))
    for day in range(10):
        at = due + timedelta(days=day)
        claim = store.claim(path=path, now=at)
        assert claim is not None
        store.finish(claim['local_date'], 'finished', path=path, now=at)
    assert store.create(path=path, now=at)['state'] == 'active'
    with pytest.raises(ValueError, match='claim_not_found'):
        store.finish(claim['local_date'], 'finished', path=path, now=at)


def test_restart_skips_backlog_but_claims_current_slot(tmp_path):
    path = tmp_path / 'private' / 'tasks.sqlite3'
    store.create(path=path, now=START)
    active = store.transition('resume', path=path, now=START)
    due = datetime.fromisoformat(active['next_due'].replace('Z', '+00:00'))
    claim = store.claim(path=path, now=due + timedelta(days=3, minutes=5))
    assert claim['local_date'] == '2026-01-04'
