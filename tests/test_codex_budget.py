import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from gway_agent_ops.codex_budget import read_budgets


class CodexBudgetTests(unittest.TestCase):
    def test_missing_sessions_remain_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = read_budgets(Path(tmp) / "missing")
        self.assertFalse(result["five_hour"]["available"])
        self.assertFalse(result["weekly"]["available"])

    def test_reads_independent_windows_from_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timestamp = datetime.now(timezone.utc).isoformat()
            event = {"type": "event_msg", "timestamp": timestamp,
                     "payload": {"type": "token_count", "info": {"rate_limits": {
                         "primary": {"used_percent": 25, "window_minutes": 300, "resets_at": 1793000000},
                         "secondary": {"used_percent": 60, "window_minutes": 10080, "resets_at": 1793500000}
                     }}}}
            (root / "session.jsonl").write_text(json.dumps(event) + "\n")
            result = read_budgets(root)
        self.assertEqual(result["five_hour"]["remaining_percent"], 75)
        self.assertEqual(result["weekly"]["remaining_percent"], 40)
        self.assertTrue(result["five_hour"]["available"])
        self.assertIn("resets_at", result["weekly"])

    def test_rejects_unrelated_window_and_invalid_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event = {"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat(),
                     "payload": {"type": "token_count", "info": {"rate_limits": {
                         "primary": {"used_percent": 200, "window_minutes": 300},
                         "secondary": {"used_percent": 10, "window_minutes": 60}
                     }}}}
            (root / "session.jsonl").write_text(json.dumps(event) + "\n")
            result = read_budgets(root)
        self.assertFalse(result["five_hour"]["available"])
        self.assertFalse(result["weekly"]["available"])


if __name__ == "__main__":
    unittest.main()
