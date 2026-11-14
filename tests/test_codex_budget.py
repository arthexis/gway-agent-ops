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



FIXED_NOW = datetime(2026, 11, 14, 5, 0, tzinfo=timezone.utc)


class FrozenBudgetClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


class SessionRateLimitsRegressionTests(unittest.TestCase):
    """Synthetic Codex JSONL only: no Codex invocation, network, or live credentials."""

    @staticmethod
    def _window(used, minutes, resets_at=1795000000):
        return {"used_percent": used, "window_minutes": minutes, "resets_at": resets_at}

    @staticmethod
    def _event(at, *, direct=None, legacy=None):
        payload = {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 123}}}
        if direct is not None:
            payload["rate_limits"] = direct
        if legacy is not None:
            payload["info"]["rate_limits"] = legacy
        return {"type": "event_msg", "timestamp": at.isoformat(), "payload": payload}

    def _read(self, events):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
            with patch("gway_agent_ops.codex_budget.datetime", FrozenBudgetClock):
                return read_budgets(root)

    def test_reads_current_direct_payload_windows(self):
        result = self._read([self._event(FIXED_NOW, direct={
            "primary": self._window(0, 300),
            "secondary": self._window(16, 10080)})])
        self.assertEqual(result["five_hour"]["remaining_percent"], 100)
        self.assertEqual(result["weekly"]["remaining_percent"], 84)
        self.assertTrue(result["five_hour"]["available"])
        self.assertTrue(result["weekly"]["available"])
        self.assertEqual(result["five_hour"]["source"], "codex_session_rate_limits")
        self.assertEqual(result["weekly"]["observed_at"], "2026-11-14T05:00:00Z")
        self.assertIsNotNone(result["weekly"]["resets_at"])

    def test_retains_legacy_info_rate_limits(self):
        result = self._read([self._event(FIXED_NOW, legacy={
            "primary": self._window(25, 300),
            "secondary": self._window(60, 10080)})])
        self.assertEqual(result["five_hour"]["remaining_percent"], 75)
        self.assertEqual(result["weekly"]["remaining_percent"], 40)

    def test_direct_payload_has_precedence_when_both_exist(self):
        result = self._read([self._event(FIXED_NOW, direct={
            "primary": self._window(40, 300)}, legacy={
            "primary": self._window(90, 300)})])
        self.assertEqual(result["five_hour"]["remaining_percent"], 60)
        self.assertFalse(result["weekly"]["available"])

    def test_nondict_direct_falls_back_to_legacy(self):
        result = self._read([self._event(FIXED_NOW, direct="invalid", legacy={
            "primary": self._window(12, 300)})])
        self.assertEqual(result["five_hour"]["remaining_percent"], 88)

    def test_validates_each_window_independently(self):
        result = self._read([self._event(FIXED_NOW, direct={
            "primary": self._window(45, 300),
            "secondary": self._window(10, 60)})])
        self.assertEqual(result["five_hour"]["remaining_percent"], 55)
        self.assertFalse(result["weekly"]["available"])
        self.assertIsNone(result["weekly"]["remaining_percent"])

    def test_newest_valid_timestamp_wins_across_formats(self):
        from datetime import timedelta
        newer = self._event(FIXED_NOW - timedelta(minutes=5), legacy={
            "primary": self._window(30, 300)})
        older = self._event(FIXED_NOW - timedelta(minutes=25), direct={
            "primary": self._window(70, 300), "secondary": self._window(40, 10080)})
        result = self._read([newer, older])
        self.assertEqual(result["five_hour"]["remaining_percent"], 70)
        self.assertEqual(result["weekly"]["remaining_percent"], 60)

    def test_invalid_json_values_stale_and_future_are_unavailable(self):
        from datetime import timedelta
        stale = self._event(FIXED_NOW - timedelta(hours=2), direct={
            "primary": self._window(10, 300)})
        future = self._event(FIXED_NOW + timedelta(minutes=1), direct={
            "secondary": self._window(10, 10080)})
        bad_percent = self._event(FIXED_NOW, direct={
            "primary": self._window(101, 300), "secondary": self._window(40, 60)})
        result = self._read([[], None, stale, future, bad_percent])
        self.assertFalse(result["five_hour"]["available"])
        self.assertFalse(result["weekly"]["available"])

    def test_missing_or_malformed_reset_is_not_invented(self):
        result = self._read([self._event(FIXED_NOW, direct={
            "primary": self._window(20, 300, resets_at="not-a-timestamp")})])
        self.assertEqual(result["five_hour"]["remaining_percent"], 80)
        self.assertIsNone(result["five_hour"]["resets_at"])

if __name__ == "__main__":
    unittest.main()
