"""Contained systemd user-manager smoke test; no GitHub or Codex calls."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
import sys

from gway_agent_ops.dispatch import admission, Busy
from gway_agent_ops.admission_status import status
from gway_agent_ops.reporting import _key
from gway_agent_ops.dispatch import timestamp


def run(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def verify() -> dict:
    if os.geteuid() == 0:
        raise RuntimeError("run_as_unprivileged_user")
    if run("systemctl", "--user", "show-environment").returncode:
        raise RuntimeError("systemd_user_manager_unavailable")
    unit = "gway-agent-probe-" + uuid.uuid4().hex[:16]
    with tempfile.TemporaryDirectory(prefix="gway-agent-probe-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        sentinel = root / "started"
        # Isolated dummy process: never invokes the real agent or Codex.
        program = ("import pathlib,time; "
                   f"pathlib.Path({str(sentinel)!r}).write_text('started'); "
                   "time.sleep(90)")
        command = [
            "systemd-run", "--user", "--quiet", "--collect",
            "--unit=" + unit, "--", "python3", "-c", program,
        ]
        launched = run(*command)
        if launched.returncode:
            raise RuntimeError("launch_failed: " + launched.stderr.strip()[:200])
        try:
            deadline = time.monotonic() + 10
            while not sentinel.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            if not sentinel.exists():
                raise RuntimeError("service_never_started")
            active = run("systemctl", "--user", "is-active", unit + ".service")
            if active.returncode or active.stdout.strip() != "active":
                raise RuntimeError("service_not_active")
            stopped = run("systemctl", "--user", "stop", unit + ".service")
            if stopped.returncode:
                raise RuntimeError("stop_unconfirmed")
            inactive = run("systemctl", "--user", "is-active", unit + ".service")
            if inactive.returncode == 0 or inactive.stdout.strip() not in (
                "inactive", "failed", "unknown"
            ):
                raise RuntimeError("inactive_unconfirmed")
            controller = root / "controller"
            controller.mkdir(mode=0o700)
            with admission("arthexis/probe#1", "2026-10-01T00:00:00Z",
                           state_dir=controller):
                child_program = "\n".join([
                    "from pathlib import Path",
                    "from gway_agent_ops.dispatch import admission, Busy",
                    "try:",
                    " with admission('arthexis/probe#2', '2026-10-01T00:01:00Z', "
                    + "state_dir=Path(" + repr(str(controller)) + ")): pass",
                    "except Busy: raise SystemExit(42)",
                    "",
                ])
                contender = run(sys.executable, "-c", child_program)
                if contender.returncode != 42:
                    raise RuntimeError("cross_process_exclusion_failed")
            if status(state_dir=controller)["available"]:
                raise RuntimeError("durable_reservation_lost")
            # An accepted execution must be finished before the next admission.
            delivered = controller / "delivered"
            delivered.mkdir(mode=0o700)
            key = _key("arthexis/probe#1", timestamp("2026-10-01T00:00:00Z"))
            (delivered / (key + ".json")).write_text(
                json.dumps({"status": "finished", "exit_code": 0}),
                encoding="utf-8",
            )
            if not status(state_dir=controller)["available"]:
                raise RuntimeError("terminal_evidence_did_not_reopen_slot")
            with admission("arthexis/probe#2", "2026-10-01T00:01:00Z",
                           state_dir=controller):
                if status(state_dir=controller)["available"]:
                    raise RuntimeError("second_admission_not_reserved")
            return {"result": "passed", "launch": True, "stop": True,
                    "inactive": True, "cross_process_exclusion": True,
                    "durable_reservation": True, "terminal_reopens_slot": True,
                    "isolated": True}
        finally:
            # Always attempt cleanup, even when verification fails.
            run("systemctl", "--user", "stop", unit + ".service")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = verify()
        code = 0
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        result = {"result": "failed", "reason": str(exc)}
        code = 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(result)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
