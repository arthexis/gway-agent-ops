"""Read-only prompt preview shares the exact executor prompt builder."""
from unittest.mock import Mock
import unittest

from gway_agent_ops.executor import build_issue_prompt, preview, ExecutionError
from gway_agent_ops.github_issues import IssueRejected


ISSUE = {"repository": "arthexis/example", "number": 12,
         "title": "Fix display", "body": "Untrusted body text",
         "url": "https://github.com/arthexis/example/issues/12"}


class PreviewTests(unittest.TestCase):
    def test_preview_uses_execution_prompt(self):
        resolver = Mock(return_value=ISSUE)
        result = preview("arthexis/example#12",
                         allowed_repositories=("arthexis/example",), resolver=resolver)
        self.assertEqual(result["prompt"], build_issue_prompt(ISSUE))
        self.assertIn("<issue-body>", result["prompt"])
        resolver.assert_called_once_with("arthexis/example#12",
                                         allowed_repositories=("arthexis/example",))

    def test_missing_allowlist_fails_closed_without_resolution(self):
        resolver = Mock()
        with self.assertRaises(ExecutionError):
            preview("arthexis/example#12", allowed_repositories=(), resolver=resolver)
        resolver.assert_not_called()

    def test_rejected_issue_cannot_be_previewed(self):
        resolver = Mock(side_effect=IssueRejected("repository_not_allowed"))
        with self.assertRaises(IssueRejected):
            preview("arthexis/example#12",
                    allowed_repositories=("arthexis/example",), resolver=resolver)


if __name__ == "__main__":
    unittest.main()
