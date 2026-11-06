"""Read-only MCP provider contract regression tests."""
import unittest
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

    def test_initialize_and_notification(self):
        init = mcp_stdio.handle({"jsonrpc": "2.0", "id": 5, "method": "initialize"})
        self.assertEqual(init["result"]["serverInfo"]["name"], "gway-agent-ops")
        self.assertIsNone(mcp_stdio.handle({"jsonrpc": "2.0",
                                            "method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
