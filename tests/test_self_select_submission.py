from unittest.mock import Mock, patch

from gway_agent_ops.nonblocking import submit
from gway_agent_ops.observation import observe

REF, AT = "arthexis/example#9", "2026-11-27T12:00:00Z"


def test_self_selected_pr_needs_claim_and_preflight(tmp_path):
    runner = Mock(return_value=Mock(returncode=0))
    with patch("gway_agent_ops.self_select_store.authorized", return_value=False):
        result = submit(REF, AT, state_dir=tmp_path, runner=runner,
                        source="self-select", trigger="four-hour-budget", pickup_preflight=lambda *_: None)
    assert result["state"] == "self_select_claim_unavailable"
    assert observe(REF, AT, state_dir=tmp_path)["state"] == "unknown"
    runner.assert_not_called()


def test_authorized_pr_uses_existing_submission_and_provenance(tmp_path):
    runner = Mock(return_value=Mock(returncode=0))
    target = {"kind": "pull_request", "labels": []}
    with patch("gway_agent_ops.self_select_store.authorized", return_value=True), \
         patch("gway_agent_ops.self_select_target.resolve", return_value=target), \
         patch("gway_agent_ops.nonblocking._launch_prerequisites", return_value="/usr/bin/agent"), \
         patch("gway_agent_ops.coordination.reserve_submission", return_value=None):
        result = submit(REF, AT, state_dir=tmp_path, runner=runner,
                        source="self-select", trigger="four-hour-budget", pickup_preflight=lambda *_: None)
    assert result["state"] == "requested"
    receipt = observe(REF, AT, state_dir=tmp_path)
    assert receipt["source"] == "self-select" and receipt["trigger"] == "four-hour-budget"
