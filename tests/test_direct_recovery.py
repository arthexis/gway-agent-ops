"""Direct CLI orphan recovery requires strong systemd identity and cgroup proof."""
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gway_agent_ops.direct_recovery import (
    direct_supervision, direct_unit, recover_direct,
)
from gway_agent_ops.dispatch import admission, Busy, timestamp
from gway_agent_ops.observation import observe
from gway_agent_ops.reporting import flush

REF = "arthexis/example#11"
AT = "2026-10-13T15:00:00Z"
ID = "a" * 32


class DirectRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "dispatch"
        self.at = timestamp(AT)
        self.unit = direct_unit(REF, self.at)
        self.cgroup = "/user.slice/user-1000.slice/user@1000.service/app.slice/" + self.unit
        self.supervisor = direct_supervision(
            REF, self.at, invocation=ID, cgroup_text="0::" + self.cgroup)
        self.assertIsNotNone(self.supervisor)

    def accepted(self, *, supervised=True):
        with patch("gway_agent_ops.direct_recovery.direct_supervision",
                   return_value=self.supervisor if supervised else None):
            with admission(REF, AT, state_dir=self.root):
                pass
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "accepted")

    def evidence(self, **overrides):
        props = {
            "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
            "InvocationID": ID, "KillMode": "control-group", "Restart": "no",
            "TasksCurrent": "0", "MainPID": "0", "ControlPID": "0",
            "ControlGroup": self.cgroup,
        }
        props.update(overrides)
        return Mock(return_value=subprocess.CompletedProcess(
            ["systemctl"], 0, "\n".join(f"{k}={v}" for k,v in props.items()) + "\n", ""))

    def test_supervisor_comes_only_from_dedicated_kernel_cgroup(self):
        self.assertIsNone(direct_supervision(REF, self.at, invocation=ID,
            cgroup_text="0::/user.slice/other.service"))
        self.assertIsNone(direct_supervision(REF, self.at, invocation="invalid",
            cgroup_text="0::" + self.cgroup))
        self.assertIsNone(direct_supervision(REF, self.at, invocation=ID,
            cgroup_text="0::" + self.cgroup + "\n0::" + self.cgroup))
        self.assertEqual(self.supervisor["unit"], self.unit)

    def test_verified_inactive_cgroup_reconciles_without_launch(self):
        self.accepted()
        runner = self.evidence()
        self.assertEqual(recover_direct(REF, AT, state_dir=self.root, runner=runner)["state"],
                         "interrupted")
        cmd = runner.call_args.args[0]
        self.assertEqual(cmd[:4], ["systemctl", "--user", "show", self.unit])
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "interrupted")
        # A different issue may start, while the old identity remains terminal.
        with admission("arthexis/example#13", AT, state_dir=self.root):
            pass
        self.assertEqual(recover_direct(REF, AT, state_dir=self.root)["state"],
                         "already_terminal")

    def test_legacy_direct_orphan_stays_blocked(self):
        self.accepted(supervised=False)
        runner = Mock()
        with self.assertRaisesRegex(ValueError, "direct_supervision_missing"):
            recover_direct(REF, AT, state_dir=self.root, runner=runner)
        runner.assert_not_called()
        with self.assertRaises(Busy):
            with admission("arthexis/example#13", AT, state_dir=self.root):
                pass

    def test_running_execution_lock_prevents_recovery(self):
        self.accepted()
        fd = os.open(self.root / "execution.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "execution_active"):
                recover_direct(REF, AT, state_dir=self.root, runner=Mock())
        finally:
            os.close(fd)

    def test_no_proof_no_write(self):
        for change in (
            {"ActiveState": "active"}, {"TasksCurrent": "1"},
            {"InvocationID": "b" * 32}, {"KillMode": "process"},
            {"MainPID": "231"}, {"ControlPID": "33"},
            {"SubState": "running"}, {"ControlGroup": "/other.service"},
            {"LoadState": "not-found"},
        ):
            with self.subTest(change=change):
                if not (self.root / "accepted.jsonl").exists():
                    self.accepted()
                with self.assertRaisesRegex(ValueError, "direct_supervisor"):
                    recover_direct(REF, AT, state_dir=self.root,
                                   runner=self.evidence(**change))
                self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "accepted")
                self.assertFalse((self.root / "outbox").exists())

    def test_unconfirmed_or_corrupt_systemd_output_stays_blocked(self):
        self.accepted()
        for payload in ("", "ActiveState=inactive\n", "ActiveState=inactive\nActiveState=inactive\n"):
            with self.subTest(payload=payload):
                runner = Mock(return_value=subprocess.CompletedProcess([], 0, payload, ""))
                with self.assertRaises(ValueError):
                    recover_direct(REF, AT, state_dir=self.root, runner=runner)
                self.assertFalse((self.root / "outbox").exists())

    def test_sigkill_failed_unit_with_empty_cgroup_can_reconcile(self):
        self.accepted()
        runner = self.evidence(ActiveState="failed", SubState="failed")
        self.assertEqual(recover_direct(REF, AT, state_dir=self.root,
                                        runner=runner)["state"], "interrupted")
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "interrupted")

    def test_failed_unit_with_descendants_or_restart_policy_stays_blocked(self):
        self.accepted()
        for properties in (
            {"ActiveState": "failed", "SubState": "failed", "TasksCurrent": "1"},
            {"ActiveState": "failed", "SubState": "failed", "Restart": "on-failure"},
            {"ActiveState": "failed", "SubState": "failed", "Restart": "always"},
            {"ActiveState": "failed", "SubState": "failed", "InvocationID": "c" * 32},
            {"ActiveState": "failed", "SubState": "failed", "KillMode": "process"},
            {"ActiveState": "failed", "SubState": "dead"},
        ):
            with self.subTest(properties=properties):
                with self.assertRaises(ValueError):
                    recover_direct(REF, AT, state_dir=self.root,
                                   runner=self.evidence(**properties))
                self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "accepted")
                self.assertFalse((self.root / "outbox").exists())

    def test_restarted_invocation_or_reused_pid_cannot_recover(self):
        self.accepted()
        # PID 0 is not enough: a restarted unit has a new InvocationID and
        # must not be confused with the original killed CLI process.
        for changes in (
            {"InvocationID": "b" * 32},
            {"TasksCurrent": "2", "MainPID": "0", "ControlPID": "0"},
            {"TasksCurrent": "18446744073709551615"},
            {"MainPID": "0", "ControlPID": "0", "ActiveState": "activating"},
            {"ControlGroup": "/user.slice/restarted.service"},
            {"KillMode": "mixed"},
        ):
            with self.subTest(changes=changes):
                runner = self.evidence(**changes)
                with self.assertRaises(ValueError):
                    recover_direct(REF, AT, state_dir=self.root, runner=runner)
                self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "accepted")
        self.assertFalse((self.root / "outbox").exists())

    def test_legacy_unsupervised_record_cannot_use_pid_or_elapsed_time(self):
        self.accepted(supervised=False)
        with self.assertRaisesRegex(ValueError, "direct_supervision_missing"):
            recover_direct(REF, AT, state_dir=self.root, runner=self.evidence(
                TasksCurrent="0", MainPID="0", ActiveState="inactive"))
        self.assertFalse((self.root / "outbox").exists())

    def test_recovery_keeps_github_pending_and_does_not_dispatch(self):
        self.accepted()
        recover_direct(REF, AT, state_dir=self.root, runner=self.evidence())
        state = observe(REF, AT, state_dir=self.root)
        self.assertTrue(state["execution_terminal"])
        self.assertFalse(state["delivery_confirmed"])
        failure = Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))
        self.assertEqual(flush(state_dir=self.root, runner=failure),
                         {"sent": 0, "pending": 1})
        self.assertEqual(observe(REF, AT, state_dir=self.root)["state"], "interrupted")


if __name__ == "__main__":
    unittest.main()
