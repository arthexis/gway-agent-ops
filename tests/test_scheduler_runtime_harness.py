"""Hermetic tests for the disposable scheduler runtime probe.

These tests do not invoke systemd, AgentOps submit or GitHub.
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE = Path(__file__).with_name("scheduler_runtime_harness.py")
spec = importlib.util.spec_from_file_location("scheduler_runtime_harness", MODULE)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def test_timeout_terminates_synthetic_process_group():
    assert harness.timeout_cleanup_probe()["process_group_gone"] is True


def test_rejects_unexpected_source_commit(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "gway_agent_ops").mkdir(parents=True)
    (checkout / "gway_agent_ops" / "scheduler.py").write_text("")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    with patch.object(harness.subprocess, "check_output", return_value="wrong-head"):
        with pytest.raises(RuntimeError, match="wrong_source_head"):
            harness.verify(checkout, private / "tasks.sqlite3")


def test_source_pin_and_fake_only_dispatch_are_explicit():
    assert harness.EXPECTED_HEAD == "4e7507240c3386270a466be7115926996be1835b"
    source = MODULE.read_text()
    assert "submitter=fake_submit" in source
    assert "os._exit(71)" in source
    assert "start_new_session=True" in source


def test_group_probe_distinguishes_zombies_from_live_descendants(tmp_path):
    # /proc stat encodes state and pgrp, even for children adopted by PID 1.
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, state, group in ((101, "Z", 700), (102, "X", 700),
                               (103, "S", 701)):
        task = proc / str(pid)
        task.mkdir()
        (task / "stat").write_text(
            f"{pid} (synthetic worker) {state} 1 {group} {group} 0 0\n")
    assert harness._group_has_live_members(700, proc) is False

    live = proc / "104"
    live.mkdir()
    (live / "stat").write_text(
        "104 (process with ) in its name) S 1 700 700 0 0\n")
    assert harness._group_has_live_members(700, proc) is True
