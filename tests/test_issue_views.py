"""Trusted cross-repository issue listing and pagination contract."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from gway_agent_ops.dispatch import admission
from gway_agent_ops.github_issues import IssueRejected
from gway_agent_ops.issue_views import list_issues
from gway_agent_ops.github_search import search_issues
from gway_agent_ops.reporting import _key


def issue(repo="arthexis/ocpp-reports", number=211, comments=4, label="gway-001"):
    return {"number": number, "state": "open",
            "repository_url": f"https://api.github.com/repos/{repo}",
            "user": {"login": "arthexis"}, "title": "Trusted field test",
            "comments": comments, "labels": [{"name": label}]}


def search_runner(pages, *, fail=False):
    def runner(argv, **kwargs):
        assert argv[:2] == ["gh", "api"] and argv[2].startswith("search/issues?")
        assert kwargs["timeout"] == 30
        assert "shell" not in kwargs and "GH_TOKEN" not in kwargs["env"]
        query = parse_qs(urlparse("https://api.github.com/" + argv[2]).query)
        number = int(query["page"][0])
        assert "author:arthexis" in query["q"][0] and "is:issue" in query["q"][0]
        payload = pages[number - 1] if number <= len(pages) else pages[-1]
        return subprocess.CompletedProcess(argv, 1 if fail else 0, json.dumps(payload), "")
    return runner


def page(items, total=None, incomplete=False):
    return {"total_count": len(items) if total is None else total,
            "incomplete_results": incomplete, "items": items}


class IssueProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "dispatch"

    def view(self, comments=4):
        return list_issues(state_dir=self.state, runner=search_runner([page([issue(comments=comments)])]))

    def test_cross_repository_trust_and_legacy_list_ignored(self):
        items = [issue(), issue(repo="other/private-repo", number=12)]
        rows = list_issues(allowed_repositories=("some/other",),
                           state_dir=self.state, runner=search_runner([page(items)]))
        self.assertEqual([(r["repository"], r["number"]) for r in rows],
                         [("arthexis/ocpp-reports", 211), ("other/private-repo", 12)])
        self.assertTrue(all(r["eligible"] for r in rows))

    def test_history_sent_and_attended(self):
        self.assertEqual(self.view()[0]["state"], "open")
        with admission("arthexis/ocpp-reports#211", "2026-11-12T20:00:00Z",
                       state_dir=self.state, comment_baseline=4):
            pass
        self.assertEqual(self.view()[0]["state"], "sent")
        self.assertEqual(self.view(5)[0]["state"], "attended")
        (self.state / "delivered").mkdir()
        (self.state / "delivered" / (_key("arthexis/ocpp-reports#211",
            "2026-11-12T20:00:00.000000Z") + ".json")).write_text(
                json.dumps({"status": "finished", "exit_code": 0,
                            "marker": "agent-dispatch:" + _key(
                                "arthexis/ocpp-reports#211", "2026-11-12T20:00:00.000000Z")}))
        self.assertEqual(self.view(6)[0]["state"], "attended")

    def test_no_issues_is_empty_not_error(self):
        self.assertEqual(list_issues(state_dir=self.state,
                         runner=search_runner([page([])])), [])

    def test_pagination_fails_if_partial(self):
        items = [issue(number=i + 1) for i in range(100)]
        with self.assertRaisesRegex(IssueRejected, "issue_search_incomplete"):
            search_issues(label="gway-001", runner=search_runner([
                page(items, total=301)]))
        with self.assertRaisesRegex(IssueRejected, "issue_search_incomplete"):
            search_issues(label="gway-001", runner=search_runner([
                page([issue()], total=2, incomplete=True)]))
        with self.assertRaisesRegex(IssueRejected, "issue_search_incomplete"):
            search_issues(label="gway-001", runner=search_runner([
                page([issue()], total=2), page([], total=2)]))

    def test_two_pages_cannot_deduplicate_silently(self):
        first = [issue(number=i + 1) for i in range(100)]
        second = [issue(repo="other/private-repo", number=201)]
        rows = search_issues(label="gway-001", runner=search_runner([
            page(first, total=101), page(second, total=101)]))
        self.assertEqual(len(rows), 101)

    def test_search_fails_closed_for_untrusted_or_malformed_results(self):
        bad = [
            {**issue(), "user": {"login": "attacker"}},
            {**issue(), "pull_request": {}},
            {**issue(), "state": "closed"},
            {**issue(), "labels": []},
            {**issue(), "repository_url": "https://api.github.com/repos/../unsafe"},
            {**issue(), "comments": -1},
        ]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(IssueRejected):
                search_issues(label="gway-001",
                              runner=search_runner([page([item])]))
        for p in ({"items": []}, [], None):
            with self.subTest(payload=p), self.assertRaises(IssueRejected):
                search_issues(label="gway-001",
                              runner=search_runner([p]))
        with self.assertRaises(IssueRejected):
            search_issues(label="gway-001", runner=search_runner([page([issue()])], fail=True))

    def test_only_bounded_known_labels_and_creator(self):
        with self.assertRaisesRegex(IssueRejected, "invalid_issue_search"):
            search_issues(label="unknown", runner=search_runner([page([])]))
        with self.assertRaisesRegex(IssueRejected, "invalid_issue_search"):
            search_issues(label="gway-001", creator="attacker",
                          runner=search_runner([page([])]))


if __name__ == "__main__":
    unittest.main()
