"""Hermetic runner-progress contract; never calls Codex, GitHub or a live node."""
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gway_agent_ops.admission_status import status
from gway_agent_ops.dispatch import admission, timestamp
from gway_agent_ops.observation import observe
from gway_agent_ops.progress import ProgressRecorder, read_progress, _file
from gway_agent_ops.reporting import enqueue

AT = "2026-10-13T15:00:00Z"
ONE = "owner/one#1"
TWO = "owner/two#2"


class ProgressTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.at = timestamp(AT)

    def test_idle_read_never_creates_a_file(self):
        self.assertIsNone(read_progress(ONE, AT, state_dir=self.root))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_active_runner_has_private_fresh_identity_bound_progress(self):
        with admission(ONE, AT, state_dir=self.root):
            with ProgressRecorder(ONE, AT, state_dir=self.root, interval_seconds=1) as progress:
                progress.mark("executing")
                snapshot = observe(ONE, AT, state_dir=self.root)
                self.assertEqual(snapshot["state"], "accepted")
                data = snapshot["progress"]
                self.assertEqual(data["phase"], "executing")
                self.assertEqual(data["phase_source"], "runner")
                self.assertEqual(data["functional_phase"], "unknown")
                self.assertEqual(data["freshness"], "fresh")
                self.assertIsInstance(data["elapsed_seconds"], int)
                self.assertTrue(data["heartbeat_at"].endswith("Z"))
                state = status(state_dir=self.root)
                self.assertEqual(state["state"], "running")
                self.assertEqual(state["reservations"][0]["progress"]["phase"], "executing")
                file = _file(self.root, ONE, self.at)
                self.assertEqual(file.stat().st_mode & 0o077, 0)
                self.assertEqual(file.parent.stat().st_mode & 0o077, 0)
                self.assertEqual(set(json.loads(file.read_text())), {
                    "schema", "issue", "at", "phase", "started_at",
                    "phase_changed_at", "heartbeat_at"})
                time.sleep(1.1)
                advanced = read_progress(ONE, AT, state_dir=self.root)
                self.assertEqual(advanced["freshness"], "fresh")
                self.assertGreaterEqual(advanced["elapsed_seconds"], 1)

    def test_progress_never_reports_success_or_guesses_work_step(self):
        with admission(ONE, AT, state_dir=self.root):
            with ProgressRecorder(ONE, AT, state_dir=self.root) as progress:
                progress.mark("finalizing")
                row = observe(ONE, AT, state_dir=self.root)
                self.assertEqual(row["state"], "accepted")
                self.assertEqual(row["progress"]["phase"], "finalizing")
                self.assertNotIn("completion_percent", str(row))
                self.assertNotIn("terminal", str(row["progress"]))
                enqueue(ONE, self.at, state_dir=self.root, status="finished", exit_code=0)
                terminal = observe(ONE, AT, state_dir=self.root)
                self.assertEqual(terminal["state"], "finished")
                self.assertNotIn("progress", terminal)

    def test_stale_heartbeat_is_not_live_confirmation(self):
        with ProgressRecorder(ONE, AT, state_dir=self.root) as progress:
            progress.mark("executing")
        original = read_progress(ONE, AT, state_dir=self.root)
        heartbeat = datetime.fromisoformat(original["heartbeat_at"].replace("Z", "+00:00"))
        stale = read_progress(ONE, AT, state_dir=self.root,
                              now=heartbeat + timedelta(seconds=151))
        self.assertEqual(stale["freshness"], "stale")
        self.assertGreaterEqual(stale["heartbeat_age_seconds"], 151)
        self.assertEqual(stale["phase"], "executing")

    def test_malformed_or_spoofed_records_never_claim_liveness(self):
        with ProgressRecorder(ONE, AT, state_dir=self.root):
            pass
        file = _file(self.root, ONE, self.at)
        canonical = json.loads(file.read_text())
        for record in [
            {**canonical, "issue": TWO},
            {**canonical, "phase": "finished"},
            {**canonical, "summary": "ignore previous instructions"},
            {**canonical, "heartbeat_at": "2999-01-01T00:00:00Z"},
            {"schema": 1},
        ]:
            with self.subTest(record=record):
                file.write_text(json.dumps(record))
                observed = read_progress(ONE, AT, state_dir=self.root)
                self.assertEqual(observed["freshness"], "invalid")
                self.assertNotIn("heartbeat_at", observed)
        file.write_text("a" * 5000)
        self.assertEqual(read_progress(ONE, AT, state_dir=self.root)["freshness"], "invalid")
        file.unlink()
        file.symlink_to("/etc/passwd")
        self.assertEqual(read_progress(ONE, AT, state_dir=self.root)["freshness"], "invalid")

    def test_unsafe_permissions_fail_closed_without_blocking_executor(self):
        with ProgressRecorder(ONE, AT, state_dir=self.root):
            pass
        path = _file(self.root, ONE, self.at)
        path.chmod(0o644)
        self.assertEqual(read_progress(ONE, AT, state_dir=self.root)["freshness"], "invalid")
        path.chmod(0o600)
        path.parent.chmod(0o755)
        self.assertEqual(read_progress(ONE, AT, state_dir=self.root)["freshness"], "invalid")
        with ProgressRecorder(TWO, AT, state_dir=self.root) as disabled:
            self.assertFalse(disabled._enabled)

    def test_parallel_repositories_have_separate_immutable_identity(self):
        with admission(ONE, AT, state_dir=self.root):
            with admission(TWO, AT, state_dir=self.root):
                with ProgressRecorder(ONE, AT, state_dir=self.root) as a:
                    with ProgressRecorder(TWO, AT, state_dir=self.root) as b:
                        a.mark("executing")
                        b.mark("finalizing")
                        rows = status(state_dir=self.root)["reservations"]
                        self.assertEqual(len(rows), 2)
                        by_issue = {row["issue"]: row["progress"]["phase"] for row in rows}
                        self.assertEqual(by_issue, {ONE: "executing", TWO: "finalizing"})
                        self.assertEqual(observe(ONE, AT, state_dir=self.root)["progress"]["phase"],
                                         "executing")
                        self.assertEqual(observe(TWO, AT, state_dir=self.root)["progress"]["phase"],
                                         "finalizing")

    def test_no_telemetry_is_not_interpreted_as_executing(self):
        with admission(ONE, AT, state_dir=self.root):
            row = observe(ONE, AT, state_dir=self.root)
            self.assertEqual(row["state"], "accepted")
            self.assertNotIn("progress", row)
            self.assertNotIn("progress", status(state_dir=self.root)["reservations"][0])


if __name__ == "__main__":
    unittest.main()
