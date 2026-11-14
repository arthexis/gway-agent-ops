"""Submission preflight errors must be actionable without leaking internals."""
import json
from unittest.mock import patch

import pytest

from gway_agent_ops.cli import main


@pytest.mark.parametrize("reason", [
    "agent_workspace_not_configured",
    "agent_workspace_unavailable",
    "agent_workspace_unsafe",
    "agent_systemd_run_unavailable",
    "agent_user_bus_unavailable",
])
def test_submit_reports_safe_preflight_error(reason, capsys):
    with patch("gway_agent_ops.nonblocking.submit", side_effect=ValueError(reason)):
        assert main(["submit", "arthexis/ocpp-reports#173",
                     "--at", "2026-11-05T01:57:00Z", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == reason
    assert result["error"]["message"] != "Unable to request invocation"


def test_submit_redacts_unexpected_value_error(capsys):
    with patch("gway_agent_ops.nonblocking.submit",
               side_effect=ValueError("sensitive/private/path")):
        assert main(["submit", "arthexis/ocpp-reports#173",
                     "--at", "2026-11-05T01:57:00Z", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == {
        "code": "submission_unavailable",
        "message": "Unable to request invocation",
    }
