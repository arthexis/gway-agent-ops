"""Free-form requests are validated before admission or execution."""
import unittest
from gway_agent_ops.prompt_contract import validate_prompt_request


class PromptContractTests(unittest.TestCase):
    def setUp(self):
        self.kwargs = {"repository": "arthexis/demo", "prompt": "Inspect tests",
                       "at": "2026-10-13T18:00:00Z",
                       "allowed_repositories": ("arthexis/demo",)}

    def test_stable_identity_and_digest(self):
        a = validate_prompt_request(**self.kwargs)
        b = validate_prompt_request(**self.kwargs)
        self.assertEqual(a, b)
        self.assertEqual(len(a["request_id"]), 64)

    def test_fail_closed(self):
        for patch in ({"repository": "../demo"}, {"repository": "other/repo"},
                      {"prompt": ""}, {"prompt": " "}, {"prompt": "x" * 16385},
                      {"prompt": "bad\\x00input"}, {"allowed_repositories": ()},
                      {"at": "not-a-time"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_prompt_request(**(self.kwargs | patch))
