"""Owner-controlled activation for automatic pickup; absent means disabled."""
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
import socket

from .dispatch import ISSUE
from .environment import _regular_private_file
from .pickup_policy import NODE, _instant
from .pickup_store import GENERATION

CONFIG = Path.home() / ".config/gway-agent-ops/pickup.json"


def load(*, path=None, node_reader=socket.gethostname):
    path = Path(path or CONFIG)
    try:
        _regular_private_file(path)
    except FileNotFoundError:
        return None
    if path.stat().st_size > 4096:
        raise ValueError("pickup_configuration_invalid")
    data = json.loads(path.read_text(encoding="utf-8"))
    node = node_reader()
    if (not isinstance(data, dict) or not isinstance(node, str)
            or not NODE.fullmatch(node) or data.get("node") != node
            or data.get("schema") != 1):
        raise ValueError("pickup_configuration_invalid")
    if data.get("mode") == "disabled":
        if set(data) != {"schema", "node", "mode"}:
            raise ValueError("pickup_configuration_invalid")
        return None
    # Initial production activation is only supported on the accepted canary
    # node. Additional nodes require their own rollout and coordination review.
    if node != "gway-001" or data.get("mode") not in ("canary", "enabled"):
        raise ValueError("pickup_activation_not_accepted")
    keys = ("issue", "generation") if data["mode"] == "canary" else ("canary_issue", "canary_generation")
    if set(data) != {"schema", "node", "mode", "cutover", *keys}:
        raise ValueError("pickup_configuration_invalid")
    ref, generation = (data[key] for key in keys)
    if (not isinstance(ref, str) or not ISSUE.fullmatch(ref)
            or not isinstance(generation, str)
            or not GENERATION.fullmatch(generation)
            or _instant(data["cutover"]) > datetime.now(timezone.utc)):
        raise ValueError("pickup_configuration_invalid")
    return data


def status():
    """Bounded read-only projection; pickup faults must not hide manual status."""
    from .pickup_store import snapshot
    try:
        config = load()
        rows = snapshot()
        return {"mode": config["mode"] if config else "disabled",
                "assignment_count": len(rows), "assignments": rows[-50:]}
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, sqlite3.Error):
        return {"mode": "unavailable", "assignment_count": None, "assignments": []}
