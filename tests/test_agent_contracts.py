"""A1 JSON protocol validation: no inferred quota values or issue lifecycle."""
import contextlib
import io
import json
import unittest
from gway_agent_ops.contracts import (
    envelope, status_data, budget_window, issue_row, json_line, unavailable_budget)
from gway_agent_ops.cli import main, NOT_IMPLEMENTED


class ContractTests(unittest.TestCase):
    def test_envelope_and_json_failure(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["status", "--json"]), NOT_IMPLEMENTED)
        output = stdout.getvalue().strip().splitlines()
        self.assertEqual(len(output), 1)
        payload = json.loads(output[0])
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["command"], "status")
        self.assertEqual(payload["status"], "not_implemented")
        self.assertIsNone(payload["data"])
        self.assertEqual(payload["error"]["code"], "not_implemented")

    def test_every_command_json_mode(self):
        cases = [["issue", "arthexis/repo#41", "--json"],
                 ["issues", "--json"], ["adopt", "--bad-option", "--json"],
                 ["hook", "SessionStart", "--json"]]
        for args in cases:
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(args), NOT_IMPLEMENTED)
                self.assertEqual(json.loads(stdout.getvalue())["status"], "not_implemented")

    def test_budget_windows_independently_unavailable(self):
        available = budget_window(remaining_percent=0, resets_at=None,
                                  source="provider", observed_at="2026-10-13T05:00:00Z")
        data = status_data(five_hour=available)
        self.assertTrue(data["budget"]["five_hour"]["available"])
        self.assertEqual(data["budget"]["five_hour"]["remaining_percent"], 0)
        self.assertFalse(data["budget"]["weekly"]["available"])
        self.assertIsNone(data["budget"]["weekly"]["remaining_percent"])
        self.assertFalse(unavailable_budget()["available"])
        for value in (-1, 101, True):
            with self.assertRaises(ValueError):
                budget_window(remaining_percent=value, source="provider",
                              observed_at="2026-10-13T05:00:00Z")
        with self.assertRaises(ValueError):
            budget_window(remaining_percent=40)

    def test_issue_states_counts_and_canonical_urls(self):
        row = issue_row(repository="arthexis/repo", number=41, title="Example",
                        state="sent", comments=7, sent_at="2026-10-13T05:00:00Z")
        self.assertEqual(row["url"], "https://github.com/arthexis/repo/issues/41")
        self.assertEqual(row["comments"], 7)
        for state in ("queued", "succeeded"):
            with self.assertRaises(ValueError):
                issue_row(repository="arthexis/repo", number=41, title="Example",
                          state=state, comments=0)
        with self.assertRaises(ValueError):
            issue_row(repository="arthexis/repo", number=41, title="Example",
                      state="sent", comments=0)

    def test_wire_json_is_one_line(self):
        payload = envelope("issues", "ok", {"issues": []})
        self.assertEqual(json.loads(json_line(payload)), payload)
        self.assertNotIn("\n", json_line(payload))
