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
    monkeypatch.setattr("gway_agent_ops.activity_feed.allowed_repositories", lambda: {"arthexis/gway-remote"})
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


def test_exact_private_allowlist(tmp_path, monkeypatch):
    from gway_agent_ops.activity_feed import allowed_repositories
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    p = tmp_path / ".config/gway-agent-ops/activity-feed.repositories"
    p.parent.mkdir(parents=True)
    assert not allowed_repositories()
    p.write_text("arthexis/gway-remote\narthexis/gway-agent-ops\n")
    p.chmod(0o600)
    assert allowed_repositories() == {"arthexis/gway-remote", "arthexis/gway-agent-ops"}
    for invalid in ("arthexis/*\n", "arthexis/gway-remote\narthexis/gway-remote\n", "\n", "a/b\ninvalid\n", "x"*8193):
        p.write_text(invalid)
        assert not allowed_repositories()
    p.write_text("arthexis/gway-remote\n")
    p.chmod(0o644)
    assert not allowed_repositories()
    p.unlink()
    target = tmp_path / "target"
    target.write_text("arthexis/gway-remote\n")
    target.chmod(0o600)
    p.symlink_to(target)
    assert not allowed_repositories()


def test_denied_never_enqueues_even_with_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("GWAY_AGENTOPS_ACTIVITY_FEED", "1")
    runner = Mock(return_value=Mock(returncode=0))
    assert not publish(REF, AT, "finished", runner=runner, node="gway-001")
    p = tmp_path / ".config/gway-agent-ops/activity-feed.repositories"
    p.parent.mkdir(parents=True)
    p.write_text("arthexis/gway-agent-ops\narthexis/gway-remote\n")
    p.chmod(0o600)
    for ref in ("arthexis/ocpp-csms#1", "arthexis/GWay-remote#705", "other/gway-remote#705"):
        assert not publish(ref, AT, "finished", runner=runner, node="gway-001")
    runner.assert_not_called()
    assert publish(REF, AT, "finished", runner=runner, node="gway-001")
