"""A3.5 trusted issue projection and dispatch baseline tests."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from gway_agent_ops.dispatch import admission
from gway_agent_ops.issue_views import list_issues
from gway_agent_ops.github_issues import IssueRejected
from gway_agent_ops.reporting import _key


class IssueProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "dispatch"
        self.repo = "arthexis/example"

    def runner(self, comments=4, *, fail=False):
        def call(argv, **kwargs):
            self.assertEqual(argv[:4], ["gh", "api", "--paginate", "--slurp"])
            items = [
                {"number": 12, "state": "open", "user": {"login": "arthexis"},
                 "title": "Trusted", "comments": comments},
                {"number": 13, "state": "open", "user": {"login": "other"},
                 "title": "Other", "comments": 0},
                {"number": 14, "state": "closed", "user": {"login": "arthexis"},
                 "title": "Closed", "comments": 0},
                {"number": 15, "state": "open", "user": {"login": "arthexis"},
                 "title": "PR", "comments": 0, "pull_request": {}},
            ]
            return subprocess.CompletedProcess(argv, 1 if fail else 0,
                                               json.dumps([items]), "")
        return call

    def project(self, comments=4):
        return list_issues(allowed_repositories=(self.repo,), state_dir=self.state,
                           runner=self.runner(comments))

    def test_open_sent_attended_and_total_comments(self):
        self.assertEqual(self.project()[0]["state"], "open")
        with admission(self.repo + "#12", "2026-10-13T01:00:00Z",
                       state_dir=self.state, comment_baseline=4):
            pass
        self.assertEqual(self.project()[0]["state"], "sent")
        attended = self.project(5)
        self.assertEqual(attended[0]["state"], "attended")
        self.assertEqual(attended[0]["comments"], 5)
        self.assertEqual(attended[0]["url"], "https://github.com/arthexis/example/issues/12")
        (self.state / "delivered").mkdir()
        (self.state / "delivered" / (_key(self.repo + "#12", "2026-10-13T01:00:00.000000Z") + ".json")).write_text(
            json.dumps({"status": "finished", "exit_code": 0}))
        with admission(self.repo + "#12", "2026-10-13T02:00:00Z",
                       state_dir=self.state, comment_baseline=5):
            pass
        self.assertEqual(self.project(5)[0]["state"], "sent")
        self.assertEqual(self.project(6)[0]["state"], "attended")

    def test_trust_and_closed_pr_filters(self):
        self.assertEqual([r["number"] for r in self.project()], [12])

    def test_legacy_record_does_not_fabricate_attendance(self):
        with admission(self.repo + "#12", "2026-10-13T01:00:00Z", state_dir=self.state):
            pass
        self.assertEqual(self.project(100)[0]["state"], "sent")

    def test_network_errors_fail_closed(self):
        with self.assertRaises(IssueRejected):
            list_issues(allowed_repositories=(self.repo,), state_dir=self.state,
                        runner=self.runner(fail=True))
        with self.assertRaises(IssueRejected):
            list_issues(allowed_repositories=(), state_dir=self.state,
                        runner=self.runner())
