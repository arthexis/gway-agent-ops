import contextlib
import io
import unittest
from unittest.mock import patch
from gway_agent_ops.cli import main, parse_issue_ref, EVENTS, NOT_IMPLEMENTED


class AgentCliTests(unittest.TestCase):
    def test_refs(self):
        self.assertEqual(parse_issue_ref("arthexis/repo#41"), ("arthexis/repo", 41))
        self.assertEqual(parse_issue_ref("41", "arthexis/repo"), ("arthexis/repo", 41))
        for ref, repo in (("41", None), ("0", "arthexis/repo"),
                          ("arthexis/repo#0", None), ("x", None),
                          ("arthexis/repo#41;bad", None),
                          ("arthexis/repo#41", "different/repo")):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                parse_issue_ref(ref, repo)

    def test_commands_are_explicit_stubs(self):
        variants = [["hook", event] for event in EVENTS]
        for args in variants:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(args), NOT_IMPLEMENTED)
                if "--json" in args:
                    self.assertIn('"status":"not_implemented"', out.getvalue())
                    self.assertEqual(err.getvalue(), "")
                else:
                    self.assertIn("not implemented", err.getvalue())

    def test_report_flush_never_executes_agent(self):
        with patch("gway_agent_ops.reporting.flush", return_value={"sent": 1, "pending": 0}) as flush:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["report", "--json"]), 0)
            self.assertIn('"sent":1', out.getvalue())
            flush.assert_called_once_with()

    def test_submit_routing_denial(self):
        from gway_agent_ops.github_issues import IssueRejected
        with patch("gway_agent_ops.nonblocking.submit", side_effect=IssueRejected("target_label_missing")):
            with contextlib.redirect_stdout(io.StringIO()) as response:
                rc = main(["submit", "owner/repo#2", "--at", "2026-10-21T20:17:23Z", "--json"])
        self.assertEqual(rc, 1)
        self.assertIn("target_label_missing", response.getvalue())
    def test_bad_ref_exit_code(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            main(["issue", "invalid"])
        self.assertEqual(result.exception.code, 2)
