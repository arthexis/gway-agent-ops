from gway_agent_ops.executor import build_self_select_prompt
from gway_agent_ops.dispatch import timestamp
from gway_agent_ops.reporting import _key
from unittest.mock import patch
import json
import pytest


def test_self_select_pr_prompt_uses_exact_head_and_preserves_scope():
    item = {"kind": "pull_request", "url": "https://github.com/arthexis/example/pull/9",
            "head_sha": "a" * 40, "title": "Fix", "body": "untrusted", "labels": []}
    prompt = build_self_select_prompt(item)
    assert "a" * 40 in prompt
    assert "isolated worktree" in prompt
    assert "Do not push, merge, close issues, deploy" in prompt
    assert "untrusted task data" in prompt


def test_explicit_read_only_policy_is_preserved():
    item = {"kind": "issue", "url": "https://github.com/arthexis/example/issues/9",
            "head_sha": None, "title": "Audit", "body": "", "labels": ["agentops:read-only"]}
    assert "READ-ONLY INVESTIGATION" in build_self_select_prompt(item)


def test_executor_requires_bound_claim_for_self_selected_pr(tmp_path):
    from gway_agent_ops.executor import execute, ExecutionError
    ref, at = "arthexis/example#9", timestamp("2026-11-27T12:00:00Z")
    requests = tmp_path / "requests"
    requests.mkdir()
    marker = requests / (_key(ref, at) + ".json")
    marker.write_text(json.dumps({"issue": ref, "at": at, "source": "self-select", "trigger": "four-hour-budget"}))
    with patch("gway_agent_ops.self_select_store.authorized", return_value=False), \
         pytest.raises(ExecutionError, match="self_select_claim_unavailable"):
        execute(ref, at=at, workspace_root=tmp_path, state_dir=tmp_path)
    with patch("gway_agent_ops.self_select_store.authorized", return_value=True), \
         patch("gway_agent_ops.executor._request_transition", return_value=False), \
         patch("gway_agent_ops.executor._execute", return_value={"status": "finished"}) as run:
        execute(ref, at=at, workspace_root=tmp_path, state_dir=tmp_path)
        assert run.call_args.kwargs["self_selected"] is True
        assert run.call_args.kwargs["resolver"].__module__.endswith("self_select_target")


def test_self_select_pr_executes_through_admission_and_retains_local_findings(tmp_path):
    import subprocess
    from gway_agent_ops.executor import execute
    from gway_agent_ops.observation import observe
    ref, at = "arthexis/example#9", timestamp("2026-11-27T12:00:00Z")
    state = tmp_path / "state"
    requests = state / "requests"
    requests.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    (requests / (_key(ref, at) + ".json")).write_text(json.dumps({"issue": ref, "at": at,
        "state": "requested", "source": "self-select", "trigger": "four-hour-budget"}))
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text("")
    item = {"repository": "arthexis/example", "number": 9, "title": "Fix", "body": "Details",
            "comments": 0, "labels": [], "kind": "pull_request", "head_sha": "a" * 40,
            "url": "https://github.com/arthexis/example/pull/9"}
    report = {"summary": "Completed the task", "findings": ["One finding"],
              "evidence": ["Test passed"], "limitations": "none"}
    seen = []
    def runner(argv, **kwargs):
        seen.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, "AGENT_OPS_FINDINGS_JSON:" + json.dumps(report), "")
    context = lambda: {"codex_home": str(home), "authentication_available": True, "environment": {"PATH": "/usr/bin"}}
    with patch("gway_agent_ops.self_select_store.authorized", return_value=True), \
         patch("gway_agent_ops.self_select_target.resolve", return_value=item), \
         patch("gway_agent_ops.workspace.provision", return_value=tmp_path), \
         patch("gway_agent_ops.executor.resolve_codex_binary", return_value="/verified/codex"), \
         patch("gway_agent_ops.coordination.reserve_admission", return_value=None), \
         patch("gway_agent_ops.activity_feed.publish"), patch("gway_agent_ops.lcd_notification.enabled", return_value=False):
        result = execute(ref, at=at, workspace_root=tmp_path, state_dir=state, runner=runner, runtime=context)
    assert result["status"] == "finished" and item["head_sha"] in seen[0]
    observation = observe(ref, at, state_dir=state)
    assert observation["report_scope"] == "local" and observation["findings_state"] == "validated"
    assert not (state / "outbox").exists()
