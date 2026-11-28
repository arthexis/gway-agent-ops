import json
from unittest.mock import Mock, patch

from gway_agent_ops.observation import observe
from gway_agent_ops.reporting import enqueue, flush
from contextlib import redirect_stdout
from io import StringIO

REF, AT = "arthexis/example#9", "2026-11-27T12:00:00.000000Z"


def test_self_select_reports_are_private_and_never_flushed(tmp_path):
    (tmp_path / "accepted.jsonl").write_text(json.dumps({"issue": REF, "at": AT}) + "\n")
    with patch("gway_agent_ops.activity_feed.publish"), patch("gway_agent_ops.lcd_notification.enabled", return_value=False):
        enqueue(REF, AT, state_dir=tmp_path, status="finished", exit_code=0, local_only=True,
                findings_state="validated", findings={"summary": "Completed the task", "findings": ["One finding"],
                "evidence": ["Test passed"], "limitations": "none"})
    observed = observe(REF, AT, state_dir=tmp_path)
    assert observed["report_scope"] == "local" and observed["execution_terminal"] is True
    assert observed["delivery_confirmed"] is False and observed["findings"]["summary"] == "Completed the task"
    runner = Mock()
    assert flush(state_dir=tmp_path, runner=runner)["sent"] == 0
    runner.assert_not_called()


def test_self_select_worker_does_not_flush_other_github_reports(tmp_path):
    from gway_agent_ops.cli import main
    with patch.dict("os.environ", {"GWAY_AGENT_WORKSPACE_ROOT": str(tmp_path)}), \
         patch("gway_agent_ops.cli.load_runtime_environment"), \
         patch("gway_agent_ops.executor.self_select_request", return_value=True), \
         patch("gway_agent_ops.executor.execute", return_value={"status": "finished"}), \
         patch("gway_agent_ops.reporting.flush") as send, redirect_stdout(StringIO()):
        assert main(["issue", REF, "--at", AT, "--json"]) == 0
    send.assert_not_called()
