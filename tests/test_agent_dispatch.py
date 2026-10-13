"""A3.2 timestamp idempotency, serial execution and restart persistence."""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from gway_agent_ops.dispatch import Busy, Duplicate, admission, timestamp
from gway_agent_ops.cli import main
from gway_agent_ops.reporting import _key


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "dispatch"
        self.issue = "arthexis/gway-lcd-sound#19"
        self.at = "2026-10-13T01:00:00Z"

    def finish(self, issue, at):
        folder = self.state / "delivered"
        folder.mkdir(exist_ok=True)
        from gway_agent_ops.dispatch import timestamp as normalize
        (folder / (_key(issue, normalize(at)) + ".json")).write_text(
            json.dumps({"status": "finished", "exit_code": 0}))

    def test_duplicate_survives_new_call_and_changed_timestamp_runs(self):
        with admission(self.issue, self.at, state_dir=self.state):
            pass
        with self.assertRaises(Duplicate):
            with admission(self.issue, self.at, state_dir=self.state):
                pass
        self.finish(self.issue, self.at)
        with admission(self.issue, "2026-10-13T01:00:01Z", state_dir=self.state):
            pass
        self.assertEqual(len((self.state / "accepted.jsonl").read_text().splitlines()), 2)

    def test_one_slot_across_processes(self):
        with admission(self.issue, self.at, state_dir=self.state):
            program = ("from gway_agent_ops.dispatch import admission, Busy\n"
                       "from pathlib import Path\n"
                       f"try:\n with admission('arthexis/other#1', '2026-10-13T01:00:02Z', state_dir=Path({str(self.state)!r})): pass\n"
                       "except Busy: exit(42)\n")
            result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
            self.assertEqual(result.returncode, 42, result.stderr)
        self.finish(self.issue, self.at)
        with admission("arthexis/other#1", "2026-10-13T01:00:02Z", state_dir=self.state):
            pass

    def test_normalization_and_future_date(self):
        self.assertEqual(timestamp("2026-10-13T01:00:00+00:00"), "2026-10-13T01:00:00.000000Z")
        with self.assertRaises(ValueError):
            timestamp("2026-10-13T01:00:00")
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):
            timestamp(future)

    def test_cli_rejects_bad_time(self):
        with self.assertRaises(SystemExit) as ctx:
            main(["issue", self.issue, "--at", "tomorrow"])
        self.assertEqual(ctx.exception.code, 2)

    def test_no_resend_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            main(["issue", self.issue, "--resend"])
        self.assertEqual(ctx.exception.code, 2)
