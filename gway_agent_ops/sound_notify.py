"""Codex external notify adapter. Only documented agent-turn-complete is supported."""
from __future__ import annotations

import json
import os
import subprocess


def dispatch(payload: str, *, runner=subprocess.run, sound_bin: str = "/usr/local/bin/sound") -> dict:
    """Accept a bounded notification, not instructions or executable arguments."""
    if len(payload.encode("utf-8")) > 65536:
        raise ValueError("notification too large")
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid notification JSON") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        raise ValueError("invalid notification object")
    kind = obj["type"]
    if kind != "agent-turn-complete":
        return {"event": kind, "played": False, "reason": "unsupported_event"}
    try:
        result = runner([sound_bin, "--event", "Stop", "--source", "codex"],
                        timeout=12, check=False, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return {"event": kind, "played": False, "reason": "sound_unavailable"}
    return {"event": kind, "played": result.returncode == 0,
            "reason": "submitted" if result.returncode == 0 else "sound_unavailable"}
