import unittest
from gway_agent_ops.executor import build_issue_prompt, ExecutionError


class PromptPolicyTests(unittest.TestCase):
    def issue(self, labels, body="Please change all files"):
        return {"repository": "arthexis/gway-agent-ops", "number": 58,
                "title": "Read-only smoke", "body": body, "labels": labels}

    def test_default_read_only_even_when_body_requests_changes(self):
        prompt = build_issue_prompt(self.issue(["gway-001"]))
        self.assertIn("READ-ONLY DEFAULT", prompt)
        self.assertNotIn("Make appropriate code changes", prompt)

    def test_trusted_read_only_label(self):
        prompt = build_issue_prompt(self.issue(["gway-001", "agentops:read-only"]))
        self.assertIn("READ-ONLY INVESTIGATION", prompt)
        self.assertIn("Do not modify files", prompt)

    def test_trusted_implementation_label(self):
        prompt = build_issue_prompt(self.issue(["gway-001", "agentops:implementation"]))
        self.assertIn("IMPLEMENTATION", prompt)
        self.assertIn("Make appropriate code changes", prompt)

    def test_invalid_labels_fail_closed(self):
        with self.assertRaises(ExecutionError):
            build_issue_prompt(self.issue("agentops:implementation"))


if __name__ == "__main__":
    unittest.main()
