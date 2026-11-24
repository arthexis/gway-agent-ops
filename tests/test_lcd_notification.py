"""AgentOps notification identity and at-most-once terminal journal hook."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from gway_agent_ops.lcd_notification import payload, publish_terminal
from gway_agent_ops.reporting import enqueue


def record(status="codex_failed"):
    return {"issue": "arthexis/gway-agent-ops#107",
            "at": "2026-11-24T07:00:00+00:00", "status": status}


def test_failure_contains_executor_repository_and_issue():
    doc = payload(record(), node="gway-001")
    assert doc["origin_node"] == doc["target_node"] == "gway-001"
    assert doc["subject"] == {"type": "github_issue",
                              "repository": "arthexis/gway-agent-ops", "number": 107}
    assert doc["state"] == "codex_failed"
    assert doc["reason_code"] == "CODEX_FAILED"
    assert doc["severity"] == "error"
    assert len(doc["episode_id"]) == 32
    assert doc["episode_id"] != payload({**record(), "at": "second"},
                                        node="gway-001")["episode_id"]


def test_process_end_is_not_verified_work():
    doc = payload(record("finished"), node="gway-001")
    assert doc["reason_code"] == "PROCESS_ENDED"
    assert doc["severity"] == "important"


def test_noncanonical_identity_rejected():
    with pytest.raises(ValueError):
        payload({"issue": "not-an-issue", "at": "now", "status": "finished"},
                node="gway-001")
    with pytest.raises(ValueError):
        payload(record(), node="localhost;rm")


def test_one_payload_and_one_sound_not_replayed():
    commands = []
    def run(argv, **kw):
        commands.append(argv)
        return type("Result", (), {"returncode": 0})()
    assert publish_terminal(record(), node="gway-001", runner=run,
                            home=Path("/tmp/mock-home"))
    assert len(commands) == 2
    assert commands[0][:2] == ["/tmp/mock-home/.local/bin/lcd", "notice"]
    notice = json.loads(commands[0][2])
    assert notice["subject"]["number"] == 107
    assert commands[1] == ["/tmp/mock-home/.local/bin/sound", "warning"]


def test_unavailable_notifications_do_not_fail_job():
    def fail(*args, **kwargs):
        raise OSError("unavailable")
    assert publish_terminal(record(), node="gway-001", runner=fail) is False


def test_enqueue_notifies_once_only_after_durable_write(tmp_path, monkeypatch):
    from gway_agent_ops import lcd_notification
    events = []
    def fake_publish(document):
        assert document["issue"] == "arthexis/aeon-mud#53"
        assert document["status"] == "codex_failed"
        assert list((tmp_path / "outbox").glob("*.json"))
        events.append(document)
    monkeypatch.setenv("GWAY_AGENTOPS_LCD_NOTIFICATIONS", "1")
    monkeypatch.setattr(lcd_notification, "publish_terminal", fake_publish)
    from gway_agent_ops import reporting
    monkeypatch.setattr(reporting, "_private_directory",
                        lambda p: Path(p).mkdir(parents=True, exist_ok=True))
    for _ in range(2):
        enqueue("arthexis/aeon-mud#53", "2026-11-24T08:00:00+00:00",
                status="codex_failed", exit_code=1, state_dir=tmp_path)
    assert len(events) == 1
