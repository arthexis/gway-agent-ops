from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_expected_sources_are_packaged():
    assert (ROOT / "scripts/gway/agent_alert.py").exists()
    assert (ROOT / "scripts/gway/operator-log.sh").exists()
    assert (ROOT / "bin/agent-alert").exists()


def test_shell_helpers_parse():
    for script in (ROOT / "bin/agent-alert", ROOT / "scripts/gway/operator-log.sh"):
        result = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_runtime_state_is_not_tracked():
    result = subprocess.run(["git", "ls-files"], cwd=ROOT, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    forbidden = {"requests", "responses", "outbox", "logs"}
    paths = [Path(line) for line in result.stdout.splitlines()]

    assert not any(forbidden.intersection(path.parts) for path in paths)
