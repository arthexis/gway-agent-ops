"""Give node-sensitive tests a deterministic default identity."""
import pytest


@pytest.fixture(autouse=True)
def default_test_node(monkeypatch):
    monkeypatch.setattr('gway_agent_ops.github_issues.socket.gethostname', lambda: 'gway-001')
