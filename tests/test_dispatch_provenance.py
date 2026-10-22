"""Dispatch provenance metadata must not alter submission identity or acceptance."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from gway_agent_ops.observation import observe


class ProvenanceTests(unittest.TestCase):
    def test_requested_marker_provenance_and_legacy_compatibility(self):
        ref = "arthexis/gway-bastion#2"
        at = "2026-10-21T20:17:23.000000Z"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "requests"
            folder.mkdir()
            key = hashlib.sha256((ref + "\n" + at).encode()).hexdigest()
            marker = folder / (key + ".json")
            marker.write_text(json.dumps({
                "issue": ref, "at": at, "state": "requested",
                "source": "remote-api", "trigger": "authenticated-agent-issue"
            }))
            result = observe(ref, at, state_dir=root)
            self.assertEqual(result["state"], "requested")
            self.assertEqual(result["source"], "remote-api")
            self.assertEqual(result["trigger"], "authenticated-agent-issue")
            marker.write_text(json.dumps({"issue": ref, "at": at, "state": "requested"}))
            legacy = observe(ref, at, state_dir=root)
            self.assertEqual(legacy["state"], "requested")
            self.assertNotIn("source", legacy)
