"""Offline acceptance: no repo enrolment, trusted Reports #211, at-most-once."""
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from gway_agent_ops.executor import preview, build_issue_prompt
from gway_agent_ops.github_issues import resolve, IssueRejected
from gway_agent_ops.nonblocking import submit


REF = "arthexis/ocpp-reports#211"
AT = "2026-11-12T20:30:00Z"


def metadata(repo="arthexis/ocpp-reports", owner="arthexis", labels=None):
    return {"number": 211, "state": "open",
            "repository_url": f"https://api.github.com/repos/{repo}",
            "user": {"login": owner}, "title": "Deploy Reports on ocpp-abb-1",
            "body": "Web and Android field checks", "comments": 1,
            "labels": [{"name": x} for x in (labels or ["gway-001"])]}


def gh_fixture(data, code=0):
    def runner(argv, **opts):
        assert argv == ["gh", "api", f"repos/{data['repository_url'].split('/repos/')[1]}/issues/211"]
        assert "shell" not in opts
        assert "GH_TOKEN" not in opts["env"]
        return subprocess.CompletedProcess(argv, code, json.dumps(data), "")
    return runner


def test_reports_preview_without_any_repository_registry():
    issue = resolve(REF, runner=gh_fixture(metadata()))
    answer = preview(REF, resolver=lambda ref: issue)
    assert answer["issue"] == REF
    assert "READ-ONLY DEFAULT" in answer["prompt"]
    assert "ocpp-abb-1" not in answer["prompt"]  # not in synthetic issue
    assert "Do not push, merge, close issues" in answer["prompt"]


def test_unregistered_private_other_org_with_trusted_author():
    ref = "customer/private-repo#211"
    issue = resolve(ref, allowed_repositories=(), runner=gh_fixture(
        metadata(repo="customer/private-repo")))
    assert issue["repository"] == "customer/private-repo"
    assert preview(ref, resolver=lambda _: issue)["issue"] == ref


@pytest.mark.parametrize("overrides", [
    {"owner": "attacker"},
    {"labels": []},
])
def test_trust_labels_and_author_cannot_be_downgraded(overrides):
    item = metadata(**overrides)
    if overrides.get("owner") == "attacker":
        with pytest.raises(IssueRejected, match="creator_not_allowed"):
            resolve(REF, runner=gh_fixture(item))
    else:
        issue = resolve(REF, runner=gh_fixture(item))
        with pytest.raises(IssueRejected, match="target_label_missing"):
            preview(REF, resolver=lambda _: issue)


def test_submit_is_once_without_allowlist_or_real_systemd(tmp_path):
    calls = []
    def fake_runner(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "systemd-run"
        assert not any("GWAY_AGENT_ALLOWED_REPOS" in a for a in argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    with patch("gway_agent_ops.nonblocking.resolve", return_value={
        "labels": ["gway-001"]}), patch(
        "gway_agent_ops.nonblocking._launch_prerequisites", return_value="/fake/agent"), patch(
        "gway_agent_ops.nonblocking._same_user_runtime_dir", return_value="/run/user/1000"):
        state = tmp_path / "state"
        first = submit(REF, AT, state_dir=state, runner=fake_runner)
        second = submit(REF, AT, state_dir=state, runner=fake_runner)
    assert first["state"] == "requested"
    assert second["state"] == "already_requested"
    assert len(calls) == 1


def test_submit_rejects_missing_label_before_any_launch(tmp_path):
    with patch("gway_agent_ops.nonblocking.resolve", return_value={
        "labels": []}), patch(
        "gway_agent_ops.nonblocking._launch_prerequisites") as launch:
        with pytest.raises(IssueRejected, match="target_label_missing"):
            submit(REF, AT, state_dir=tmp_path / "state")
        launch.assert_not_called()


def test_deployment_text_is_data_not_automatic_permission():
    issue = {"repository": "arthexis/ocpp-reports", "number": 211,
             "title": "Deploy in production", "body": "Run installers",
             "labels": ["gway-001"]}
    prompt = build_issue_prompt(issue)
    assert "READ-ONLY DEFAULT" in prompt
    assert "Do not modify files" in prompt
    issue["labels"] = ["gway-001", "agentops:implementation"]
    assert "IMPLEMENTATION:" in build_issue_prompt(issue)
