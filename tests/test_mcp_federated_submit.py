import unittest
from unittest.mock import patch
from gway_agent_ops import mcp_stdio


class FederatedSubmitTests(unittest.TestCase):
    def test_catalog_classifies_submit_as_write(self):
        tools = {tool["name"]: tool for tool in mcp_stdio.TOOLS}
        self.assertFalse(tools["submit"]["annotations"]["readOnlyHint"])
        for name in ("issues", "status", "preview", "observe"):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])

    def test_submit_uses_existing_nonblocking_gate(self):
        with patch.object(mcp_stdio, "issue_submit", return_value={"state": "requested"}) as submit:
            self.assertEqual(mcp_stdio.call_tool("submit", {
                "ref": "arthexis/gway-agent-ops#58", "at": "2026-11-07T20:00:00Z"
            }), {"state": "requested"})
            submit.assert_called_once_with("arthexis/gway-agent-ops#58",
                "2026-11-07T20:00:00Z", source="remote-api", trigger="mcp-submit")

    def test_invalid_submit_is_rejected_before_dispatch(self):
        with patch.object(mcp_stdio, "issue_submit") as submit:
            for args in ({}, {"ref": "bad", "at": "2026-11-07T20:00:00Z"},
                         {"ref": "arthexis/gway-agent-ops#58", "at": 3}):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    mcp_stdio.call_tool("submit", args)
            submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
