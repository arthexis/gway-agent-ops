"""Protected on-demand Codex invocation context; no process execution."""
from __future__ import annotations

import os
import stat
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".local/share/gway-agent-ops"
# Intentionally small: do not inherit API keys, tokens or arbitrary shell config.
PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TERM", "TZ")


def _same_user_runtime_dir(base: Path = Path("/run/user")) -> str | None:
    """Discover only the current UID's private, real user-systemd bus.

    This grants no privileges and never reads bus data. It avoids propagating
    arbitrary caller-controlled XDG_RUNTIME_DIR to an administrative agent.
    Missing or unsafe buses simply leave user systemd access unconfigured.
    """
    path = base / str(os.getuid())
    try:
        info = path.lstat()
        bus = (path / "bus").lstat()
    except OSError:
        return None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or not stat.S_ISSOCK(bus.st_mode)):
        return None
    return str(path)


def _check_directory(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("agent directory must be a real directory")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("agent directory must be owned by current user and private")


def runtime_context(*, root: Path | None = None, env: dict | None = None,
                    create: bool = False) -> dict:
    """Prepare, but never execute, a credential-isolated environment.

    Caller must explicitly provision adopted auth.json using agent adopt.
    The process may inherit operating-system privileges of the calling account;
    this environment is not a sandbox or OS user isolation boundary.
    """
    root = Path(root or DEFAULT_ROOT)
    _check_directory(root, create=create)
    seed = (os.environ if env is None else env).get("GWAY_AGENT_SEED_CODEX_HOME", "")
    if seed:
        # Explicit opt-in: reference same-user Codex home; never copy credentials.
        codex = Path(seed)
        if not codex.is_absolute():
            raise ValueError("seed Codex home must be absolute")
    else:
        codex = root / "codex-home"
    _check_directory(codex)
    config = codex / "config.toml"
    if config.is_symlink() or not config.is_file():
        raise ValueError("adopted Codex configuration is unavailable")
    info = config.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("adopted Codex configuration must be private")
    auth = codex / "auth.json"
    if auth.is_symlink():
        raise ValueError("symlinked agent authentication is not supported")
    if auth.exists():
        info = auth.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("agent authentication must be a private regular file")
    original = os.environ if env is None else env
    safe = {name: original[name] for name in PASSTHROUGH if original.get(name)}
    safe["CODEX_HOME"] = str(codex)
    safe["HOME"] = str(Path.home())
    # A trusted administrative agent may need to manage the existing
    # same-user systemd services. Non-login providers often lack this variable
    # even when the private user bus is already running.
    runtime_dir = _same_user_runtime_dir()
    if runtime_dir is not None:
        safe["XDG_RUNTIME_DIR"] = runtime_dir
    return {"environment": safe, "codex_home": str(codex),
            "authentication_available": auth.is_file(),
            "isolation": "same-user-seed-reference" if seed else "environment-only"}
