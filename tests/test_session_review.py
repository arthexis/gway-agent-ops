import importlib.util
import json
import sqlite3
import sys

spec = importlib.util.spec_from_file_location('session_review', 'scripts/review_agent_sessions.py')
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def test_review_preserves_unknown_and_flags_candidate(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'state.sqlite'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE threads (id TEXT, cwd TEXT, first_user_message TEXT, created_at INTEGER, updated_at INTEGER, attached_thread_parent_id TEXT, archived INTEGER)')
    db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', [
        ('closed', '/work', 'Work on the trusted GitHub issue owner/repo#1.', 100, 100, None, 0),
        ('unknown', '/work', 'Work on the trusted GitHub issue owner/repo#2.', 100, 100, None, 0),
        ('other', '/work', 'Unrelated conversation', 100, 100, None, 0),
    ])
    db.commit(); db.close()
    def fake_command(args):
        if args[0:2] == ['agent', 'history']:
            return {'status':'ok', 'data':{'executions':[{'issue':'owner/repo#1','at':'1970-01-01T00:01:40Z','state':'finished'}]}}
        if args[0:2] == ['agent', 'status']:
            return {'status':'ok', 'data':{'admission':{'reservations':[]}}}
        if args[0:3] == ['gh', 'issue', 'view'] and args[3] == '1':
            return {'state':'CLOSED','title':'done','url':'https://example.test/1'}
        return None
    monkeypatch.setattr(review, 'command', fake_command)
    monkeypatch.setattr(sys, 'argv', ['review', '--database', str(path), '--json'])
    assert review.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert {s['thread_id']: s['assessment'] for s in report['sessions']} == {'closed':'candidate_verify_thread', 'unknown':'unknown'}
    assert sqlite3.connect(path).execute('SELECT count(*) FROM threads WHERE archived=0').fetchone()[0] == 3


def test_malformed_evidence_is_unavailable():
    for value in (None, {}, {"status": "ok"}, {"status": "ok", "data": {"executions": [None]}}):
        assert review.evidence(value, "executions") is None
    assert review.evidence({"status": "ok", "data": {"executions": []}}, "executions") == []
