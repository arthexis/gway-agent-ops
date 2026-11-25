"""A3.1 issue lookup must reject untrusted content before dispatch."""
import json
import subprocess
import unittest

from gway_agent_ops.github_issues import IssueRejected, resolve


REPO = "arthexis/gway-lcd-sound"


def payload(**overrides):
    data = {"number": 19, "state": "open", "user": {"login": "arthexis"},
            "title": "Generalize sound hook", "body": "Build it",
            "comments": 4, "repository_url": f"https://api.github.com/repos/{REPO}"}
    data["labels"] = [{"name": "gway-001"}]
    data.update(overrides)
    return data


def runner(body, code=0):
    def invoke(argv, **kwargs):
        assert argv == ["gh", "api", "--hostname", "github.com", f"repos/{REPO}/issues/19"]
        assert kwargs["timeout"] == 20
        assert "shell" not in kwargs
        assert "GH_TOKEN" not in kwargs["env"]
        return subprocess.CompletedProcess(argv, code, json.dumps(body), "")
    return invoke


class GithubIssueTests(unittest.TestCase):
    def test_resolve_trusted_issue(self):
        result = resolve(f"{REPO}#19", allowed_repositories=(REPO,), runner=runner(payload()))
        self.assertEqual(result["comments"], 4)
        self.assertEqual(result["url"], f"https://github.com/{REPO}/issues/19")
        self.assertEqual(result["body"], "Build it")

    def test_unlabeled_trusted_issue_is_accepted(self):
        result = resolve(f"{REPO}#19", runner=runner(payload(labels=[])))
        self.assertEqual(result["labels"], [])

    def test_missing_label_metadata_is_invalid(self):
        body = payload()
        body.pop("labels")
        with self.assertRaisesRegex(IssueRejected, "invalid_issue_labels"):
            resolve(f"{REPO}#19", allowed_repositories=(REPO,), runner=runner(body))

    def test_legacy_allowlist_does_not_reject_accessible_repo(self):
        result = resolve(f"{REPO}#19", allowed_repositories=("arthexis/other",),
                         runner=runner(payload()))
        self.assertEqual(result["repository"], REPO)

    def test_untrusted_creator_closed_issue_and_pr(self):
        for body, code in ((payload(user={"login": "other"}), "creator_not_allowed"),
                           (payload(state="closed"), "issue_not_open"),
                           (payload(pull_request={}), "not_an_issue"),
                           (payload(number=20), "issue_mismatch"),
                           (payload(repository_url="https://api.github.com/repos/other/repo"),
                            "repository_mismatch")):
            with self.subTest(code=code), self.assertRaisesRegex(IssueRejected, code):
                resolve(f"{REPO}#19", allowed_repositories=(REPO,), runner=runner(body))

    def test_no_fallback_for_bad_api_data(self):
        for body in ({}, [], payload(comments=None, user=None)):
            with self.subTest(body=body), self.assertRaises(IssueRejected):
                resolve(f"{REPO}#19", allowed_repositories=(REPO,), runner=runner(body))

    def test_api_failure(self):
        with self.assertRaisesRegex(IssueRejected, "github_unavailable"):
            resolve(f"{REPO}#19", allowed_repositories=(REPO,), runner=runner({}, 1))

    def test_repository_url_must_be_on_github_api_host(self):
        for url in (f"https://evil.example/repos/{REPO}",
                    f"https://api.github.com.evil.example/repos/{REPO}",
                    f"http://api.github.com/repos/{REPO}",
                    f"https://api.github.com/repos/{REPO}?redirect=1"):
            with self.subTest(url=url), self.assertRaisesRegex(IssueRejected, "repository_mismatch"):
                resolve(f"{REPO}#19", runner=runner(payload(repository_url=url)))
