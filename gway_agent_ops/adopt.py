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
