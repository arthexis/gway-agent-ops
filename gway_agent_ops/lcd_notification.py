"""Best-effort AgentOps LCD/Sound transition from a durable terminal report.

The status is the Codex process result, NOT a claim that an external
administrative effect was verified. Never send report content/findings or
re-trigger Codex. Disabled unless operator explicitly enables this hook.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess

ISSUE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)\Z")
NODE = re.compile(r"gway-[0-9]{3,}\Z")
STATES = {"finished": ("important", "PROCESS_ENDED", "notice", 30),
          "codex_failed": ("error", "CODEX_FAILED", "warning", 120),
          "interrupted": ("error", "INTERRUPTED", "warning", 120)}


def payload(report, *, node=None):
    if type(report) is not dict:
        raise ValueError("invalid terminal report")
    ref, at, status = (report.get("issue"), report.get("at"), report.get("status"))
    match = ISSUE.fullmatch(ref) if type(ref) is str else None
    node = node or socket.gethostname().split(".", 1)[0]
    if (match is None or type(at) is not str or not at or len(at) > 64 or
            type(status) is not str or status not in STATES or
            type(node) is not str or NODE.fullmatch(node) is None):
        raise ValueError("unverified AgentOps identity")
    repo, number = match.groups()
    if int(number) > 999999999:
        raise ValueError("issue number exceeds LCD contract")
    severity, reason, _sound, expiry = STATES[status]
    episode = hashlib.sha256((ref + "\n" + at).encode()).hexdigest()[:32]
    return {
        "schema": "gway-notification/v1",
        "system": "agentops", "origin_node": node, "target_node": node,
        "subject": {"type": "github_issue", "repository": repo,
                    "number": int(number)},
        "state": status, "severity": severity,
        "reason_code": reason, "episode_id": episode, "expires_in": expiry,
    }


def publish_terminal(report, *, node=None, runner=subprocess.run, home=None):
    """One attempt only, after durable report commit. Never fails AgentOps."""
    try:
        notice = payload(report, node=node)
        name = STATES[report["status"]][2]
        home = Path.home() if home is None else Path(home)
        data = json.dumps(notice, sort_keys=True, separators=(",", ":"))
        # Separate fixed executable commands; payload is data, never shell.
        lcd = [str(home / ".local/bin/lcd"), "notice", data]
        sound = [str(home / ".local/bin/sound"), name]
        first = runner(lcd, timeout=8, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Journal claims occur in reporting.enqueue BEFORE this call. A crash
        # after LCD send or sound output never causes replay on recovery.
        second = runner(sound, timeout=8, check=False,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return first.returncode == 0 and second.returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
