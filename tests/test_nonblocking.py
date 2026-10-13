import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from gway_agent_ops.nonblocking import submit
from gway_agent_ops.observation import observe


class NonblockingTests(unittest.TestCase):
    def test_once_only_and_observable(self):
        ref, at = "arthexis/example#3", "2026-10-13T15:00:00Z"
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"GWAY_AGENT_WORKSPACE_ROOT": "/tmp/workspace"}), \
                 patch("gway_agent_ops.nonblocking.resolve", return_value={}), \
                 patch("gway_agent_ops.nonblocking.shutil.which", return_value="/usr/bin/agent"), \
                 patch("gway_agent_ops.nonblocking.Path.is_file", return_value=True):
                runner = Mock(return_value=Mock(returncode=0))
                first = submit(ref, at, allowed_repositories=("arthexis/example",),
                               state_dir=Path(temp), runner=runner)
                self.assertEqual(first["state"], "requested")
                self.assertEqual(observe(ref, at, state_dir=Path(temp))["state"], "requested")
                second = submit(ref, at, allowed_repositories=("arthexis/example",),
                                state_dir=Path(temp), runner=runner)
                self.assertEqual(second["state"], "already_requested")
                self.assertEqual(runner.call_count, 1)
                self.assertEqual(runner.call_args.args[0][-6:],
                                 ["/usr/bin/agent", "issue", ref, "--at",
                                  "2026-10-13T15:00:00.000000Z", "--json"])

    def test_trust_rejection_never_launches(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("gway_agent_ops.nonblocking.resolve", side_effect=ValueError("rejected")):
                runner = Mock()
                with self.assertRaises(ValueError):
                    submit("owner/repo#1", "2026-10-13T15:00:00Z",
                           allowed_repositories=("owner/repo",),
                           state_dir=Path(temp), runner=runner)
                runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
