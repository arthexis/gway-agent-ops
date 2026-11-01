"""Canonical managed immutable installer for GWAY Agent Ops.

No adoption, Codex execution, service changes or pruning. Owner-only home paths.
"""
from __future__ import annotations
from contextlib import contextmanager
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid

SHA = re.compile(r"[a-f0-9]{40}")
HISTORY = "verified-releases.json"
LOCK = ".deploy.lock"


def _directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise ValueError("unsafe owner directory")
    return info


def _owned_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError("unsafe owner file")
    return info


@contextmanager
def locked(home):
    home = Path(home)
    _directory(home)
    root = home / ".local/share/gway-agent-ops"
    for parent in (home / ".local", home / ".local/share", root):
        parent.mkdir(mode=0o700, exist_ok=True)
        _directory(parent)
    fd = os.open(root / LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError("unsafe owner lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield root
    finally:
        os.close(fd)


def _read_history(root):
    path = root / HISTORY
    if not path.exists() and not path.is_symlink():
        return []
    _owned_file(path)
    if path.stat().st_size > 65536:
        raise ValueError("verification history too large")
    data = json.loads(path.read_text())
    if data.get("schema") != 1 or not isinstance(data.get("verified"), list):
        raise ValueError("invalid verification history")
    records = data["verified"]
    if len(records) > 32:
        raise ValueError("verification history too large")
    for item in records:
        if (not isinstance(item, dict) or not SHA.fullmatch(str(item.get("sha", "")))
                or not isinstance(item.get("identity"), list) or len(item["identity"]) != 2
                or any(type(value) is not int for value in item["identity"])):
            raise ValueError("invalid verification entry")
    return records


def _write_history(root, records):
    dest = root / HISTORY
    temp = root / (".verified-" + uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_EXCL | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"schema": 1, "verified": records[-32:]}, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)


def _release_for(link, releases):
    if not link.is_symlink():
        if link.exists():
            raise ValueError("unexpected non-link activation")
        return None
    resolved = link.resolve(strict=True)
    directory = releases.resolve(strict=True)
    if resolved.parent == directory and SHA.fullmatch(resolved.name):
        return resolved
    if resolved.parent.name == "bin" and resolved.name == "agent":
        candidate = resolved.parent.parent
        if candidate.parent == directory and SHA.fullmatch(candidate.name):
            return candidate
    raise ValueError("unrecognized activation target")


def _switch(link, target):
    link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _directory(link.parent)
    temp = link.parent / (".agent-release-" + uuid.uuid4().hex)
    try:
        temp.symlink_to(target)
        os.replace(temp, link)
    finally:
        temp.unlink(missing_ok=True)


def _probe(link, *, run=subprocess.run):
    for argument in ("--help", "--version"):
        run([str(link), argument], check=True, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install(source, *, expected_sha=None, home=None, run=subprocess.run):
    source = Path(source).resolve(strict=True)
    home = Path(home) if home is not None else Path.home()
    if os.geteuid() == 0:
        raise ValueError("do not deploy Agent Ops as root")
    sha = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True, timeout=20).strip().lower()
    if not SHA.fullmatch(sha) or (expected_sha is not None and sha != expected_sha.lower()):
        raise ValueError("source SHA is not the approved revision")
    with locked(home) as root:
        releases = root / "releases"
        releases.mkdir(mode=0o700, exist_ok=True)
        _directory(releases)
        entry = home / ".local/bin/agent"
        current = root / "current"
        previous = root / "previous"
        old = _release_for(entry, releases)
        _release_for(current, releases)
        _release_for(previous, releases)
        # Reject damaged history rather than overwriting it after activation.
        history = _read_history(root)
        destination = releases / sha
        created = False
        if destination.is_symlink():
            raise ValueError("symlink release")
        if not destination.exists():
            created = True
            try:
                run([sys.executable, "-m", "venv", str(destination)], check=True, timeout=90)
                _directory(destination)
                run([str(destination / "bin/pip"), "install", "--no-deps", str(source)],
                    check=True, timeout=300)
            except Exception:
                # A failed build is left for owner-controlled recovery, not recursive deletion.
                raise
        else:
            _directory(destination)
        exe = destination / "bin/agent"
        info = exe.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not info.st_mode & 0o111:
            raise ValueError("invalid release executable")
        run([str(exe), "--help"], check=True, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if old != destination:
            # Switch the established executable link atomically, then verify the installed path.
            _switch(entry, exe)
        try:
            if entry.resolve(strict=True) != exe:
                raise ValueError("installed activation mismatch")
            _probe(entry, run=run)
        except Exception:
            # Restore previous entry before releasing the installation lock.
            if old is not None and old != destination:
                _switch(entry, old / "bin/agent")
                _probe(entry, run=run)
            raise
        if old is not None and old != destination:
            _switch(previous, old)
        _switch(current, destination)
        identity = destination.stat()
        history = [row for row in history if row["sha"] != sha]
        history.append({"sha": sha, "identity": [identity.st_dev, identity.st_ino]})
        try:
            _write_history(root, history)
        except OSError:
            print("Agent Ops: verification history unavailable; retention remains blocked",
                  file=sys.stderr)
        return {"installed_sha": sha, "active": str(destination), "prune_enabled": False}



def prune(*, home=None, apply=False, limit=64):
    """Owner-locked release retention; leave verification history and data alone."""
    home = Path(home) if home is not None else Path.home()
    if os.geteuid() == 0:
        raise ValueError("do not prune Agent Ops as root")
    with locked(home) as root:
        releases = root / "releases"
        _directory(releases)
        current = _release_for(root / "current", releases)
        previous = _release_for(root / "previous", releases)
        entry = _release_for(home / ".local/bin/agent", releases)
        if current is None or entry is None:
            raise ValueError("missing active Agent Ops pointer")
        protected = {current, previous, entry} - {None}
        names = sorted(releases.iterdir())
        if len(names) > 4096:
            raise ValueError("release inventory too large")
        candidates = []
        legacy = []
        saved = []
        for path in names:
            if not SHA.fullmatch(path.name):
                legacy.append(path.name)
                continue
            info = _directory(path)
            if path in protected:
                saved.append({"name": path.name, "reasons": [
                    reason for reason, target in (("current", current), ("previous", previous),
                                                  ("cli", entry)) if target == path]})
            else:
                candidates.append({"name": path.name, "identity": [info.st_dev, info.st_ino]})
        report = {"owner": "gway-agent-ops", "root": str(root),
                  "status": "blocked" if legacy else "candidates_ready",
                  "dry_run": not apply, "protected": saved,
                  "candidates": candidates, "legacy": legacy,
                  "errors": ["unrecognized release entries present"] if legacy else []}
        if not apply:
            return report
        report["deleted"] = []
        report["skipped"] = []
        if report["errors"]:
            return report
        if len(candidates) > limit or not shutil.rmtree.avoids_symlink_attacks:
            report["errors"].append("unsafe or excessive deletion candidates")
            report["status"] = "blocked"
            return report
        for item in candidates:
            path = releases / item["name"]
            # Verify pointer state again immediately before each removal.
            if any(_release_for(link, releases) == path for link in
                   (root / "current", root / "previous", home / ".local/bin/agent")):
                report["skipped"].append(item["name"])
                continue
            try:
                info = _directory(path)
                if [info.st_dev, info.st_ino] != item["identity"]:
                    raise ValueError("release identity changed")
                shutil.rmtree(path)
                report["deleted"].append(item["name"])
            except (OSError, ValueError) as exc:
                report["errors"].append(f"{item['name']}: {type(exc).__name__}: {exc}")
                break
        report["status"] = "partial" if report["errors"] else "completed"
        return report


def format_prune(report):
    applied = "deleted" in report
    lines = ["AGENT OPS release retention — " + ("apply" if applied else "dry-run"),
             f"Root: {report['root']}",
             f"Status: {report['status']} | Protected: {len(report['protected'])} | "
             f"Candidates: {len(report['candidates'])} | Legacy: {len(report['legacy'])}"]
    for label, values in (("Protected", report["protected"]),
                          ("Deletion candidates", report["candidates"])):
        if values:
            lines.append(label + ":")
            for value in values:
                lines.append("  " + value["name"] +
                             (" (" + ", ".join(value["reasons"]) + ")" if value.get("reasons") else ""))
    if report["legacy"]:
        lines.extend("  legacy: " + name for name in report["legacy"])
    if report["errors"]:
        lines.extend("  ERROR: " + error for error in report["errors"])
    if applied:
        lines.append(f"Deleted {len(report['deleted'])} release(s).")
        lines.extend("  " + name for name in report["deleted"])
    else:
        lines.append("No releases deleted (dry-run).")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Canonical Agent Ops appliance installation")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--expected-sha")
    parser.add_argument("--prune", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.prune:
            result = prune(apply=args.apply)
            print(json.dumps(result, indent=2, sort_keys=True) if args.json else format_prune(result))
            if result["errors"]:
                parser.exit(1)
        else:
            if args.apply or args.dry_run or args.json:
                parser.error("prune-specific options require --prune")
            print(json.dumps(install(args.source, expected_sha=args.expected_sha), sort_keys=True))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, "Agent Ops installation refused: " + type(exc).__name__ + "\n")


if __name__ == "__main__":
    main()
