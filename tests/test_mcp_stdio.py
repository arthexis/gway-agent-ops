"""Read-only MCP provider contract regression tests."""
import unittest
import json
from unittest.mock import patch

from gway_agent_ops import mcp_stdio


class McpStdioTests(unittest.TestCase):
    def test_catalog_has_no_mutations(self):
        response = mcp_stdio.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(names, {"issues", "status", "preview", "observe"})
        self.assertTrue(all(tool["annotations"]["readOnlyHint"]
                            for tool in response["result"]["tools"]))

    def test_submit_cannot_be_called(self):
        result = mcp_stdio.handle({"jsonrpc": "2.0", "id": 2,
                                   "method": "tools/call",
                                   "params": {"name": "submit", "arguments": {}}})
        self.assertTrue(result["result"]["isError"])

    def test_status_delegates_to_read_only_state(self):
        with patch.object(mcp_stdio, "admission_status", return_value={"state": "idle"}):
            response = mcp_stdio.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                         "params": {"name": "status", "arguments": {}}})
        self.assertIn('"state": "idle"', response["result"]["content"][0]["text"])

    def test_preview_requires_exact_ref(self):
        result = mcp_stdio.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                   "params": {"name": "preview", "arguments": {"ref": "../secret"}}})
        self.assertTrue(result["result"]["isError"])

    def test_safe_error_classification(self):
        from gway_agent_ops.github_issues import IssueRejected
        from gway_agent_ops.executor import ExecutionError
        cases = [
            (IssueRejected("repository_not_allowed"), "repository_not_allowed"),
            (ExecutionError("repository_allowlist_missing"), "repository_allowlist_missing"),
            (IssueRejected("github_unavailable"), "github_unavailable"),
            (RuntimeError("token=secret"), "provider_unavailable"),
        ]
        for exc, code in cases:
            with self.subTest(code=code):
                with patch.object(mcp_stdio, "call_tool", side_effect=exc):
                    response = mcp_stdio.handle({"jsonrpc": "2.0", "id": 6,
                        "method": "tools/call", "params": {"name": "issues", "arguments": {}}})
                result = response["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(json.loads(result["content"][0]["text"]), {"error": code})
                self.assertNotIn("secret", str(result))

    def test_initialize_and_notification(self):
        init = mcp_stdio.handle({"jsonrpc": "2.0", "id": 5, "method": "initialize"})
        self.assertEqual(init["result"]["serverInfo"]["name"], "gway-agent-ops")
        self.assertIsNone(mcp_stdio.handle({"jsonrpc": "2.0",
                                            "method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
