"""Private, declarative runtime configuration for Agent Ops entrypoints."""
from __future__ import annotations

import os
import stat
from pathlib import Path


CONFIGURATION = Path.home() / ".config/gway-agent-ops/runtime.env"
NAMES = frozenset((
    "GWAY_AGENT_WORKSPACE_ROOT",
    "GWAY_AGENT_ALLOWED_REPOS",
    "GWAY_AGENT_SEED_CODEX_HOME",
))


def _regular_private_file(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("Agent runtime configuration must be a regular file")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Agent runtime configuration must be private and operator-owned")


def _parse(path: Path) -> dict[str, str]:
    configured: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name not in NAMES or not value:
            raise ValueError(f"Invalid Agent runtime configuration at line {number}")
        if name in configured:
            raise ValueError(f"Duplicate Agent runtime configuration key at line {number}")
        if "\x00" in value:
            raise ValueError(f"Invalid Agent runtime configuration at line {number}")
        if name in ("GWAY_AGENT_WORKSPACE_ROOT", "GWAY_AGENT_SEED_CODEX_HOME"):
            if not Path(value).is_absolute():
                raise ValueError(f"Agent runtime path must be absolute at line {number}")
        configured[name] = value
    return configured


def load_runtime_environment(*, path: Path | None = None,
                             environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """Fill absent Agent Ops settings from a protected local configuration.

    This parser never sources shell code and never overrides explicitly supplied
    process configuration. A missing file preserves the existing fail-closed
    behavior; an invalid present file is rejected.
    """
    path = Path(path or CONFIGURATION)
    environment = os.environ if environ is None else environ
    try:
        _regular_private_file(path)
    except FileNotFoundError:
        return ()
    configured = _parse(path)
    applied = []
    for name, value in configured.items():
        if not environment.get(name):
            environment[name] = value
            applied.append(name)
    return tuple(applied)
