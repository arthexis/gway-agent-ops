"""A2.4 non-executing readiness and adoption verification."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from .runtime import runtime_context


def verify(*, root: Path | None = None) -> dict:
    """Report safe readiness without leaking authentication or launching Codex."""
    try:
        context = runtime_context(root=root)
        codex_home = Path(context["codex_home"])
        config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not config:
            raise ValueError("adopted configuration is empty")
        auth_available = context["authentication_available"]
        if auth_available:
            # Validate JSON syntax without emitting or persisting its contents.
            auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
            if not isinstance(auth, dict):
                raise ValueError("agent auth file must contain an object")
        return {"ready": True, "config_present": True,
                "authentication_available": auth_available,
                "authentication_format_valid": auth_available,
                "runtime": context["isolation"], "issues": []}
    except (OSError, ValueError, PermissionError, tomllib.TOMLDecodeError) as exc:
        # Generic failure categories avoid leaking local paths or secret contents.
        reason = ("permissions" if isinstance(exc, PermissionError)
                  else "configuration_unavailable")
        return {"ready": False, "config_present": False,
                "authentication_available": False,
                "authentication_format_valid": False,
                "runtime": "environment-only", "issues": [reason]}
