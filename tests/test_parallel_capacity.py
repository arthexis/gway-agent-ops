"""Adversarial capacity, idempotency and parallel-repository execution tests."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gway_agent_ops.admission_status import status
from gway_agent_ops.capacity import capacity, repo_lock, unresolved
from gway_agent_ops.dispatch import Busy, Duplicate, admission
from gway_agent_ops.nonblocking import submit
from gway_agent_ops.observation import observe

AT = "2026-10-13T15:00:00Z"


class CapacityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.patchers = [
            patch("gway_agent_ops.nonblocking.resolve", return_value={"labels": []}),
            patch("gway_agent_ops.nonblocking._launch_prerequisites",
                  return_value="/usr/bin/agent"),
            patch("gway_agent_ops.nonblocking._same_user_runtime_dir",
                  return_value="/run/user/1000"),
            patch("gway_agent_ops.capacity.os.cpu_count", return_value=4),
        ]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)

    def submit(self, ref, at=AT):
        return submit(ref, at, state_dir=self.root,
                      runner=Mock(return_value=Mock(returncode=0)))

    def test_three_independent_repositories_not_one_global_slot(self):
        for number in (1, 2, 3):
            result = self.submit(f"owner/repo{number}#1")
            self.assertEqual(result["state"], "requested")
        snapshot = status(state_dir=self.root)
        self.assertEqual(snapshot["capacity"], 3)
        self.assertEqual(snapshot["active_count"], 3)
        self.assertFalse(snapshot["available"])
        fourth = self.submit("owner/repo4#1")
        self.assertEqual(fourth["state"], "busy")
        self.assertEqual(fourth["reason"], "capacity_full")
        self.assertEqual(len(list((self.root / "requests").glob("*.json"))), 3)
        self.assertEqual(self.submit("owner/repo1#1")["state"], "already_requested")

    def test_one_repository_remains_isolated(self):
        self.assertEqual(self.submit("owner/repo1#1")["state"], "requested")
        self.assertTrue(status(state_dir=self.root)["available"])
        other = self.submit("owner/repo1#2")
        self.assertEqual(other["state"], "busy")
        self.assertEqual(other["reason"], "repository_active")
        self.assertEqual(self.submit("owner/repo2#1")["state"], "requested")
        with self.assertRaises(Busy):
            with admission("owner/repo1#9", "2026-10-13T15:01:00Z",
                           state_dir=self.root):
                pass

    def test_pending_reservation_is_counted_during_worker_admission(self):
        self.submit("owner/one#1")
        self.submit("owner/two#1")
        self.submit("owner/three#1")
        # A worker consuming its own request does not require a fourth slot.
        with admission("owner/two#1", AT, state_dir=self.root):
            self.assertEqual(status(state_dir=self.root)["active_count"], 3)
            with self.assertRaises(Busy):
                with admission("owner/four#1", AT, state_dir=self.root):
                    pass
        with self.assertRaises(Duplicate):
            with admission("owner/two#1", AT, state_dir=self.root):
                pass

    def test_capacity_scales_with_cpu(self):
        with patch("gway_agent_ops.capacity.os.cpu_count", return_value=2):
            self.assertEqual(capacity(), 1)
            self.assertEqual(self.submit("owner/a#1")["state"], "requested")
            self.assertEqual(self.submit("owner/b#1")["state"], "busy")
        with patch("gway_agent_ops.capacity.os.cpu_count", return_value=3):
            self.assertEqual(capacity(), 2)
            self.assertEqual(self.submit("owner/b#1")["state"], "requested")
            self.assertEqual(self.submit("owner/c#1")["state"], "busy")

    def test_corrupt_request_fails_closed(self):
        (self.root / "requests").mkdir()
        (self.root / "requests" / "broken.json").write_text('{"issue": 5}')
        with self.assertRaisesRegex(ValueError, "invalid request journal"):
            self.submit("owner/one#1")
        self.assertEqual(len(list((self.root / "requests").glob("*.json"))), 1)

    def test_parallel_work_is_not_a_retry_of_old_request(self):
        old = self.submit("owner/one#1")
        self.assertEqual(old["state"], "requested")
        self.assertEqual(self.submit("owner/two#1")["state"], "requested")
        self.assertEqual(observe("owner/one#1", AT, state_dir=self.root)["state"],
                         "requested")
        self.assertEqual(self.submit("owner/one#1")["state"], "already_requested")


if __name__ == "__main__":
    unittest.main()
