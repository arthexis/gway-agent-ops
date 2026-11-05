"""Agent Ops requires report evidence, not merely Codex exit 0."""
import json

from gway_agent_ops.findings import MARKER, parse_findings
from gway_agent_ops.executor import build_issue_prompt


def test_valid_report():
    report = {
        "summary": "Dashboard checks were completed.",
        "findings": ["HTTP endpoint responded successfully."],
        "evidence": ["HTTP status 200 observed at the endpoint."],
        "limitations": "No browser screenshot available.",
    }
    assert parse_findings("codex logs\\n" + MARKER + json.dumps(report)) == report


def test_missing_and_incomplete_reports_rejected():
    assert parse_findings("all tests passed") is None
    assert parse_findings(MARKER + '{"summary":"done"}') is None


def test_secret_rejected():
    report = {
        "summary": "Dashboard checks were completed.",
        "findings": ["HTTP endpoint responded successfully."],
        "evidence": ["Credential ghp_123456789 leaked in logs."],
        "limitations": "No browser screenshot available.",
    }
    assert parse_findings(MARKER + json.dumps(report)) is None


def test_prompt_requires_structured_evidence():
    prompt = build_issue_prompt({"repository": "arthexis/example", "number": 1,
                                 "title": "Audit", "body": "Read-only checks"})
    assert MARKER in prompt
    assert "evidence" in prompt
    assert "could not be verified" in prompt
