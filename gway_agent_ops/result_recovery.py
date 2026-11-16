"""Read-only recovery of a delivered AgentOps findings comment by frozen identity.

This is intentionally an explicit MCP operation. Normal observation and admission
must remain local, bounded and independent of GitHub availability.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .dispatch import ISSUE, timestamp
from .findings import MARKER, parse_findings
from .observation import observe
from .reporting import _key

MAX_PAGES = 10
MAX_COMMENT = 16000
TRUSTED_AUTHOR = "arthexis"


def _extract(body: str, *, at: str, marker: str) -> dict | None:
    """Require the exact bounded rendering produced by AgentOps reporting.

    A matching marker alone does not authorize arbitrary user-supplied Markdown
    as a findings envelope. Validate and reconstruct the complete comment.
    """
    if type(body) is not str or len(body) > MAX_COMMENT or "\r" in body:
        return None
    lines = body.split("\n")
    if len(lines) < 13 or lines[0] != f"Agent findings for {at}":
        return None
    if lines[1] != "" or not lines[2].startswith("**Summary:** "):
        return None
    try:
        fi = lines.index("**Findings:**")
        ei = lines.index("**Evidence:**")
        li = next(i for i, s in enumerate(lines) if s.startswith("**Limitations:** "))
    except (ValueError, StopIteration):
        return None
    if not (fi == 4 and fi < ei < li):
        return None
    findings = lines[fi + 1:ei - 1]
    evidence = lines[ei + 1:li - 1]
    if not findings or not evidence or any(not s.startswith("- ") for s in findings + evidence):
        return None
    candidate = {
        "summary": lines[2][len("**Summary:** "):],
        "findings": [s[2:] for s in findings],
        "evidence": [s[2:] for s in evidence],
        "limitations": lines[li][len("**Limitations:** "):],
    }
    validated = parse_findings(MARKER + json.dumps(candidate))
    if validated is None:
        return None
    canonical = "\n".join([
        f"Agent findings for {at}", "",
        "**Summary:** " + validated["summary"], "",
        "**Findings:**", *["- " + s for s in validated["findings"]],
        "", "**Evidence:**", *["- " + s for s in validated["evidence"]],
        "", "**Limitations:** " + validated["limitations"], "",
        "External services, deployments and administrative jobs are not attested by this Codex process result.",
        f"<!-- {marker} -->",
    ])
    return validated if body == canonical else None


def report(ref: str, at: str, *, state_dir: Path | None = None,
           runner=subprocess.run) -> dict:
    """Recover validated structured findings; never dispatch or repair a report."""
    if type(ref) is not str or not ISSUE.fullmatch(ref):
        raise ValueError("invalid_issue_ref")
    if type(at) is not str:
        raise ValueError("invalid_timestamp")
    at = timestamp(at)
    local = observe(ref, at, state_dir=state_dir)
    result = {"issue": ref, "at": at, "execution_state": local["state"],
              "findings_state": local.get("findings_state"),
              "delivery_confirmed": local.get("delivery_confirmed", False)}
    if not local.get("delivery_confirmed"):
        return {**result, "report_state": "not_delivered"}
    if local.get("findings_state") != "validated":
        return {**result, "report_state": "no_validated_findings"}
    repository, number = ref.rsplit("#", 1)
    endpoint = f"repos/{repository}/issues/{number}/comments"
    marker = "agent-dispatch:" + _key(ref, at)
    matches = []
    for page in range(1, MAX_PAGES + 1):
        try:
            response = runner(
                ["gh", "api", f"{endpoint}?per_page=100&page={page}"],
                capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {**result, "report_state": "github_unavailable"}
        if response.returncode:
            return {**result, "report_state": "github_unavailable"}
        try:
            rows = json.loads(response.stdout)
        except (ValueError, TypeError):
            return {**result, "report_state": "invalid_github_response"}
        if type(rows) is not list or len(rows) > 100 or any(type(row) is not dict for row in rows):
            return {**result, "report_state": "invalid_github_response"}
        for row in rows:
            body = row.get("body")
            if type(body) is not str or marker not in body:
                continue
            # GitHub content is untrusted. Bind author, marker, exact frozen
            # identity, bounded canonical rendering and sanitized schema.
            user = row.get("user")
            if type(user) is not dict or user.get("login") != TRUSTED_AUTHOR:
                continue
            parsed = _extract(body, at=at, marker=marker)
            if parsed is None:
                return {**result, "report_state": "invalid_report"}
            comment_id = row.get("id")
            if type(comment_id) is not int or comment_id < 1:
                return {**result, "report_state": "invalid_report"}
            matches.append((comment_id, parsed))
        if len(matches) > 1:
            return {**result, "report_state": "ambiguous"}
        if len(rows) < 100:
            break
    else:
        return {**result, "report_state": "incomplete_search"}
    if len(matches) != 1:
        return {**result, "report_state": "not_found"}
    comment_id, findings = matches[0]
    return {**result, "report_state": "retrieved", "source": "github_comment",
            "report_url": f"https://github.com/{repository}/issues/{number}#issuecomment-{comment_id}",
            "findings": findings}
