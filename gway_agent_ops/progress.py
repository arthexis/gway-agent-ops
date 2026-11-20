"""Bounded, identity-bound progress of the local AgentOps runner.

This records *runner activity*, not Codex reasoning, task completion or a
verified external side effect. No prompts, output, logs or credentials are read.
Telemetry is best-effort: it never changes admission or terminal reporting.
"""
from __future__ import annotations

import json
import os
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

from .dispatch import DEFAULT_STATE, _private_directory, timestamp
from .reporting import _key, _write

SCHEMA = 1
PHASES = frozenset({"preparing", "executing", "finalizing", "uncertain"})
HEARTBEAT_SECONDS = 60
STALE_SECONDS = 150
MAX_BYTES = 2048

_SUMMARIES = {
    "preparing": "Runner preparing the invocation",
    "executing": "Runner waiting for the Codex invocation",
    "finalizing": "Codex returned; runner persisting the final report",
    "uncertain": "Runner stopped without confirmed terminal report",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        raise ValueError("invalid progress timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
        raise ValueError("invalid progress timestamp")
    return result


def _file(root: Path, ref: str, at: str) -> Path:
    return root / "progress" / (_key(ref, at) + ".json")


class ProgressRecorder:
    """One private snapshot per accepted identity, no additional worker process."""

    def __init__(self, ref: str, at: str, *, state_dir: Path | None = None,
                 interval_seconds: int = HEARTBEAT_SECONDS):
        if type(interval_seconds) is not int or not 1 <= interval_seconds <= 300:
            raise ValueError("invalid heartbeat interval")
        self.ref = ref
        self.at = timestamp(at)
        self.root = Path(state_dir or DEFAULT_STATE)
        self.path = _file(self.root, self.ref, self.at)
        self.interval = interval_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = False
        self._report_persisted = False
        started = _format(_utc_now())
        self._record = {
            "schema": SCHEMA,
            "issue": self.ref,
            "at": self.at,
            "phase": "preparing",
            "started_at": started,
            "phase_changed_at": started,
            "heartbeat_at": started,
        }

    def _persist(self) -> None:
        if self.path.is_symlink():
            raise ValueError("unsafe progress file")
        _write(self.path, self._record.copy())

    def __enter__(self):
        try:
            _private_directory(self.root)
            _private_directory(self.path.parent)
            with self._lock:
                self._persist()
            self._enabled = True
        except (OSError, ValueError):
            # Observability cannot block the already admitted administrative job.
            self._enabled = False
        if self._enabled:
            self._thread = threading.Thread(target=self._heartbeat,
                                            name="agentops-progress", daemon=True)
            self._thread.start()
        return self

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.interval):
            if not self._enabled:
                return
            with self._lock:
                self._record["heartbeat_at"] = _format(_utc_now())
                try:
                    self._persist()
                except (OSError, ValueError):
                    self._enabled = False
                    return

    def mark(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError("invalid progress phase")
        if not self._enabled:
            return
        with self._lock:
            instant = _format(_utc_now())
            self._record["phase"] = phase
            self._record["phase_changed_at"] = instant
            self._record["heartbeat_at"] = instant
            try:
                self._persist()
            except (OSError, ValueError):
                self._enabled = False

    def terminal_report_persisted(self) -> None:
        """Called only after the existing terminal outbox fsync succeeded."""
        self._report_persisted = True

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._report_persisted:
            discard_progress(self.ref, self.at, state_dir=self.root)


def discard_progress(ref: str, at: str, *, state_dir: Path | None = None) -> None:
    """Best-effort cleanup only after a durable terminal or release marker."""
    root = Path(state_dir or DEFAULT_STATE)
    folder = root / "progress"
    if folder.is_symlink() or not folder.is_dir():
        return
    try:
        folder_info = folder.lstat()
        if folder_info.st_uid != os.getuid() or folder_info.st_mode & 0o077:
            return
        path = _file(root, ref, timestamp(at))
        info = path.lstat()
        if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077):
            path.unlink()
    except (OSError, ValueError):
        pass


def read_progress(ref: str, at: str, *, state_dir: Path | None = None,
                  now: datetime | None = None) -> dict | None:
    """Read only bounded, private data for an exact issue/timestamp identity.

    Invalid or spoofed snapshots never become positive liveness evidence.
    Legacy executions have no progress field.
    """
    root = Path(state_dir or DEFAULT_STATE)
    at = timestamp(at)
    folder = root / "progress"
    if folder.is_symlink():
        return {"freshness": "invalid", "phase": "unknown"}
    if not folder.exists():
        return None
    try:
        folder_stat = folder.lstat()
        if (not stat.S_ISDIR(folder_stat.st_mode)
                or folder_stat.st_uid != os.getuid() or folder_stat.st_mode & 0o077):
            return {"freshness": "invalid", "phase": "unknown"}
        path = _file(root, ref, at)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
                raise ValueError("invalid progress file")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
                payload = handle.read(MAX_BYTES + 1)
            if len(payload.encode("utf-8")) > MAX_BYTES:
                raise ValueError("oversized progress record")
        finally:
            os.close(fd)
        data = json.loads(payload)
        keys = {"schema", "issue", "at", "phase", "started_at",
                "phase_changed_at", "heartbeat_at"}
        if (type(data) is not dict or set(data) != keys or type(data["schema"]) is not int
                or data["schema"] != SCHEMA or data["issue"] != ref
                or data["at"] != at or data["phase"] not in PHASES):
            raise ValueError("invalid progress identity")
        started = _parse(data["started_at"])
        changed = _parse(data["phase_changed_at"])
        heartbeat = _parse(data["heartbeat_at"])
        current = now or _utc_now()
        if current.tzinfo is None:
            raise ValueError("naive current time")
        age = (current - heartbeat).total_seconds()
        if not started <= changed <= heartbeat or age < -5:
            raise ValueError("invalid progress clock")
        return {
            "phase": data["phase"],
            "phase_source": "runner",
            "functional_phase": "unknown",
            "heartbeat_at": data["heartbeat_at"],
            "phase_changed_at": data["phase_changed_at"],
            "last_progress_at": data["phase_changed_at"],
            "elapsed_seconds": max(0, int((current - started).total_seconds())),
            "heartbeat_age_seconds": max(0, int(age)),
            "freshness": "fresh" if age <= STALE_SECONDS else "stale",
            "summary": _SUMMARIES[data["phase"]],
        }
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        return {"freshness": "invalid", "phase": "unknown"}
