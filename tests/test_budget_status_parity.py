"""Offline CLI/MCP budget-status parity using synthetic Codex JSONL sessions."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gway_agent_ops import cli, codex_budget, mcp_stdio


class BudgetStatusParityTests(unittest.TestCase):
    def _assert_parity(self, events, expected_five, expected_weekly):
        admission = {"state": "idle", "available": True,
                     "active_issue": None, "active_at": None}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if events:
                (root / "synthetic.jsonl").write_text(
                    "\n".join(json.dumps(event) for event in events) + "\n",
                    encoding="utf-8")
            actual_read_budgets = codex_budget.read_budgets

            def fixture_budgets():
                return actual_read_budgets(root)

            output = io.StringIO()
            with patch.object(cli, "load_runtime_environment", return_value=None), \
                 patch("gway_agent_ops.admission_status.status", return_value=admission), \
                 patch.object(mcp_stdio, "admission_status", return_value=admission), \
                 patch.object(codex_budget, "read_budgets", side_effect=fixture_budgets), \
                 patch.object(mcp_stdio, "read_budgets", side_effect=fixture_budgets), \
                 redirect_stdout(output):
                exit_code = cli.main(["status", "--json"])
                mcp_data = mcp_stdio.call_tool("status", {})

        self.assertEqual(exit_code, 0)
        cli_envelope = json.loads(output.getvalue().strip())
        self.assertEqual(cli_envelope["status"], "ok")
        self.assertEqual(cli_envelope["data"], mcp_data)
        self.assertEqual(cli_envelope["data"]["admission"], admission)
        self.assertFalse(cli_envelope["data"]["active"])
        self.assertEqual(
            mcp_data["budget"]["five_hour"]["remaining_percent"], expected_five)
        self.assertEqual(
            mcp_data["budget"]["weekly"]["remaining_percent"], expected_weekly)

    def test_current_session_format_is_visible_on_both_interfaces(self):
        event = {"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat(),
                 "payload": {
                     "type": "token_count",
                     "info": {"total_token_usage": {"total_tokens": 40}},
                     "rate_limits": {
                         "primary": {"used_percent": 10, "window_minutes": 300,
                                     "resets_at": 1795000000},
                         "secondary": {"used_percent": 35, "window_minutes": 10080,
                                       "resets_at": 1796000000}}}}
        self._assert_parity([event], 90, 65)

    def test_single_available_window_is_independent(self):
        event = {"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat(),
                 "payload": {"type": "token_count", "rate_limits": {
                     "secondary": {"used_percent": 55, "window_minutes": 10080}}}}
        self._assert_parity([event], None, 45)

    def test_unavailable_windows_are_equivalent(self):
        self._assert_parity([], None, None)

    def test_stale_observations_do_not_become_available(self):
        event = {"type": "event_msg",
                 "timestamp": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                 "payload": {"type": "token_count", "rate_limits": {
                     "primary": {"used_percent": 15, "window_minutes": 300}}}}
        self._assert_parity([event], None, None)


if __name__ == "__main__":
    unittest.main()
