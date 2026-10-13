"""End-to-end installed CLI contracts without live credentials."""
import json
import os
import subprocess
import sys
import unittest


class InstalledCliTests(unittest.TestCase):
    @staticmethod
    def call(*args):
        return subprocess.run(["agent", *args], text=True, capture_output=True,
                              timeout=15, env={**os.environ, "GITHUB_TOKEN": "",
                                               "CODEX_HOME": "/nonexistent-codex-home"})

    def test_help_and_version(self):
        for args in (("--help",), ("--version",), ("issue", "--help"),
                     ("issues", "--help"), ("status", "--help"),
                     ("adopt", "--help"), ("hook", "--help")):
            with self.subTest(args=args):
                result = self.call(*args)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_contracts_without_credentials_or_execution(self):
        commands = (("issue", "arthexis/gway-lcd-sound#19", "--json"),
                    ("issues", "--json"), ("status", "--json"),
                    ("hook", "Stop", "--json"))
        for args in commands:
            with self.subTest(args=args):
                result = self.call(*args)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(len(result.stdout.splitlines()), 1)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["schema_version"], 1)
                self.assertEqual(payload["status"], "not_implemented")
                self.assertEqual(payload["error"]["code"], "not_implemented")

    def test_invalid_arguments_never_dispatch(self):
        for args in (("issue", "invalid"), ("issue", "other/repo#0"),
                     ("hook", "Unknown"), ("issue", "arthexis/repo#12", "--bad")):
            with self.subTest(args=args):
                result = self.call(*args)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(result.stdout)
