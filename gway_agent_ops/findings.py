"""Validate a bounded, explicitly structured agent findings report."""
from __future__ import annotations

import json
import re

MARKER = "AGENT_OPS_FINDINGS_JSON:"
REQUIRED = ("summary", "findings", "evidence", "limitations")
SECRET = re.compile(r"(?i)(bearer\\s+[a-z0-9._-]+|gh[pousr]_[a-z0-9_]+|sk-[a-z0-9_-]+|-----BEGIN [^-]+PRIVATE KEY-----)")


def parse_findings(stdout: str) -> dict | None:
    """Accept only the last explicit report, never arbitrary Codex logs."""
    if not isinstance(stdout, str):
        return None
    start = stdout.rfind(MARKER)
    if start < 0:
        return None
    raw = stdout[start + len(MARKER):].strip()
    if len(raw) > 12000:
        return None
    try:
        report = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(report, dict) or set(report) != set(REQUIRED):
        return None
    if not isinstance(report["summary"], str) or not 10 <= len(report["summary"].strip()) <= 1200:
        return None
    if not isinstance(report["limitations"], str) or not 3 <= len(report["limitations"].strip()) <= 1200:
        return None
    for field in ("findings", "evidence"):
        rows = report[field]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
            return None
        if any(not isinstance(row, str) or not 8 <= len(row.strip()) <= 500 for row in rows):
            return None
    if SECRET.search(json.dumps(report)):
        return None
    return report
