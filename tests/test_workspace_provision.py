import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gway_agent_ops.workspace import WorkspaceError, provision


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def fake_git(self, args, *, cwd=None, timeout=120):
        if args[0] == 'clone':
            path = Path(args[-1])
            (path / '.git').mkdir()
            (path / '.origin').write_text(args[-2])
            return ''
        return (cwd / '.origin').read_text()

    def test_clone_alias_reuse_and_dirty_preservation(self):
        with patch('gway_agent_ops.workspace._git', side_effect=self.fake_git):
            checkout = provision(self.root, 'Arthexis/Example')
            (checkout / 'dirty').write_text('keep')
            self.assertEqual(provision(self.root, 'arthexis/example'), checkout)
            self.assertEqual((checkout / 'dirty').read_text(), 'keep')

    def test_wrong_origin_and_collision_fail_closed(self):
        with patch('gway_agent_ops.workspace._git', side_effect=self.fake_git):
            checkout = provision(self.root, 'Arthexis/Example')
            (checkout / '.origin').write_text('https://github.com/other/example.git')
            with self.assertRaisesRegex(WorkspaceError, 'origin_mismatch'):
                provision(self.root, 'arthexis/example')
            (checkout.parent / 'EXAMPLE').mkdir()
            with self.assertRaisesRegex(WorkspaceError, 'collision'):
                provision(self.root, 'arthexis/example')

    def test_symlink_and_failed_clone_leave_no_checkout(self):
        (self.root / 'arthexis').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            provision(self.root, 'arthexis/example')
        (self.root / 'arthexis').unlink()
        with patch('gway_agent_ops.workspace._git', side_effect=WorkspaceError('workspace_git_failed')):
            with self.assertRaises(WorkspaceError):
                provision(self.root, 'arthexis/example')
        self.assertEqual(list((self.root / 'arthexis').iterdir()), [])

    def test_git_failure_does_not_expose_output(self):
        with patch('gway_agent_ops.workspace.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'secret')):
            with self.assertRaises(WorkspaceError) as caught:
                provision(self.root, 'arthexis/example')
            self.assertNotIn('secret', str(caught.exception))

    def test_git_ignores_ambient_git_configuration(self):
        from gway_agent_ops.workspace import _git
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1',
                                     'GIT_CONFIG_KEY_0': 'url.evil.insteadOf',
                                     'GIT_CONFIG_VALUE_0': 'https://github.com/'}), \
             patch('gway_agent_ops.workspace.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            _git(['clone', '--', 'https://github.com/a/b.git', '/tmp/clone'])
        args = run.call_args.args[0]
        env = run.call_args.kwargs['env']
        self.assertNotIn('GIT_CONFIG_COUNT', env)
        self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)
        self.assertIn('http.followRedirects=false', args)
        self.assertIn('credential.helper=!gh auth git-credential', args)

class ReservationTests(unittest.TestCase):
    def test_accepted_identity_is_duplicate_across_case_aliases(self):
        from gway_agent_ops.dispatch import admission, Duplicate
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state'
            at = '2026-11-24T23:49:27Z'
            with admission('arthexis/Melquisedec#3', at, state_dir=state):
                pass
            with self.assertRaises(Duplicate):
                with admission('Arthexis/melquisedec#3', at, state_dir=state):
                    pass

    def test_missing_workspace_records_terminal_without_acceptance(self):
        import json
        from gway_agent_ops.dispatch import timestamp
        from gway_agent_ops.executor import execute, ExecutionError
        from gway_agent_ops.reporting import _key
        from gway_agent_ops.observation import observe
        from gway_agent_ops.capacity import unresolved
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'state'
            requests = state / 'requests'
            requests.mkdir(parents=True, mode=0o700)
            state.chmod(0o700)
            ref = 'arthexis/Melquisedec#3'
            at = timestamp('2026-11-24T23:49:27Z')
            marker = requests / (_key(ref, at) + '.json')
            marker.write_text(json.dumps({'issue': ref, 'at': at, 'state': 'requested'}))
            issue = {'repository': 'arthexis/Melquisedec', 'number': 3,
                     'title': 'test', 'body': '', 'comments': 0, 'labels': [], 'url': 'https://github.com/'}
            with patch('gway_agent_ops.workspace._git', side_effect=WorkspaceError('workspace_git_failed')):
                with self.assertRaises(ExecutionError):
                    execute(ref, at=at, workspace_root=root, state_dir=state,
                            resolver=lambda *a, **k: issue)
            result = observe(ref, at, state_dir=state)
            self.assertEqual(result['state'], 'preacceptance_failed')
            self.assertEqual(result['failure_code'], 'workspace_git_failed')
            self.assertEqual(unresolved(state), [])
            self.assertFalse((state / 'accepted.jsonl').exists())
            from gway_agent_ops.dispatch import Duplicate
            with self.assertRaises(Duplicate):
                execute(ref, at=at, workspace_root=root, state_dir=state,
                        resolver=lambda *a, **k: issue)
