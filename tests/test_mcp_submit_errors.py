"""MCP submit failures must be actionable without leaking exception details."""
import json
import unittest
from unittest.mock import patch
from gway_agent_ops.mcp_stdio import handle


class SubmitErrorTests(unittest.TestCase):
    def test_safe_prerequisite_errors_are_reported(self):
        for code in ("agent_cli_not_found", "agent_workspace_not_configured",
                     "agent_workspace_unavailable", "agent_workspace_unsafe",
                     "agent_systemd_run_unavailable", "agent_user_bus_unavailable"):
            with self.subTest(code=code):
                with patch("gway_agent_ops.mcp_stdio._repositories", return_value=("arthexis/gway-agent-ops",)), patch(
                        "gway_agent_ops.mcp_stdio.issue_submit", side_effect=ValueError(code)):
                    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "submit", "arguments": {
                                           "ref": "arthexis/gway-agent-ops#68",
                                           "at": "2026-11-07T22:21:30Z"}}})
                result = response["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(json.loads(result["content"][0]["text"]), {"error": code})

    def test_unknown_exception_remains_redacted(self):
        with patch("gway_agent_ops.mcp_stdio._repositories", return_value=("arthexis/gway-agent-ops",)), patch(
                "gway_agent_ops.mcp_stdio.issue_submit", side_effect=ValueError("private secret")):
            response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "submit", "arguments": {
                                   "ref": "arthexis/gway-agent-ops#68",
                                   "at": "2026-11-07T22:21:30Z"}}})
        self.assertEqual(json.loads(response["result"]["content"][0]["text"]),
                         {"error": "provider_unavailable"})


if __name__ == "__main__":
    unittest.main()
