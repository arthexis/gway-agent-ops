"""Read-only, bounded issue/PR evidence. Run with python -m gway_agent_ops.issue_pulse."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def api(path):
    result = subprocess.run(['gh', 'api', path], capture_output=True, text=True,
                            timeout=30, check=False)
    if result.returncode:
        raise RuntimeError('GitHub request failed; inspect gh authentication/access separately')
    return json.loads(result.stdout)


def pages(path, get=api):
    """Read complete lists; refuse to silently accept an incomplete collection."""
    rows = []
    for page in range(1, 101):
        data = get(f'{path}?per_page=100&page={page}')
        if not isinstance(data, list):
            raise ValueError('Expected a GitHub list')
        rows.extend(data)
        if len(data) < 100:
            return rows
    raise ValueError('Pagination limit reached; evidence incomplete')


def references(repository, bodies):
    found = set()
    for body in bodies:
        found.update((repo, int(number)) for repo, number in re.findall(
            r'https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)\b', body))
        found.update((repository, int(number)) for number in re.findall(
            r'\b(?:PR|pull request)\s*#?(\d+)\b', body, re.I))
    return sorted(found)


def brief_comment(row):
    body = row.get('body') or ''
    return {'id': row['id'], 'author': (row.get('user') or {}).get('login'),
            'updated_at': row.get('updated_at'), 'url': row['html_url'],
            'body_sha256': digest(body), 'excerpt': ' '.join(body.split())[:240]}


def pull(repository, number, get=api):
    row = get(f'repos/{repository}/pulls/{number}')
    sha = row['head']['sha']
    checks = []
    for page in range(1, 101):
        data = get(f'repos/{repository}/commits/{sha}/check-runs?per_page=100&page={page}')
        batch = data['check_runs']
        checks.extend({'name': c['name'], 'status': c['status'],
                       'conclusion': c.get('conclusion')} for c in batch)
        if len(batch) < 100:
            break
    else:
        raise ValueError('Check pagination incomplete')
    return {'repository': repository, 'number': number, 'url': row['html_url'],
            'head_sha': sha, 'base_sha': row['base']['sha'], 'state': row['state'],
            'merged': bool(row.get('merged')), 'draft': bool(row.get('draft')),
            'mergeable': row.get('mergeable'), 'checks': checks,
            'checks_scope': 'observed check-runs; not proof of required-check or review approval'}


def collect(repository, number, get=api, is_pr=False, pr_limit=8):
    issue = get(f'repos/{repository}/issues/{number}')
    comments = pages(f'repos/{repository}/issues/{number}/comments', get)
    body = issue.get('body') or ''
    refs = references(repository, [body] + [c.get('body') or '' for c in comments])
    # Do not silently expand access to repositories linked by untrusted issue text.
    local = [(r, n) for r, n in refs if r == repository]
    if is_pr and (repository, number) not in local:
        local.append((repository, number))
    issue_row = {key: issue.get(key) for key in
                 ('number', 'title', 'state', 'updated_at', 'comments')}
    issue_row.update(url=issue['html_url'], body_sha256=digest(body),
                     excerpt=' '.join(body.split())[:240])
    return {'issue': issue_row,
            'comments': {str(c['id']): brief_comment(c) for c in comments},
            'prs': {f'{r}#{n}': pull(r, n, get) for r, n in local[:pr_limit]},
            'external_pr_links': [f'{r}#{n}' for r, n in refs if r != repository],
            'pr_links_truncated': len(local) > pr_limit}


def fingerprints(snapshot):
    return {section: {key: digest(value) for key, value in values.items()}
            if section in ('comments', 'prs') else digest(values)
            for section, values in snapshot.items()}


def report(snapshot, previous=None, limit=4):
    current = fingerprints(snapshot)
    if previous == current:
        return {'status': 'unchanged'}, current, True
    result = {'status': 'changed' if previous else 'snapshot',
              'evidence_source': 'github_live', 'truncated': False}
    for key, value in snapshot.items():
        if key in ('comments', 'prs'):
            prior = (previous or {}).get(key, {})
            changed = [v for k, v in value.items() if prior.get(k) != current[key][k]]
            if key == 'comments':
                if previous and len(changed) > limit:
                    result['truncated'] = True
                result['comment_changes_available'] = len(changed)
                changed = changed[-limit:]
            result[key] = changed
            result['removed_' + key] = sorted(set(prior) - set(value))
        elif not previous or previous.get(key) != current[key]:
            result[key] = value
    result['truncated'] |= snapshot['pr_links_truncated']
    return result, current, not result['truncated']


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.pulse-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--issue', type=int)
    target.add_argument('--pr', type=int)
    parser.add_argument('--comments', type=int, default=4)
    parser.add_argument('--prs', type=int, default=8)
    parser.add_argument('--max-chars', type=int, default=6000)
    parser.add_argument('--state', type=Path, help='Private fingerprint baseline; no GitHub mutation')
    args = parser.parse_args(argv)
    number = args.issue or args.pr
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', args.repo) or number is None or number < 1:
        parser.error('Expected owner/repository and positive issue/PR number')
    if args.comments < 1 or not 1 <= args.prs <= 100 or args.max_chars < 512:
        parser.error('--comments must be positive; --prs 1..100; --max-chars at least 512')
    identity = f'{args.repo}#{number}'
    try:
        previous = None
        if args.state and args.state.exists():
            stored = json.loads(args.state.read_text())
            if stored.get('identity') != identity or stored.get('schema_version') != 1:
                raise ValueError('Baseline belongs to another issue or schema')
            previous = stored['fingerprints']
        snapshot = collect(args.repo, number, is_pr=args.pr is not None, pr_limit=args.prs)
        result, baseline, advance = report(snapshot, previous, args.comments)
        result['identity'] = identity
        result['observed_at'] = datetime.now(timezone.utc).isoformat()
        result['evidence_source'] = 'github_live'
        result['checkpoint_advanced'] = bool(args.state and advance)
        output = json.dumps(result, separators=(',', ':'), sort_keys=True)
        if len(output) > args.max_chars:
            advance = False
            output = json.dumps({'identity': identity, 'status': 'output_limit',
                                 'truncated': True, 'checkpoint_advanced': False,
                                 'required_chars': len(output),
                                 'hint': 'Increase --max-chars; baseline retained'})
        if args.state and advance:
            save(args.state, {'schema_version': 1, 'identity': identity,
                              'fingerprints': baseline})
        print(output)
        return 0
    except (RuntimeError, ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired):
        print(json.dumps({'status': 'error', 'identity': identity,
                          'checkpoint_advanced': False,
                          'error': 'Evidence unavailable or invalid; baseline retained'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
