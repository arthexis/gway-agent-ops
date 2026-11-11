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
                "status": "finished", "exit_code": 0,
                "marker": "agent-dispatch:" + _key(issue, "2026-10-13T15:00:00.000000Z")}))
            outbox.unlink()
            observed = observe(issue, at, state_dir=root)
            self.assertEqual(observed["state"], "finished")
            self.assertTrue(observed["reported"])

    def test_crash_window_prefers_durable_delivered_receipt(self):
        issue = "arthexis/example#4"
        at = "2026-10-13T15:00:00.000000Z"
        from gway_agent_ops.reporting import _key, enqueue
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.joinpath("accepted.jsonl").write_text(
                json.dumps({"issue": issue, "at": at}) + "\n")
            enqueue(issue, at, state_dir=root, status="finished", exit_code=0,
                    findings_state="validated",
                    findings={"summary": "The offline observation test completed.",
                              "findings": ["Checked persistence and recovery."],
                              "evidence": ["Synthetic durable receipt fixture."],
                              "limitations": "Only synthetic data inspected."})
            key = _key(issue, at) + ".json"
            delivered = root / "delivered"
            delivered.mkdir()
            delivered.joinpath(key).write_text(json.dumps(
                {"status": "finished", "exit_code": 0,
                 "marker": "agent-dispatch:" + _key(issue, at),
                 "findings_state": "validated"}))
            observed = observe(issue, at, state_dir=root)
            self.assertEqual(observed["state"], "finished")
            self.assertEqual(observed["findings_state"], "validated")
            self.assertTrue(observed["delivery_confirmed"])
            self.assertTrue(observed["reported"])
            self.assertTrue((root / "outbox" / key).exists())

    def test_corrupt_acceptance_journal_never_becomes_unknown_or_terminal(self):
        issue = "arthexis/example#3"
        at = "2026-10-13T15:00:00Z"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "accepted.jsonl"
            for entry in ("[]\n", "null\n", '{"issue":123,"at":"x"}\n',
                          '{"issue":"arthexis/example#3","at":"2026-10-13T15:00:00.000000Z"}\n[]\n'):
                with self.subTest(entry=entry):
                    ledger.write_text(entry)
                    with self.assertRaisesRegex(ValueError, "invalid acceptance journal"):
                        observe(issue, at, state_dir=root)

    def test_broken_acceptance_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "accepted.jsonl").symlink_to("missing-file")
            with self.assertRaisesRegex(ValueError, "unsafe dispatch journal"):
                observe("arthexis/example#3", "2026-10-13T15:00:00Z",
                        state_dir=root)

    def test_rejects_invalid_issue(self):
        with self.assertRaises(ValueError):
            observe("owner/repo/pull/2", "2026-10-13T15:00:00Z")


if __name__ == "__main__":
    unittest.main()
