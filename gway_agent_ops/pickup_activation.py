"""Owner-controlled activation for automatic pickup; absent means disabled."""
from datetime import datetime, timezone
import json
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
    # Only 001 is currently approved for canary activation. A fleet-wide
    # activation and cross-node/manual execution fence require acceptance.
    if node != "gway-001" or data.get("mode") != "canary":
        raise ValueError("pickup_activation_not_accepted")
    if set(data) != {"schema", "node", "mode", "cutover", "issue", "generation"}:
        raise ValueError("pickup_configuration_invalid")
    if (not isinstance(data["issue"], str) or not ISSUE.fullmatch(data["issue"])
            or not isinstance(data["generation"], str)
            or not GENERATION.fullmatch(data["generation"])
            or _instant(data["cutover"]) > datetime.now(timezone.utc)):
        raise ValueError("pickup_configuration_invalid")
    return data
