from unittest.mock import patch

import pytest

from gway_agent_ops.github_issues import IssueRejected, require_node_route


def check(node, labels):
    with patch('gway_agent_ops.github_issues.socket.gethostname', return_value=node):
        require_node_route({'labels': labels})


def test_004_requires_its_single_label():
    check('gway-004', ['gway-004'])
    for labels in ([], ['gway-001'], ['gway-001', 'gway-004']):
        with pytest.raises(IssueRejected):
            check('gway-004', labels)


def test_001_preserves_unlabelled_but_refuses_004_and_ambiguous():
    check('gway-001', [])
    check('gway-001', ['gway-001'])
    for labels in (['gway-004'], ['gway-001', 'gway-004']):
        with pytest.raises(IssueRejected):
            check('gway-001', labels)


def test_execution_and_preview_reject_before_using_workspace(tmp_path):
    from gway_agent_ops import executor
    issue = {'repository': 'arthexis/example', 'number': 1,
             'labels': ['gway-001'], 'title': 'x', 'body': '',
             'url': 'https://example.invalid', 'comments': 0}
    def resolver(*args, **kwargs):
        return issue
    with patch('gway_agent_ops.github_issues.socket.gethostname', return_value='gway-004'):
        with pytest.raises(IssueRejected):
            executor.preview('arthexis/example#1', resolver=resolver)
        with pytest.raises(IssueRejected):
            executor.execute('arthexis/example#1', workspace_root=tmp_path,
                             resolver=resolver)
