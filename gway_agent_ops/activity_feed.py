"""Independent best-effort Activity Feed hook; no network, sound or agent actions."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import socket
import stat
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


def allowed_repositories():
    """Read a bounded, owner-private exact allowlist; absent/invalid means deny."""
    path = Path.home() / ".config/gway-agent-ops/activity-feed.repositories"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1
                    or not 0 < info.st_size <= 8192):
                return frozenset()
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(8193)
        finally:
            os.close(fd)
        if len(raw) > 8192:
            return frozenset()
        repos = raw.decode("ascii").splitlines()
        if (not 1 <= len(repos) <= 32 or len(set(repos)) != len(repos)
                or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo)
                       for repo in repos)):
            return frozenset()
        return frozenset(repos)
    except (OSError, ValueError, UnicodeError):
        return frozenset()


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
        if repository not in allowed_repositories():
            return False
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
