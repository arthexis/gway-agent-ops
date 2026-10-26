"""Strict, bounded audit evidence. Never retain model transcripts or raw logs."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

STATES = frozenset(("PASS", "BLOCKED", "UNKNOWN"))
FIELDS = frozenset(("version", "issue", "at", "scope", "checks", "summary"))
CHECKS = ("T1", "T2", "T3", "T4")
ISSUE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")
SAFE = re.compile(r"^[A-Za-z0-9 .,;:()/_+#%=-]*$")
DANGEROUS = re.compile(
    r"(?i)(token|password|secret|credential|private.key|BEGIN |ssh-|"
    r"sk-[a-z0-9]|ghp_|github_pat_|Bearer |"
    r"(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.|172\.(?:1[6-9]|2[0-9]|3[01])\.|"
    r"\b[A-Fa-f0-9]{32,}\b)"
)


def _string(value: object, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum or not SAFE.fullmatch(value):
        raise ValueError("receipt_invalid")
    if DANGEROUS.search(value) or "<" in value or ">" in value or "\n" in value:
        raise ValueError("receipt_invalid")
    return value


def validate(value: object, *, issue: str, at: str, scope: str) -> dict:
    """Fail closed on unknown keys, invalid identities, uncontrolled prose or secrets."""
    if not isinstance(value, dict) or set(value) != FIELDS or value["version"] != 1:
        raise ValueError("receipt_invalid")
    if not ISSUE.fullmatch(issue) or value["issue"] != issue or value["at"] != at:
        raise ValueError("receipt_invalid")
    try:
        parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z") != at:
            raise ValueError("receipt_invalid")
    except (TypeError, ValueError) as exc:
        raise ValueError("receipt_invalid") from exc
    if _string(value["scope"], 80) != _string(scope, 80) or not scope:
        raise ValueError("receipt_invalid")
    checks = value["checks"]
    if not isinstance(checks, dict) or set(checks) != set(CHECKS):
        raise ValueError("receipt_invalid")
    if any(type(checks[k]) is not str or checks[k] not in STATES for k in CHECKS):
        raise ValueError("receipt_invalid")
    _string(value["summary"], 240)
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 1024:
        raise ValueError("receipt_invalid")
    return {k: value[k] for k in ("version", "issue", "at", "scope", "checks", "summary")}


def parse(raw: str, *, issue: str, at: str, scope: str) -> dict:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4096:
        raise ValueError("receipt_invalid")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("receipt_invalid") from exc
    return validate(data, issue=issue, at=at, scope=scope)
