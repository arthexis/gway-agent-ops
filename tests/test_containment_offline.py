"""Linux-only, unprivileged negative experiment for process lifetime.

This tests *process-session* escape from subprocess.run(timeout=...), NOT
systemd cgroup migration or an OS-level containment proof. All synthetic
processes run in the disposable CI environment and are killed in finally.
"""
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform.startswith("linux") and Path("/proc").is_dir(),
                     "requires Linux /proc")
class OfflineContainmentTests(unittest.TestCase):
    def test_timed_out_parent_can_leave_detached_grandchild_running(self):
        parent = (
            "import pathlib,subprocess,sys,time\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
            "start_new_session=True,stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
            "time.sleep(30)\n"
        )
        child_pid = None
        with tempfile.TemporaryDirectory(prefix="agentops-process-test-") as directory:
            pidfile = Path(directory) / "grandchild.pid"
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    subprocess.run(
                        [sys.executable, "-c", parent, str(pidfile)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=2,
                        check=False,
                    )
                self.assertTrue(pidfile.is_file(), "synthetic parent did not spawn")
                child_pid = int(pidfile.read_text(encoding="ascii"))
                # subprocess.run() already killed/waited for the parent. A
                # detached grandchild retains independent process lifetime.
                statline = Path(f"/proc/{child_pid}/stat").read_text(encoding="ascii")
                state = statline.rsplit(")", 1)[1].strip().split()[0]
                self.assertNotIn(state, ("Z", "X"), "grandchild must still be live")
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
