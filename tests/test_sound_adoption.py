"""Codex S4 notify routing and opt-in adoption."""
import json
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from gway_agent_ops.sound_notify import dispatch
from gway_agent_ops.adopt import adopt


class SoundAdoptionTests(unittest.TestCase):
    def test_only_supported_completion_invokes_sound(self):
        calls = []
        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0)
        result = dispatch(json.dumps({"type": "agent-turn-complete",
                                      "last-assistant-message": "private content"}),
                          runner=runner)
        self.assertTrue(result["played"])
        self.assertEqual(calls[0][0], ["/usr/local/bin/sound", "--event", "Stop", "--source", "codex"])
        self.assertNotIn("private content", str(calls))
        calls.clear()
        self.assertFalse(dispatch('{"type":"approval-requested"}', runner=runner)["played"])
        self.assertFalse(calls)

    def test_malformed_or_huge_payload_rejected(self):
        for data in ("nonsense", "[]", '{"type":42}', "x" * 65537):
            with self.assertRaises(ValueError):
                dispatch(data)

    def test_sound_adoption_idempotent_and_not_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "operator", root / "agent"
            source.mkdir()
            (source / "config.toml").write_text('model = "gpt-5"\n')
            result = adopt(source=source, destination=target, with_sound=True)
            self.assertTrue(result["sound_enabled"])
            config = (target / "config.toml").read_text()
            self.assertEqual(tomllib.loads(config)["notify"], ["agent", "notify"])
            self.assertEqual(adopt(source=source, destination=target,
                                   with_sound=True)["files"]["config.toml"], "unchanged")
            self.assertEqual(config.count("notify ="), 1)
            with self.assertRaises(FileExistsError):
                adopt(source=source, destination=target)
