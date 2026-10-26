import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from gway_agent_ops.audit_receipt import validate, parse
from gway_agent_ops.audit import audit
from gway_agent_ops.observation import observe
from gway_agent_ops.reporting import flush


AT = "2026-10-13T01:00:00.000000Z"
ISSUE = "arthexis/example#19"
SCOPE = "T1-T4"


def receipt():
    return {"version": 1, "issue": ISSUE, "at": AT, "scope": SCOPE,
            "checks": {"T1": "PASS", "T2": "BLOCKED",
                       "T3": "UNKNOWN", "T4": "PASS"},
            "summary": "Offline checks completed"}


class ReceiptTests(unittest.TestCase):
    def test_valid_and_rejects_untrusted_content(self):
        self.assertEqual(parse(json.dumps(receipt()), issue=ISSUE, at=AT, scope=SCOPE), receipt())
        invalid = [
            {"extra": "x"},
            {"summary": "<script>"},
            {"summary": "token ghp_abcdef"},
            {"summary": "192.168.1.2"},
            {"summary": "a" * 241},
            {"checks": {"T1": "PASS", "T2": "PASS", "T3": "PASS", "T4": "SUCCESS"}},
        ]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate({**receipt(), **change}, issue=ISSUE, at=AT, scope=SCOPE)

    def test_identity_mismatch(self):
        with self.assertRaises(ValueError):
            validate(receipt(), issue=ISSUE, at=AT, scope="different")


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkout = self.root / "arthexis" / "example"
        self.checkout.mkdir(parents=True)
        (self.checkout / ".git").mkdir()
        self.state = self.root / "state"

    def run_audit(self, *, output=True, dirty_after=False, exit_code=0, raw=None,
                  scope=SCOPE):
        calls = []
        def git_runner(argv, **options):
            calls.append(argv)
            change = " M app.py" if dirty_after and len(calls) == 2 else ""
            return subprocess.CompletedProcess(argv, 0, change, "")
        def runner(argv, **options):
            self.audit_prompt = options["input"]
            self.assertIn("read-only", argv)
            self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
            self.assertNotIn("workspace-write", argv)
            if output:
                p = Path(argv[argv.index("--output-last-message") + 1])
                document = receipt()
                document["scope"] = scope
                p.write_text(raw if raw is not None else json.dumps(document))
            return subprocess.CompletedProcess(argv, exit_code, "SECRET-RAW-STDOUT", "SECRET-RAW-STDERR")
        def resolver(ref, **kwargs):
            return {"repository": "arthexis/example", "number": 19, "comments": 0,
                    "title": "Audit", "body": "Only inspect", "labels": ["gway-001"],
                    "url": "https://github.com/arthexis/example/issues/19"}
        def runtime():
            home = self.root / "codex-home"
            home.mkdir(exist_ok=True)
            (home / "config.toml").write_text("test")
            return {"authentication_available": True, "codex_home": str(home),
                    "environment": {"CODEX_HOME": str(home)}}
        result = audit(ISSUE, scope=scope, at=AT, allowed_repositories=("arthexis/example",),
                       workspace_root=self.root, state_dir=self.state, resolver=resolver,
                       runner=runner, git_runner=git_runner, runtime=runtime)
        return result

    def test_valid_receipt_and_observation(self):
        result = self.run_audit()
        self.assertEqual(result["receipt_state"], "receipt_valid")
        observed = observe(ISSUE, AT, state_dir=self.state)
        self.assertTrue(observed["execution_terminal"])
        self.assertTrue(observed["receipt_retained"])
        self.assertFalse(observed["delivery_confirmed"])
        self.assertNotIn("SECRET-RAW", str(list(self.state.rglob("*.json"))))
        data = "\n".join(p.read_text() for p in self.state.rglob("*.json"))
        self.assertNotIn("SECRET-RAW", data)

    def test_zero_exit_without_receipt_does_not_pass(self):
        result = self.run_audit(output=False)
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["receipt_state"], "receipt_missing")
        self.assertFalse(observe(ISSUE, AT, state_dir=self.state)["receipt_retained"])

    def test_generic_scope_remains_offline_only(self):
        self.run_audit()
        self.assertIn("Do not execute remote or network operations.", self.audit_prompt)
        self.assertNotIn("bounded ICMP probe", self.audit_prompt)

    def test_exact_field_scope_allows_only_bounded_read_only_checks(self):
        self.run_audit(scope="gway-004-wireguard-readonly-v1")
        self.assertIn("bounded ICMP probe", self.audit_prompt)
        self.assertIn("TCP connection test to port 22", self.audit_prompt)
        self.assertIn("Never reboot", self.audit_prompt)
        self.assertIn("Never prompt for, add, copy, replace, or relax SSH", self.audit_prompt)

    def test_workspace_diff_blocks_receipt(self):
        result = self.run_audit(dirty_after=True)
        self.assertEqual(result["receipt_state"], "audit_workspace_modified")
        self.assertIsNone(result["receipt"])

    def test_github_failure_retains_receipt_and_delivery_attempt(self):
        self.run_audit()
        def failing(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "", "")
        self.assertEqual(flush(state_dir=self.state, runner=failing)["pending"], 1)
        observed = observe(ISSUE, AT, state_dir=self.state)
        self.assertTrue(observed["receipt_retained"])
        self.assertTrue(observed["delivery_attempted"])
        self.assertFalse(observed["delivery_confirmed"])

    def test_invalid_receipt_does_not_post_raw(self):
        result = self.run_audit(raw=json.dumps({**receipt(), "summary": "password secret"}))
        self.assertEqual(result["receipt_state"], "receipt_invalid")
        posted = []
        def gh(argv, **kwargs):
            if "per_page=100" in str(argv):
                return subprocess.CompletedProcess(argv, 0, "[]", "")
            posted.append(argv)
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        self.assertEqual(flush(state_dir=self.state, runner=gh)["sent"], 1)
        self.assertNotIn("password secret", str(posted))


if __name__ == "__main__":
    unittest.main()
