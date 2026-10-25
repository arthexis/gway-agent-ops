"""The controller may be an Agent Ops issue source, not a deployment target."""
import json

from gway_agent_ops.repository_registry import agent_repositories, parse_snapshot


def test_only_controller_is_added():
    source = ("arthexis/ocpp-csms", "arthexis/gway-agent-ops")
    authorized = agent_repositories(source)
    assert set(authorized) == set(source) | {"arthexis/gway-remote"}
    assert len(authorized) == 3
    assert "arthexis/unapproved" not in authorized


def test_absent_allowlist_is_still_closed():
    assert agent_repositories(()) == ()


def test_export_stays_unmodified_and_exception_is_local():
    payload = {"schema": 1, "revision": "a" * 40,
               "targets": [{"name": "csms", "repository": "arthexis/ocpp-csms"}]}
    revision, repos = parse_snapshot(json.dumps(payload))
    assert revision == payload["revision"]
    assert set(repos) == {"arthexis/gway-remote", "arthexis/ocpp-csms"}
    assert len(payload["targets"]) == 1
