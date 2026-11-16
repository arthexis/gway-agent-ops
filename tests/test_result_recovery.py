"""Hermetic report recovery: never invoke Codex, mutate GitHub or nodes."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gway_agent_ops.findings import MARKER
from gway_agent_ops.reporting import _key
from gway_agent_ops.result_recovery import report, _extract
from gway_agent_ops import mcp_stdio


REF = "arthexis/ocpp-csms#366"
AT = "2026-11-16T15:20:32.000000Z"
FINDINGS = {
    "summary": "Historical EV Ready handshake evidence recovered.",
    "findings": ["HTTP 101 responses were observed in saved captures."],
    "evidence": ["Synthetic fixture for prior field report."],
    "limitations": "Historical BootNotification is not proven.",
}


def comment(*, identity=AT, author="arthexis", comment_id=123, body=None):
    marker = "agent-dispatch:" + _key(REF, AT)
    if body is None:
        body = "\n".join([
            f"Agent findings for {identity}", "",
            "**Summary:** " + FINDINGS["summary"], "",
            "**Findings:**", *["- " + x for x in FINDINGS["findings"]],
            "", "**Evidence:**", *["- " + x for x in FINDINGS["evidence"]],
            "", "**Limitations:** " + FINDINGS["limitations"], "",
            "External services, deployments and administrative jobs are not attested by this Codex process result.",
            f"<!-- {marker} -->",
        ])
    return {"id": comment_id, "user": {"login": author}, "body": body}


class ResultRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "accepted.jsonl").write_text(
            json.dumps({"issue": REF, "at": AT}) + "\n", encoding="utf-8")
        path = self.root / "delivered"
        path.mkdir()
        (path / (_key(REF, AT) + ".json")).write_text(json.dumps({
            "marker": "agent-dispatch:" + _key(REF, AT),
            "status": "finished", "exit_code": 0,
            "findings_state": "validated",
        }), encoding="utf-8")
        self.pages = [[comment()]]
        self.calls = []

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        page = int(argv[-1].split("page=")[-1])
        rows = self.pages[page - 1] if page <= len(self.pages) else []
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows))

    def test_real_case_366_recovered_without_local_findings(self):
        value = report(REF, AT, state_dir=self.root, runner=self.runner)
        self.assertEqual(value["report_state"], "retrieved")
        self.assertEqual(value["findings"], FINDINGS)
        self.assertEqual(value["report_url"],
                         "https://github.com/arthexis/ocpp-csms/issues/366#issuecomment-123")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0:2], ["gh", "api"])

    def test_not_delivered_never_contacts_github(self):
        (self.root / "delivered" / (_key(REF, AT) + ".json")).unlink()
        value = report(REF, AT, state_dir=self.root, runner=self.runner)
        self.assertEqual(value["report_state"], "not_delivered")
        self.assertEqual(self.calls, [])

    def test_rejects_forged_author_and_invalid_comment(self):
        for row, expected in [
            (comment(author="intruder"), "not_found"),
            (comment(identity="2026-11-16T15:20:33.000000Z"), "invalid_report"),
            (comment(body="credential leaked <!-- agent-dispatch:" + _key(REF, AT) + " -->"),
             "invalid_report"),
        ]:
            with self.subTest(expected=expected):
                self.pages = [[row]]
                self.assertEqual(report(REF, AT, state_dir=self.root,
                                        runner=self.runner)["report_state"], expected)

    def test_ambiguous_markers_fail_closed(self):
        self.pages = [[comment(comment_id=1), comment(comment_id=2)]]
        self.assertEqual(report(REF, AT, state_dir=self.root,
                                runner=self.runner)["report_state"], "ambiguous")

    def test_api_failure_is_not_report_absence(self):
        def failed(*args, **kwargs):
            return SimpleNamespace(returncode=1, stdout="token=secret")
        value = report(REF, AT, state_dir=self.root, runner=failed)
        self.assertEqual(value["report_state"], "github_unavailable")
        self.assertNotIn("secret", str(value))

    def test_pagination_finds_exact_identity_on_later_page(self):
        unrelated = {"id": 999, "user": {"login": "arthexis"},
                     "body": "Unrelated comment"}
        self.pages = [[unrelated for _ in range(100)], [comment()]]
        value = report(REF, AT, state_dir=self.root, runner=self.runner)
        self.assertEqual(value["report_state"], "retrieved")
        self.assertEqual(len(self.calls), 2)

    def test_full_page_at_scan_limit_is_incomplete_not_missing(self):
        unrelated = {"id": 999, "user": {"login": "arthexis"},
                     "body": "Unrelated comment"}
        self.pages = [[unrelated for _ in range(100)] for _ in range(10)]
        value = report(REF, AT, state_dir=self.root, runner=self.runner)
        self.assertEqual(value["report_state"], "incomplete_search")
        self.assertEqual(len(self.calls), 10)

    def test_rejects_noncanonical_extra_lines(self):
        row = comment()
        row["body"] = row["body"].replace(
            "**Evidence:**", "unexpected narrative\\n**Evidence:**")
        self.pages = [[row]]
        value = report(REF, AT, state_dir=self.root, runner=self.runner)
        self.assertEqual(value["report_state"], "invalid_report")

    def test_deadline_shared_across_pages(self):
        from gway_agent_ops.result_recovery import TOTAL_BUDGET_SECONDS, PAGE_TIMEOUT_SECONDS
        unrelated = {"id": 9, "user": {"login": "arthexis"},
                     "body": "Unrelated"}
        calls = []
        now = [0.0]
        def clock():
            return now[0]
        def slow_page(argv, **kwargs):
            calls.append(kwargs["timeout"])
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], PAGE_TIMEOUT_SECONDS)
            now[0] += 4.0
            return SimpleNamespace(returncode=0, stdout=json.dumps([unrelated] * 100))
        result = report(REF, AT, state_dir=self.root, runner=slow_page, clock=clock)
        self.assertEqual(result["report_state"], "timeout")
        self.assertLessEqual(len(calls), 4)
        self.assertEqual(calls[:3], [5.0, 5.0, 4.0])
        self.assertLessEqual(now[0], TOTAL_BUDGET_SECONDS)

    def test_subprocess_timeout_is_distinguished_from_github_failure(self):
        import subprocess
        def timed_out(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        result = report(REF, AT, state_dir=self.root, runner=timed_out, clock=lambda: 0.0)
        self.assertEqual(result["report_state"], "timeout")
        self.assertNotIn("findings", result)

    def test_overrunning_runner_cannot_return_partial_findings(self):
        now = [0.0]
        def late(argv, **kwargs):
            now[0] = 13.0
            return SimpleNamespace(returncode=0, stdout=json.dumps([comment()]))
        result = report(REF, AT, state_dir=self.root, runner=late, clock=lambda: now[0])
        self.assertEqual(result["report_state"], "timeout")
        self.assertNotIn("findings", result)

    def test_existing_paginated_results_preserve_frozen_identity(self):
        unrelated = {"id": 11, "user": {"login": "arthexis"},
                     "body": "Unrelated"}
        self.pages = [[unrelated] * 100, [comment()]]
        deadlines = []
        def bounded_runner(argv, **kwargs):
            deadlines.append(kwargs["timeout"])
            return self.runner(argv, **kwargs)
        value = report(REF, AT, state_dir=self.root, runner=bounded_runner,
                       clock=lambda: 0.0)
        self.assertEqual(value["report_state"], "retrieved")
        self.assertEqual(value["at"], AT)
        self.assertEqual(value["findings"], FINDINGS)
        self.assertEqual(deadlines, [5.0, 5.0])

    def test_timeout_without_delivery_does_not_contact_network(self):
        (self.root / "delivered" / (_key(REF, AT) + ".json")).unlink()
        def forbidden(*args, **kwargs):
            self.fail("GitHub must not be invoked")
        value = report(REF, AT, state_dir=self.root, runner=forbidden,
                       clock=lambda: 1000.0)
        self.assertEqual(value["report_state"], "not_delivered")

    def test_mcp_report_is_read_only(self):
        tool = next(t for t in mcp_stdio.TOOLS if t["name"] == "report")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        with patch.object(mcp_stdio, "recover_report",
                          return_value={"report_state": "retrieved"}) as recover:
            value = mcp_stdio.call_tool("report", {"ref": REF, "at": AT})
        self.assertEqual(value["report_state"], "retrieved")
        recover.assert_called_once_with(REF, AT)


if __name__ == "__main__":
    unittest.main()
