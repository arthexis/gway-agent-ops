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
            return {"result": "passed", "launch": True, "stop": True,
                    "inactive": True, "isolated": True}
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
