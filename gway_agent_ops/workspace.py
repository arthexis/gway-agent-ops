"""Provision trusted GitHub workspaces without modifying existing worktrees."""
from __future__ import annotations

import ctypes
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit

from .github_issues import REPOSITORY


class WorkspaceError(RuntimeError):
    pass


def _directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise WorkspaceError("workspace_unsafe")


def _match(parent: Path, name: str) -> Path | None:
    matches = [p for p in parent.iterdir() if p.name.casefold() == name.casefold()]
    if len(matches) > 1:
        raise WorkspaceError("workspace_collision")
    if matches:
        _directory(matches[0])
        return matches[0]
    return None


def _origin_matches(value: str, repository: str) -> bool:
    # Accept standard credential-free GitHub HTTPS and SSH origins only.
    match = re.fullmatch(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?", value, re.I)
    if match:
        return match.group(1).casefold() == repository.casefold()
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
            or parsed.query or parsed.fragment):
        return False
    path = parsed.path.removeprefix("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path.casefold() == repository.casefold()


def _git(args: list[str], *, cwd: Path | None = None, timeout: int = 120):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
    try:
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                                cwd=cwd, env=env, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise WorkspaceError("workspace_git_unavailable") from None
    if result.returncode:
        raise WorkspaceError("workspace_git_failed")
    return result.stdout.strip()


def provision(root: Path, repository: str) -> Path:
    if not REPOSITORY.fullmatch(repository) or any(x in (".", "..") for x in repository.split("/")):
        raise WorkspaceError("workspace_identity_invalid")
    root = Path(root)
    try:
        _directory(root)
        root = root.resolve(strict=True)
        owner, name = repository.split("/")
        # Lock files are private and identity-normalized, independent of spelling.
        lock = root / (".workspace-" + hashlib.sha256(repository.casefold().encode()).hexdigest() + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise WorkspaceError("workspace_unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX)
            parent = _match(root, owner)
            if parent is None:
                parent = root / owner.casefold()
                try:
                    parent.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                parent = _match(root, owner)
            checkout = _match(parent, name)
            if checkout is None:
                checkout = parent / name.casefold()
                tmp = Path(tempfile.mkdtemp(prefix=".clone-", dir=parent))
                try:
                    _git(["clone", "--no-recurse-submodules", "--", f"https://github.com/{repository}.git", str(tmp)])
                    _validate(tmp, repository)
                    # Lock protects AgentOps writers; never replace another path.
                    if _match(parent, name) is not None:
                        raise WorkspaceError("workspace_collision")
                    # Linux renameat2 prevents replacing a path created by an
                    # unrelated process between the collision check and publish.
                    libc = ctypes.CDLL(None, use_errno=True)
                    rename = libc.renameat2
                    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                       ctypes.c_char_p, ctypes.c_uint]
                    rename.restype = ctypes.c_int
                    if rename(-100, os.fsencode(tmp), -100, os.fsencode(checkout), 1) != 0:
                        raise WorkspaceError("workspace_publish_failed")
                finally:
                    if tmp.exists():
                        shutil.rmtree(tmp)
            _validate(checkout, repository)
            return checkout
        finally:
            os.close(fd)
    except WorkspaceError:
        raise
    except (OSError, ValueError):
        raise WorkspaceError("workspace_unavailable") from None


def _validate(checkout: Path, repository: str) -> None:
    _directory(checkout)
    gitdir = checkout / ".git"
    _directory(gitdir)
    origin = _git(["remote", "get-url", "origin"], cwd=checkout, timeout=10)
    if not _origin_matches(origin, repository):
        raise WorkspaceError("workspace_origin_mismatch")
