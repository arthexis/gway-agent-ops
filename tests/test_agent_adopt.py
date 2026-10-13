"""A2.1 inspection is non-mutating and does not expose secrets."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gway_agent_ops.adopt import inspect
from gway_agent_ops.cli import main


class AdoptInspectionTests(unittest.TestCase):
    def test_metadata_without_reading_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.toml").write_text("secret-config-value")
            (root / "auth.json").write_text("secret-auth-value")
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            with patch.dict(os.environ, {"OPENAI_API_KEY": "secret-api-value"}):
                result = inspect(codex_home=root, executable="/usr/bin/codex")
            payload = json.dumps(result)
            for secret in ("secret-config-value", "secret-auth-value", "secret-api-value"):
                self.assertNotIn(secret, payload)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
            self.assertTrue(result["config"]["readable"])
            self.assertEqual(result["adoption"]["mode"], "copy")
            self.assertFalse(result["adoption"]["changes_made"])

    def test_no_auth_does_not_report_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                data = inspect(codex_home=Path(folder), executable="")
            self.assertEqual(data["authentication"]["status"], "unavailable")
            self.assertFalse(data["codex_executable"]["available"])

    def test_check_json_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"CODEX_HOME": folder, "OPENAI_API_KEY": ""}):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(["adopt", "--check", "--json"]), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["command"], "adopt")
            self.assertEqual(payload["status"], "ok")
            self.assertFalse(payload["data"]["adoption"]["changes_made"])
