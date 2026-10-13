import json
import tempfile
import unittest
from pathlib import Path
from gway_agent_ops.observation import observe
from gway_agent_ops.reporting import enqueue


class ObservationTests(unittest.TestCase):
    def test_unknown_accepted_and_finished(self):
        issue = "arthexis/example#3"
        at = "2026-10-13T15:00:00Z"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(observe(issue, at, state_dir=root)["state"], "unknown")
            (root / "accepted.jsonl").write_text(json.dumps({
                "issue": issue, "at": "2026-10-13T15:00:00.000000Z"}) + "\n")
            self.assertEqual(observe(issue, at, state_dir=root)["state"], "accepted")
            enqueue(issue, "2026-10-13T15:00:00.000000Z", state_dir=root,
                    status="finished", exit_code=0)
            result = observe(issue, at, state_dir=root)
            self.assertEqual(result["state"], "finished")
            self.assertEqual(result["exit_code"], 0)
            from gway_agent_ops.reporting import _key
            outbox = root / "outbox" / (_key(issue, "2026-10-13T15:00:00.000000Z") + ".json")
            delivered = root / "delivered"
            delivered.mkdir()
            (delivered / outbox.name).write_text(json.dumps({
                "status": "finished", "exit_code": 0, "marker": "test"}))
            outbox.unlink()
            observed = observe(issue, at, state_dir=root)
            self.assertEqual(observed["state"], "finished")
            self.assertTrue(observed["reported"])

    def test_rejects_invalid_issue(self):
        with self.assertRaises(ValueError):
            observe("owner/repo/pull/2", "2026-10-13T15:00:00Z")


if __name__ == "__main__":
    unittest.main()
