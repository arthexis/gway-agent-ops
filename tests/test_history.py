import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gway_agent_ops import cli
from gway_agent_ops.history import history
from gway_agent_ops.reporting import enqueue

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def put(self, rows):
        (self.root / 'accepted.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))

    def test_empty_missing_and_bad_limits(self):
        self.assertEqual(history(state_dir=self.root), [])
        for n in (0, -1, 1001, True):
            with self.subTest(n=n), self.assertRaises(ValueError): history(limit=n, state_dir=self.root)

    def test_order_repeated_issue_and_actual_report(self):
        old='2026-01-01T00:00:00.000000Z'; new='2026-01-02T00:00:00.000000Z'
        issue='arthexis/test#1'
        self.put([{'issue':issue,'at':new},{'issue':issue,'at':old}])
        enqueue(issue, old, state_dir=self.root, status='finished', exit_code=0)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        rows=history(limit=2,state_dir=self.root)
        self.assertEqual([r['at'] for r in rows],[new,old])
        self.assertEqual([r['state'] for r in rows],['accepted','finished'])
        self.assertEqual(rows[1]['exit_code'],0)
        self.assertFalse(rows[1]['delivery_confirmed'])
        self.assertEqual(history(limit=1,state_dir=self.root),rows[:1])
        self.assertEqual(before,{p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_corrupt_or_symlink_journal_fails(self):
        p=self.root/'accepted.jsonl'
        for data in ('[]\n','not json\n','{"issue":"bad","at":"bad"}\n'):
            p.write_text(data)
            with self.subTest(data=data), self.assertRaises(ValueError): history(state_dir=self.root)
        p.unlink(); p.symlink_to('missing')
        with self.assertRaises(ValueError): history(state_dir=self.root)

    def test_cli_json_and_error(self):
        entries=[{'issue':'a/b#1','at':'2026-01-01T00:00:00.000000Z','state':'accepted','exit_code':None,'delivery_attempted':None,'delivery_confirmed':None}]
        with patch('gway_agent_ops.history.history',return_value=entries), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['history','--limit','3','--json']),0)
        self.assertEqual(json.loads(out.getvalue())['data']['executions'],entries)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['history','--limit','0','--json']),1)
        self.assertEqual(json.loads(out.getvalue())['error']['code'],'history_unavailable')

if __name__=='__main__': unittest.main()
