"""Read-only local Codex configuration inspection, with no secret disclosure."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


def _metadata(path: Path) -> dict:
    try:
        info = path.lstat()
    except OSError:
        return {"present": False, "regular": False, "symlink": False, "readable": False}
    regular = stat.S_ISREG(info.st_mode)
    symlink = stat.S_ISLNK(info.st_mode)
    return {"present": True, "regular": regular, "symlink": symlink,
            "readable": bool(regular and os.access(path, os.R_OK)),
            "permissions_restrictive": bool(info.st_mode & 0o077 == 0)}


def inspect(*, home: Path | None = None, codex_home: Path | None = None,
            executable: str | None = None) -> dict:
    """Inspect only filenames and metadata; never open config or auth files.

    Source may be the operator's current Codex home. Destination will be a
    separate agent-owned copy in A2.2; no configuration links are created.
    """
    home = Path.home() if home is None else Path(home)
    source = (Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
              if codex_home is None else Path(codex_home))
    candidate = executable if executable is not None else shutil.which("codex")
    config = _metadata(source / "config.toml")
    auth = _metadata(source / "auth.json")
    return {
        "source": str(source),
        "codex_executable": {"available": bool(candidate), "path": candidate},
        "config": config,
        "authentication": {"file": auth, "environment_api_key_present": bool(os.environ.get("OPENAI_API_KEY")),
                           "status": "available" if auth["readable"] or os.environ.get("OPENAI_API_KEY") else "unavailable"},
        "adoption": {"mode": "copy", "changes_made": False,
                     "supported": False, "next_step": "A2.2"},
    }


# Only scalar Codex preferences with well-defined behavior are adopted.
# Provider credentials require an additional explicit flag.
SETTING_KEYS = ("model", "model_reasoning_effort", "approval_policy", "sandbox_mode")


def _safe_source(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("source must be a regular non-symlink file")
    if info.st_size > 1024 * 1024:
        raise ValueError("source is too large")
    return path.read_bytes()


def _toml_literal(value: str) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


def _install_file(target: Path, data: bytes, *, replace: bool) -> str:
    import tempfile
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("invalid destination file")
    if target.exists() and target.read_bytes() == data:
        return "unchanged"
    if target.exists() and not replace:
        raise FileExistsError("destination exists; use --force to replace")
    fd, temporary = tempfile.mkstemp(prefix=".agent-adopt-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return "written"


def adopt(*, source: Path | None = None, destination: Path | None = None,
          include_auth: bool = False, force: bool = False) -> dict:
    """Copy explicitly supported settings; credentials only with opt-in.

    This never links to the operator home, never reads the API-key environment
    into stored files, and refuses replacement of different existing files
    unless force is requested.
    """
    import tomllib
    source = Path(source or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    target = Path(destination or Path.home() / ".local/share/gway-agent-ops/codex-home")
    if source.is_symlink() or target.is_symlink():
        raise ValueError("symlinked Codex home is not supported")
    if not source.is_dir():
        raise ValueError("source Codex home does not exist")
    source_config = source / "config.toml"
    parsed = tomllib.loads(_safe_source(source_config).decode("utf-8"))
    selected = {key: parsed[key] for key in SETTING_KEYS
                if key in parsed and type(parsed[key]) is str}
    # Keep a minimal, explicitly allowlisted Codex TOML file, never a
    # wholesale configuration with arbitrary hooks or external commands.
    data = "".join(f"{key} = {_toml_literal(value)}\n"
                   for key, value in selected.items()).encode("utf-8")
    if not data:
        raise ValueError("source has no supported scalar Codex settings")
    auth_data = None
    if include_auth:
        auth_path = source / "auth.json"
        if auth_path.stat().st_mode & 0o077:
            raise ValueError("auth.json must be owner-only before adoption")
        auth_data = _safe_source(auth_path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists() and (not target.is_dir() or target.is_symlink()):
        raise ValueError("invalid agent destination directory")
    target.mkdir(mode=0o700, exist_ok=True)
    target.chmod(0o700)
    # Preflight all collisions before changing anything.
    planned = {"config.toml": data}
    if auth_data is not None:
        planned["auth.json"] = auth_data
    for name, content in planned.items():
        dest = target / name
        if dest.is_symlink() or (dest.exists() and not dest.is_file()):
            raise ValueError("invalid destination file")
        if dest.exists() and dest.read_bytes() != content and not force:
            raise FileExistsError("destination exists; use --force to replace")
    result = {name: _install_file(target / name, content, replace=force)
              for name, content in planned.items()}
    return {"destination": str(target), "mode": "copy", "files": result,
            "auth_copied": auth_data is not None}
