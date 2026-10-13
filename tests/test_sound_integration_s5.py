"""S5 integration tests for Codex's external notify contract and isolated adoption."""
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from gway_agent_ops.adopt import adopt

ROOT = Path(__file__).resolve().parents[1]


class SoundEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "gway_agent_ops", *args],
                              cwd=ROOT, env={**os.environ, **(env or {})},
                              text=True, capture_output=True, timeout=10)

    def test_agent_owned_config_is_valid_and_no_operator_hook_is_copied(self):
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        (source / "config.toml").write_text(
            'model = "gpt-5"\nnotify = ["untrusted-program"]\n'
            '[mcp_servers.bad]\ncommand = "untrusted-program"\n')
        adopt(source=source, destination=target, with_sound=True)
        parsed = tomllib.loads((target / "config.toml").read_text())
        self.assertEqual(parsed["notify"], ["agent", "notify"])
        self.assertEqual(parsed["model"], "gpt-5")
        self.assertNotIn("mcp_servers", parsed)
        self.assertEqual(adopt(source=source, destination=target, with_sound=True)
                         ["files"]["config.toml"], "unchanged")

    def test_cli_ignores_unknown_notifications_and_rejects_bad_json(self):
        ignored = self.cli("notify", '{"type":"something-else"}', "--json")
        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        self.assertFalse(json.loads(ignored.stdout)["data"]["played"])
        invalid = self.cli("notify", "not-json", "--json")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertEqual(json.loads(invalid.stdout)["status"], "error")

    def test_notify_adapter_invokes_exact_source_event_with_bounded_timeout(self):
        from gway_agent_ops.sound_notify import dispatch
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0)

        payload = json.dumps({"type": "agent-turn-complete",
                              "last-assistant-message": "do not forward this"})
        result = dispatch(payload, runner=runner, sound_bin="/fake/sound")
        self.assertTrue(result["played"])
        self.assertEqual(calls[0][0],
                         ["/fake/sound", "--event", "Stop", "--source", "codex"])
        self.assertEqual(calls[0][1]["timeout"], 12)
        self.assertNotIn("do not forward this", str(calls))


if __name__ == "__main__":
    unittest.main()
