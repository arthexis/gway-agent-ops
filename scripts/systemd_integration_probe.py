"""Isolated systemd user-manager smoke probe. Never invokes Codex or GitHub."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

def run(*argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)

def verify():
    if os.geteuid() == 0:
        raise RuntimeError("run_as_unprivileged_user")
    if run("systemctl", "--user", "show-environment").returncode:
        raise RuntimeError("systemd_user_manager_unavailable")
    unit = "gway-agent-probe-" + uuid.uuid4().hex[:16] + ".service"
    with tempfile.TemporaryDirectory(prefix="gway-agent-probe-") as directory:
        marker = Path(directory) / "started"
        program = ("import pathlib,time; "
                   f"pathlib.Path({str(marker)!r}).write_text('started'); "
                   "time.sleep(90)")
        launched = run("systemd-run", "--user", "--quiet", "--collect",
                       "--unit=" + unit, "--", sys.executable, "-c", program)
        if launched.returncode:
            raise RuntimeError("launch_failed")
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            if not marker.exists():
                raise RuntimeError("service_never_started")
            active = run("systemctl", "--user", "is-active", unit)
            if active.returncode or active.stdout.strip() != "active":
                raise RuntimeError("service_not_active")
            stopped = run("systemctl", "--user", "stop", unit)
            if stopped.returncode:
                raise RuntimeError("stop_failed")
            inactive = run("systemctl", "--user", "is-active", unit)
            if inactive.returncode == 0:
                raise RuntimeError("still_active")
            return {"result": "passed", "checks": ["launch", "active", "stop", "inactive"]}
        finally:
            run("systemctl", "--user", "stop", unit)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = verify()
        code = 0
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        result = {"result": "failed", "reason": str(exc)}
        code = 1
    print(json.dumps(result) if args.json else result)
    return code

if __name__ == "__main__":
    raise SystemExit(main())
