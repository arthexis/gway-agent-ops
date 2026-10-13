"""Static and failure-path checks for the optional real-systemd probe."""
import subprocess
import unittest
from unittest.mock import patch

from scripts.systemd_integration_probe import verify


class SystemdProbeTests(unittest.TestCase):
    @patch("scripts.systemd_integration_probe.os.geteuid", return_value=0)
    def test_refuses_root(self, _):
        with self.assertRaisesRegex(RuntimeError, "unprivileged"):
            verify()

    @patch("scripts.systemd_integration_probe.os.geteuid", return_value=1000)
    @patch("scripts.systemd_integration_probe.run")
    def test_fails_without_user_manager(self, runner, _):
        runner.return_value = subprocess.CompletedProcess([], 1, "", "no bus")
        with self.assertRaisesRegex(RuntimeError, "user_manager_unavailable"):
            verify()
        runner.assert_called_once_with("systemctl", "--user", "show-environment")


if __name__ == "__main__":
    unittest.main()
