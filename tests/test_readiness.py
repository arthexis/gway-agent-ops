import os
import tempfile
import unittest
from unittest.mock import patch

from gway_agent_ops.readiness import check


class ReadinessTest(unittest.TestCase):
    @patch('gway_agent_ops.readiness.subprocess.run')
    @patch('gway_agent_ops.readiness.shutil.which', return_value='/usr/bin/codex')
    @patch('gway_agent_ops.readiness.verify', return_value={'ready': True, 'authentication_available': True, 'authentication_format_valid': True})
    @patch('gway_agent_ops.readiness._launch_prerequisites')
    def test_missing_ledger_degrades_without_creating_it(self, launch, verify, which, run):
        run.return_value.returncode = 0
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'missing', 'ledger.sqlite3')
            with patch.dict(os.environ, {'GWAY_AGENT_LEDGER_PATH': path}):
                result = check()
            self.assertFalse(result['ready'])
            self.assertEqual(result['checks']['registry'], 'unavailable')
            self.assertFalse(os.path.exists(path))
            self.assertIn('calling process', result['context'])

    @patch('gway_agent_ops.readiness.subprocess.run', side_effect=OSError())
    @patch('gway_agent_ops.readiness.shutil.which', return_value=None)
    @patch('gway_agent_ops.readiness.verify', return_value={'ready': False, 'authentication_available': False, 'authentication_format_valid': False})
    @patch('gway_agent_ops.readiness._launch_prerequisites', side_effect=ValueError('agent_workspace_not_configured'))
    def test_failures_are_sanitized(self, launch, verify, which, run):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'GWAY_AGENT_LEDGER_PATH': root + '/none'}):
            result = check()
        self.assertFalse(result['ready'])
        self.assertEqual(result['checks']['launch_prerequisites'], 'agent_workspace_not_configured')
        self.assertEqual(result['checks']['github_authentication'], 'unavailable')
