"""Hermetic AgentOps Activity Feed adapter tests."""
import subprocess
from unittest.mock import Mock

from gway_agent_ops.activity_feed import publish
from gway_agent_ops.reporting import enqueue

REF = "arthexis/gway-remote#705"
AT = "2026-11-24T10:00:00+00:00"


def test_disabled_no_process(monkeypatch):
    monkeypatch.setattr("gway_agent_ops.activity_feed.enabled", lambda: False)
    runner = Mock()
    assert not publish(REF, AT, "finished", runner=runner, node="gway-001")
    runner.assert_not_called()


def test_stable_id_bounded_cli_and_neutral_finish(monkeypatch):
    monkeypatch.setattr("gway_agent_ops.activity_feed.enabled", lambda: True)
    runner = Mock(return_value=Mock(returncode=0))
    assert publish(REF, AT, "finished", observed_at=AT, runner=runner, node="gway-001")
    first = runner.call_args.args[0]
    assert first[:3] == ["/usr/local/bin/gway-remote", "feed", "Codex process ended"]
    assert first[first.index("--severity")+1] == "info"
    assert runner.call_args.kwargs["timeout"] == 1.5
    assert publish(REF, AT, "finished", observed_at=AT, runner=runner, node="gway-001")
    assert first == runner.call_args.args[0]
    assert publish(REF, AT, "codex_failed", observed_at=AT, runner=runner, node="gway-001")
    second = runner.call_args.args[0]
    assert first[first.index("--id")+1] != second[second.index("--id")+1]


def test_terminal_after_journal_once_even_on_failure(tmp_path, monkeypatch):
    events = []
    def sink(ref, at, state):
        assert list((tmp_path / "outbox").glob("*.json"))
        events.append((ref, at, state))
        raise OSError("unavailable")
    monkeypatch.setattr("gway_agent_ops.activity_feed.publish", sink)
    for _ in range(2):
        result = enqueue(REF, AT, state_dir=tmp_path, status="codex_failed", exit_code=1)
        assert result["status"] == "codex_failed"
    assert len(events) == 1
