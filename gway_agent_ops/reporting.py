"""Durable GitHub reporting outbox; never repeats agent execution."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from .dispatch import DEFAULT_STATE, _private_directory

ISSUE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")


def _key(issue: str, at: str) -> str:
    if not ISSUE.fullmatch(issue):
        raise ValueError("invalid issue")
    return hashlib.sha256((issue + "\n" + at).encode()).hexdigest()


def _write(path: Path, data: dict) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".report-", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def enqueue(issue: str, at: str, *, state_dir: Path | None = None,
            status: str = "interrupted", exit_code: int | None = None,
            audit: bool = False, receipt_state: str | None = None, receipt: dict | None = None,
            findings: dict | None = None, findings_state: str | None = None) -> dict:
    """Persist sanitized report prior to attempting network delivery."""
    root = Path(state_dir or DEFAULT_STATE)
    _private_directory(root)
    outbox = root / "outbox"
    _private_directory(outbox)
    identity = _key(issue, at)
    path = outbox / (identity + ".json")
    if path.is_symlink():
        raise ValueError("unsafe report path")
    if path.exists():
        return json.loads(path.read_text())
    if status not in ("interrupted", "finished", "codex_failed"):
        raise ValueError("invalid reporting status")
    record = {"issue": issue, "at": at, "status": status, "exit_code": exit_code,
              "marker": "agent-dispatch:" + identity}
    if audit:
        from .audit_receipt import validate
        allowed = ("receipt_valid", "receipt_missing", "receipt_invalid",
                   "audit_workspace_modified", "codex_failed", "codex_timeout",
                   "codex_unavailable")
        if receipt_state not in allowed:
            raise ValueError("invalid receipt state")
        if receipt_state == "receipt_valid":
            if not isinstance(receipt, dict):
                raise ValueError("missing receipt")
            record["receipt"] = validate(receipt, issue=issue, at=at, scope=receipt.get("scope"))
        elif receipt is not None:
            raise ValueError("unvalidated receipt")
        record["audit"] = True
        record["receipt_state"] = receipt_state
    if findings_state is not None:
        if findings_state not in ("validated", "missing_or_invalid"):
            raise ValueError("invalid findings state")
        if findings_state == "validated":
            from .findings import parse_findings, MARKER
            if not isinstance(findings, dict) or parse_findings(
                    MARKER + json.dumps(findings)) is None:
                raise ValueError("invalid findings")
            record["findings"] = findings
        elif findings is not None:
            raise ValueError("unexpected findings")
        record["findings_state"] = findings_state
    elif findings is not None:
        raise ValueError("findings state required")
    _write(path, record)
    return record


def _api(args: list[str], *, runner=subprocess.run, timeout=25):
    result = runner(["gh", "api", *args], capture_output=True, text=True,
                    timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("github_reporting_unavailable")
    return json.loads(result.stdout)


def deliver(record: dict, *, runner=subprocess.run, deadline=None,
            clock=time.monotonic) -> None:
    """Idempotent against successfully posted but locally unacknowledged reports."""
    repository, number = record["issue"].rsplit("#", 1)
    endpoint = f"repos/{repository}/issues/{number}/comments"
    def bounded_api(args):
        remaining = 25 if deadline is None else deadline - clock()
        if remaining <= 0:
            raise RuntimeError("report_time_budget_exceeded")
        return _api(args, runner=runner, timeout=min(25, remaining))

    # gh 2.23 does not support --slurp. Query bounded pages explicitly,
    # failing closed rather than posting when any page cannot be inspected.
    for page in range(1, 1001):
        rows = bounded_api([f"{endpoint}?per_page=100&page={page}"])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("github_reporting_invalid_response")
        if any(record["marker"] in str(row.get("body", "")) for row in rows):
            return
        if len(rows) < 100:
            break
    else:
        raise RuntimeError("github_reporting_pagination_exceeded")
    message = (f"Agent invocation {record['at']}: {record['status']}."
               + (f" Codex exit code: {record['exit_code']}." if record["exit_code"] is not None else "")
               + f"\n<!-- {record['marker']} -->")
    if record.get("findings_state") == "validated":
        from .findings import parse_findings, MARKER
        report = parse_findings(MARKER + json.dumps(record.get("findings")))
        if report is None:
            raise ValueError("invalid stored findings")
        lines = [f"Agent findings for {record['at']}", "",
                 "**Summary:** " + report["summary"], "", "**Findings:**"]
        lines.extend("- " + item for item in report["findings"])
        lines.extend(["", "**Evidence:**"])
        lines.extend("- " + item for item in report["evidence"])
        lines.extend(["", "**Limitations:** " + report["limitations"],
                      "", f"<!-- {record['marker']} -->"])
        message = "\n".join(lines)
    elif record.get("findings_state") == "missing_or_invalid":
        message = (f"Agent invocation {record['at']}: Codex exited successfully, "
                   "but the required structured findings report was missing or invalid. "
                   "Work is not validated; manual review or a new authorized run is required."
                   + f"\n<!-- {record['marker']} -->")
    if record.get("audit") is True:
        state = record.get("receipt_state")
        if state == "receipt_valid":
            from .audit_receipt import validate
            receipt = record.get("receipt")
            if not isinstance(receipt, dict):
                raise ValueError("invalid stored receipt")
            receipt = validate(receipt, issue=record["issue"], at=record["at"],
                               scope=receipt.get("scope"))
            checks = " ".join(f"{name}={receipt['checks'][name]}" for name in ("T1", "T2", "T3", "T4"))
            message = (f"Agent read-only audit {record['at']}: validated receipt. "
                       f"Scope: {receipt['scope']}. {checks}. "
                       f"Summary: {receipt['summary']}."
                       f"\n<!-- {record['marker']} -->")
        else:
            if state not in ("receipt_missing", "receipt_invalid", "audit_workspace_modified",
                             "codex_failed", "codex_timeout", "codex_unavailable"):
                raise ValueError("invalid stored receipt")
            message = (f"Agent read-only audit {record['at']}: {state}; "
                       f"execution={record['status']}. No validated PASS evidence."
                       f"\n<!-- {record['marker']} -->")
    bounded_api([endpoint, "-f", f"body={message}"])


def flush(*, state_dir: Path | None = None, runner=subprocess.run,
          max_reports=4, budget_seconds=80, clock=time.monotonic) -> dict:
    """Serialize bounded outbox-only GitHub delivery across concurrent callers.

    A timer may call `agent report` repeatedly. It must never invoke Codex or
    re-submit an issue. Unsent items stay on disk; a crash after posting is
    reconciled by the existing remote marker check on the next tick.
    """
    if type(max_reports) is not int or not 1 <= max_reports <= 16:
        raise ValueError("invalid report batch size")
    if type(budget_seconds) not in (int, float) or not 0 < budget_seconds <= 300:
        raise ValueError("invalid report time budget")
    root = Path(state_dir or DEFAULT_STATE)
    outbox = root / "outbox"
    if outbox.is_symlink():
        raise ValueError("unsafe outbox path")
    if not outbox.exists():
        return {"sent": 0, "pending": 0}
    _private_directory(root)
    _private_directory(outbox)
    gate = root / "report.lock"
    if gate.is_symlink():
        raise ValueError("unsafe report lock")
    fd = os.open(gate, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe report lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another agent report is processing this outbox; the next timer
            # tick will retry. Never race the GitHub marker-check/Post pair.
            return {"sent": 0, "pending": sum(1 for _ in outbox.glob("*.json"))}
        deadline = clock() + budget_seconds
        sent = 0
        pending = 0
        delivered = root / "delivered"
        _private_directory(delivered)
        # A failed or malformed report must not monopolize the first batch
        # forever. Persist the last *attempted* filename under report.lock;
        # restart resumes at the next filename, with wrap-around.
        cursor = root / "report-cursor.json"
        if cursor.is_symlink():
            raise ValueError("unsafe report cursor")
        after = ""
        if cursor.exists():
            previous = json.loads(cursor.read_text(encoding="utf-8"))
            after = previous.get("last") if type(previous) is dict else None
            if type(after) is not str or not re.fullmatch(r"[0-9a-f]{64}\\.json", after):
                raise ValueError("invalid report cursor")
        files = sorted(outbox.glob("*.json"))
        start = next((i for i, path in enumerate(files) if path.name > after), 0)
        scheduled = files[start:] + files[:start]
        for index, path in enumerate(scheduled):
            if index >= max_reports or clock() >= deadline:
                pending += 1
                continue
            # Cursor first: a crash cannot starve later entries. Retry of a
            # previously posted comment is still safe via the GitHub marker.
            _write(cursor, {"last": path.name})
            if path.is_symlink():
                pending += 1
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not record.get("delivery_attempted"):
                    record["delivery_attempted"] = True
                    _write(path, record)
                deliver(record, runner=runner, deadline=deadline, clock=clock)
                _write(delivered / path.name, {"marker": record["marker"], "status": record["status"],
                             "exit_code": record.get("exit_code"),
                             "audit": record.get("audit", False), "receipt_state": record.get("receipt_state"),
                             "receipt": record.get("receipt"),
                             "findings_state": record.get("findings_state")})
                path.unlink()
                sent += 1
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                pending += 1
        return {"sent": sent, "pending": pending}
    finally:
        os.close(fd)

def recover(*, state_dir: Path | None = None, active: tuple[str, str] | None = None) -> int:
    """Reconcile accepted invocations not yet given a reporting record.

    Must be called while the dispatch execution lock is held, so an active
    process cannot be misidentified as crashed.
    """
    root = Path(state_dir or DEFAULT_STATE)
    ledger = root / "accepted.jsonl"
    if not ledger.exists():
        return 0
    outbox = root / "outbox"
    _private_directory(outbox)
    delivered = root / "delivered"
    _private_directory(delivered)
    created = 0
    for line in ledger.read_text().splitlines():
        accepted = json.loads(line)
        path = outbox / (_key(accepted["issue"], accepted["at"]) + ".json")
        if ((accepted["issue"], accepted["at"]) != active
                and not path.exists() and not (delivered / path.name).exists()):
            enqueue(accepted["issue"], accepted["at"], state_dir=root)
            created += 1
    return created
