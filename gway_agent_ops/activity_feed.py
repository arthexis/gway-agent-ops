"""Independent best-effort Activity Feed hook; no network, sound or agent actions."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import socket
import subprocess

STATES = {"accepted": ("Invocation accepted", "info"),
          "finished": ("Codex process ended", "info"),
          "codex_failed": ("Codex process failed", "error"),
          "interrupted": ("Codex process interrupted", "warning"),
          "released": ("Invocation released", "warning")}


def enabled():
    if os.environ.get("GWAY_AGENTOPS_ACTIVITY_FEED") == "1":
        return True
    try:
        p = Path.home() / ".config/gway-agent-ops/activity-feed.enabled"
        info = p.lstat()
        return (not p.is_symlink() and p.is_file() and info.st_uid == os.geteuid()
                and not info.st_mode & 0o077 and info.st_size <= 16
                and p.read_text(encoding="ascii").strip() == "enabled")
    except (OSError, UnicodeError):
        return False


def publish(issue, at, state, *, observed_at=None, runner=subprocess.run, node=None):
    """Journal caller owns the transition. CLI only enqueues locally."""
    try:
        if not enabled():
            return False
        match = re.fullmatch(r"([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})#([1-9][0-9]{0,9})", issue)
        node = node or socket.gethostname().split(".")[0]
        if not match or not re.fullmatch(r"gway-[0-9]{3,}", node) or state not in STATES:
            return False
        from .dispatch import timestamp
        at = timestamp(at)
        repository, number = match.groups()
        title, severity = STATES[state]
        identity = hashlib.sha256(("agentops:" + node + ":" + issue + ":" + at + ":" + state).encode()).hexdigest()
        occurred = observed_at or datetime.now(timezone.utc).isoformat()
        result = runner(["/usr/local/bin/gway-remote", "feed", title, "--summary", issue,
                         "--id", identity, "--time", occurred, "--type", "agentops." + state,
                         "--category", "agentops", "--severity", severity, "--repo", repository,
                         "--node", node, "--issue", number, "--url", "https://github.com/" + repository + "/issues/" + number],
                        timeout=1.5, check=False, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except Exception:
        return False
