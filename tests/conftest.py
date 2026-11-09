import pytest


@pytest.fixture(autouse=True)
def stable_node_identity(monkeypatch):
    # CI runners are not GWAY nodes; production has no hostname override.
    monkeypatch.setattr("gway_agent_ops.github_issues._hostname", lambda: "gway-001")
