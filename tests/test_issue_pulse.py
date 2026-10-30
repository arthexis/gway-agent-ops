import json

from gway_agent_ops import issue_pulse as p


def comment(n, body=None):
    return {'id': n, 'body': body or f'Instruction {n}', 'html_url': f'https://example/{n}',
            'updated_at': str(n), 'user': {'login': 'operator'}}


def snapshot():
    return {'issue': {'number': 1}, 'comments': {'1': p.brief_comment(comment(1))},
            'prs': {}, 'external_pr_links': [], 'pr_links_truncated': False}


def test_references_and_cross_repository_identity():
    assert p.references('a/b', ['PR #304; pull request 305',
        'https://github.com/c/d/pull/306']) == [('a/b', 304), ('a/b', 305), ('c/d', 306)]


def test_pagination_reaches_latest_and_plain_text():
    rows = [comment(n) for n in range(105)]
    seen = []
    def get(path):
        seen.append(path)
        return rows[:100] if path.endswith('page=1') else rows[100:]
    result = p.pages('comments', get)
    assert len(seen) == 2
    assert p.brief_comment(result[-1])['excerpt'] == 'Instruction 104'


def test_edited_comment_and_unchanged():
    data = snapshot()
    _, baseline, _ = p.report(data)
    assert p.report(data, baseline)[0] == {'status': 'unchanged'}
    data['comments']['1'] = p.brief_comment(comment(1, 'Hold merge'))
    report, _, advance = p.report(data, baseline)
    assert report['comments'][0]['excerpt'] == 'Hold merge'
    assert advance


def test_hidden_body_edit_detected():
    data = snapshot()
    data['comments']['1'] = p.brief_comment(comment(1, 'x' * 300))
    _, baseline, _ = p.report(data)
    data['comments']['1'] = p.brief_comment(comment(1, 'x' * 299 + 'y'))
    assert p.report(data, baseline)[0]['comments']


def test_truncation_does_not_advance():
    data = snapshot()
    _, baseline, _ = p.report(data)
    for n in range(2, 8):
        data['comments'][str(n)] = p.brief_comment(comment(n))
    result, _, advance = p.report(data, baseline, limit=2)
    assert result['truncated'] and not advance


def test_output_limit_retains_checkpoint(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'state.json'
    data = snapshot()
    monkeypatch.setattr(p, 'collect', lambda *a, **kw: data)
    args = ['--repo', 'a/b', '--issue', '1', '--state', str(path)]
    assert p.main(args) == 0
    original = path.read_bytes()
    data['issue']['title'] = 'x' * 2000
    assert p.main(args + ['--max-chars', '512']) == 0
    assert path.read_bytes() == original
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['status'] == 'output_limit'
    assert path.stat().st_mode & 0o777 == 0o600


def test_failure_retains_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    p.save(path, {'schema_version': 1, 'identity': 'a/b#1', 'fingerprints': {}})
    original = path.read_bytes()
    def fail(*a, **kw):
        raise RuntimeError('private diagnostic')
    monkeypatch.setattr(p, 'collect', fail)
    assert p.main(['--repo', 'a/b', '--issue', '1', '--state', str(path)]) == 1
    assert path.read_bytes() == original


def test_cross_repo_links_do_not_trigger_fetch():
    def get(path):
        if '/comments?' in path:
            return []
        assert path == 'repos/a/b/issues/1'
        return {'number': 1, 'body': 'https://github.com/c/d/pull/4', 'html_url': 'url'}
    assert p.collect('a/b', 1, get)['external_pr_links'] == ['c/d#4']
