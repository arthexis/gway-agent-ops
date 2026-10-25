"""CLI contract only: this foundation never executes Codex."""
from .repository_registry import agent_repositories
import argparse
import os
from pathlib import Path
import re
import sys
from .contracts import envelope, json_line, status_data
from . import __version__
from .adopt import inspect, adopt as adopt_config
from .environment import load_runtime_environment
from .verify import verify
from .dispatch import timestamp

REF = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
EVENTS = ("SessionStart", "UserPromptSubmit", "PermissionRequest",
          "PreCompact", "PostCompact", "SubagentStart", "SubagentStop", "Stop")
NOT_IMPLEMENTED = 3


def parse_issue_ref(ref, repo=None):
    match = REF.fullmatch(ref)
    if match:
        if repo and repo != match.group(1):
            raise ValueError("conflicting repository")
        return match.group(1), int(match.group(2))
    if re.fullmatch(r"[1-9][0-9]*", ref) and repo and REPO.fullmatch(repo):
        return repo, int(ref)
    raise ValueError("expected OWNER/REPO#NUMBER or NUMBER --repo OWNER/REPO")


def build_parser():
    parser = argparse.ArgumentParser(prog="agent", description="On-demand GWAY agent operations")
    parser.add_argument("--version", action="version", version=f"agent {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue", help="Invoke an agent for a GitHub issue")
    issue.add_argument("ref")
    issue.add_argument("--repo")
    issue.add_argument("--at", metavar="TIMESTAMP", help="Frozen ISO-8601 dispatch timestamp for idempotent retries")
    issue.add_argument("--json", action="store_true", help="Machine-readable response")
    submission = sub.add_parser("submit", help="Request a trusted issue without waiting for execution")
    submission.add_argument("ref")
    submission.add_argument("--at", required=True)
    submission.add_argument("--origin", choices=("direct-cli", "remote-api"), default="direct-cli")
    submission.add_argument("--json", action="store_true")
    report = sub.add_parser("report", help="Deliver retained GitHub reports without invoking Codex")
    report.add_argument("--json", action="store_true")
    release_parser = sub.add_parser("release", help="Retire an unresolved submission after stopping its unit")
    release_parser.add_argument("ref")
    release_parser.add_argument("--at", required=True)
    release_parser.add_argument("--json", action="store_true")
    preview_parser = sub.add_parser("preview", help="Read the trusted issue execution prompt without dispatch")
    preview_parser.add_argument("ref")
    preview_parser.add_argument("--json", action="store_true")
    observation = sub.add_parser("observe", help="Inspect a previously submitted issue and timestamp")
    observation.add_argument("ref")
    observation.add_argument("--at", required=True)
    observation.add_argument("--json", action="store_true")
    for name in ("issues", "status"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    adopt = sub.add_parser("adopt")
    modes = adopt.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--verify", action="store_true")
    adopt.add_argument("--include-auth", action="store_true")
    adopt.add_argument("--with-sound", action="store_true", help="Install supported Codex completion notification")
    adopt.add_argument("--force", action="store_true")
    adopt.add_argument("--json", action="store_true")
    notify = sub.add_parser("notify", help="Handle supported Codex external notify JSON payload")
    notify.add_argument("payload")
    notify.add_argument("--json", action="store_true")
    hook = sub.add_parser("hook")
    hook.add_argument("event", choices=EVENTS)
    hook.add_argument("--json", action="store_true")
    worker = sub.add_parser("discover-worker", help="Persistent discovery only; never execute")
    worker.add_argument("--interval", type=int, default=60, metavar="SECONDS")
    worker.add_argument("--json", action="store_true")
    discovery = sub.add_parser("discover-tick", help="One read-only discovery pass; no execution")
    discovery.add_argument("--json", action="store_true")
    tasks = sub.add_parser("tasks", help="Read durable task ledger; never dispatch")
    tasks.add_argument("--json", action="store_true")
    tasks.add_argument("--issue", help="Optional issue identity for task and audit events")
    registry = sub.add_parser("registry", help="Inspect or import validated Remote targets; no execution")
    registry.add_argument("--json", action="store_true")
    registry.add_argument("--refresh-file", type=Path, help="Read a Remote deploy-targets JSON snapshot")
    registry.add_argument("--sync", action="store_true", help="Read installed gway-remote deploy-targets export")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        load_runtime_environment()
    except (OSError, UnicodeError, ValueError):
        result = envelope(args.command, "error", error={
            "code": "runtime_configuration_invalid",
            "message": "Agent runtime configuration is unavailable"})
        if getattr(args, "json", False):
            print(json_line(result))
        else:
            print("agent: runtime configuration is unavailable", file=sys.stderr)
        return 1
    if args.command == "issue":
        try:
            parse_issue_ref(args.ref, args.repo)
            if args.at is not None:
                timestamp(args.at)
        except ValueError as exc:
            parser.error(str(exc))
    if args.command == "discover-worker":
        from .discovery_worker import run_forever
        import signal
        path = Path(os.environ.get("GWAY_AGENT_LEDGER_PATH",
                       str(Path.home() / ".local/state/gway-agent-ops/tasks.sqlite3")))
        stopping = False
        def stop_worker(signum, frame):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop_worker)
        signal.signal(signal.SIGINT, stop_worker)
        try:
            run_forever(ledger_path=path, interval=args.interval,
                        stop=lambda: stopping,
                        report=lambda result: print(json_line(envelope(
                            "discover-worker", "degraded" if result["errors"] else "ok",
                            data=result)), flush=True))
        except ValueError as exc:
            parser.error(str(exc))
        return 0
    if args.command == "discover-tick":
        from .task_ledger import TaskLedger
        from .discovery_worker import tick
        path = Path(os.environ.get("GWAY_AGENT_LEDGER_PATH",
                       str(Path.home() / ".local/state/gway-agent-ops/tasks.sqlite3")))
        try:
            with TaskLedger(path) as ledger:
                data = tick(ledger)
            print(json_line(envelope("discover-tick",
                                     "degraded" if data["errors"] else "ok", data=data)))
            return 1 if data["errors"] else 0
        except (OSError, ValueError) as exc:
            print(json_line(envelope("discover-tick", "error",
                                     error={"code": "discovery_unavailable",
                                            "message": str(exc)})))
            return 1
    if args.command == "tasks":
        from .task_ledger import TaskLedger
        path = Path(os.environ.get("GWAY_AGENT_LEDGER_PATH",
                       str(Path.home() / ".local/state/gway-agent-ops/tasks.sqlite3")))
        try:
            with TaskLedger(path) as ledger:
                data = {"tasks": ledger.list_tasks(), "registry": ledger.registry_status()}
                if args.issue is not None:
                    data["tasks"] = [row for row in data["tasks"] if row["issue"] == args.issue]
                    data["events"] = ledger.events_for(args.issue)
                    data["provenance"] = ledger.provenance_for(args.issue)
            result, code = envelope("tasks", "ok", data=data), 0
        except (OSError, ValueError):
            result, code = envelope("tasks", "error", error={
                "code": "ledger_unavailable", "message": "Task ledger unavailable"}), 1
        print(json_line(result) if args.json else
              (data["tasks"] if code == 0 else "Task ledger unavailable"))
        return code
    if args.command == "registry":
        from .task_ledger import TaskLedger
        import json
        path = Path(os.environ.get("GWAY_AGENT_LEDGER_PATH",
                       str(Path.home() / ".local/state/gway-agent-ops/tasks.sqlite3")))
        try:
            with TaskLedger(path) as ledger:
                if args.sync and args.refresh_file is not None:
                    raise ValueError("choose --sync or --refresh-file")
                if args.sync:
                    import subprocess
                    try:
                        output = subprocess.run(
                            ["gway-remote", "deploy-targets", "--json"],
                            capture_output=True, text=True, check=True, timeout=20)
                        ledger.refresh_repositories(output.stdout)
                    except (OSError, subprocess.SubprocessError, ValueError):
                        ledger.invalidate_registry()
                        raise ValueError("Remote target export unavailable")
                if args.refresh_file is not None:
                    try:
                        raw = args.refresh_file.read_text(encoding="utf-8")
                    except OSError:
                        ledger.invalidate_registry()
                        raise
                    ledger.refresh_repositories(raw)
                data = ledger.registry_status()
            result = envelope("registry", "ok" if data["healthy"] else "degraded", data=data)
            code = 0 if data["healthy"] else 1
        except (OSError, ValueError) as exc:
            result = envelope("registry", "error", error={
                "code": "registry_unavailable", "message": str(exc)})
            code = 1
        print(json_line(result) if args.json else
              (result["data"]["repositories"] if code == 0 else "Registry unavailable"))
        return code
    if args.command == "submit":
        from .nonblocking import submit
        from .github_issues import IssueRejected
        try:
            allowed = agent_repositories(tuple(x.strip() for x in os.environ.get("GWAY_AGENT_ALLOWED_REPOS", "").split(",") if x.strip()))
            data = submit(args.ref, args.at, allowed_repositories=allowed,
                          source=args.origin, trigger=("authenticated-agent-issue"
                          if args.origin == "remote-api" else "cli-submit"))
            result = envelope("submit", "ok", data=data)
            code = 0
        except IssueRejected as exc:
            reason = exc.code if exc.code in ("target_label_missing", "invalid_issue_labels") else "submission_unavailable"
            message = ("Add the exact gway-001 label before sending"
                       if reason == "target_label_missing" else "Issue label metadata unavailable"
                       if reason == "invalid_issue_labels" else "Unable to request invocation")
            result = envelope("submit", "error", error={"code": reason, "message": message})
            code = 1
        except (ValueError, OSError, KeyError, TypeError):
            result = envelope("submit", "error", error={
                "code": "submission_unavailable", "message": "Unable to request invocation"})
            code = 1
        if args.json:
            print(json_line(result))
        else:
            print(result["data"]["state"] if code == 0 else "Submission unavailable")
        return code
    if args.command == "report":
        from .reporting import flush
        try:
            data = flush()
            status, code = ("ok", 0) if data["pending"] == 0 else ("error", 1)
            result = envelope("report", status, data=data,
                              error=None if code == 0 else {
                                  "code": "report_delivery_pending",
                                  "message": "Some durable reports remain queued"})
        except (OSError, ValueError, RuntimeError):
            code = 1
            result = envelope("report", "error", error={
                "code": "report_delivery_unavailable",
                "message": "Unable to process durable reports"})
        print(json_line(result) if args.json else
              (str(result["data"]) if code == 0 else "Report delivery incomplete"))
        return code
    if args.command == "status":
        from .admission_status import status as admission_status
        try:
            admission = admission_status()
            from .codex_budget import read_budgets
            budgets = read_budgets()
            data = status_data(active=not admission["available"], **budgets)
            data["admission"] = admission
            result, code = envelope("status", "ok", data=data), 0
        except (ValueError, OSError, KeyError, TypeError):
            result, code = envelope("status", "error", error={
                "code": "status_unavailable", "message": "Admission state unavailable"}), 1
        print(json_line(result) if args.json else
              (admission["state"] if code == 0 else "Status unavailable"))
        return code
    if args.command == "release":
        from .nonblocking import release
        try:
            data = release(args.ref, args.at)
            result, code = envelope("release", "ok", data=data), 0
        except (ValueError, OSError):
            result, code = envelope("release", "error", error={
                "code": "release_unavailable", "message": "Cannot safely release request"}), 1
        print(json_line(result) if args.json else
              (result["data"]["state"] if code == 0 else "Release unavailable"))
        return code
    if args.command == "preview":
        from .executor import preview, ExecutionError
        from .github_issues import IssueRejected
        try:
            allowed = agent_repositories(tuple(x.strip() for x in os.environ.get("GWAY_AGENT_ALLOWED_REPOS", "").split(",") if x.strip()))
            data = preview(args.ref, allowed_repositories=allowed)
            result, code = envelope("preview", "ok", data=data), 0
        except (ValueError, OSError, IssueRejected, ExecutionError):
            result, code = envelope("preview", "error", error={"code": "preview_unavailable", "message": "Trusted prompt unavailable"}), 1
        print(json_line(result) if args.json else (data["prompt"] if code == 0 else "Prompt unavailable"))
        return code
    if args.command == "observe":
        from .observation import observe
        try:
            data = observe(args.ref, args.at)
            result = envelope("observe", "ok", data=data)
            code = 0
        except (ValueError, OSError, KeyError, TypeError):
            result = envelope("observe", "error", error={
                "code": "observation_unavailable", "message": "Unable to inspect invocation"})
            code = 1
        if args.json:
            print(json_line(result))
        else:
            print(result["data"]["state"] if code == 0 else "Observation unavailable")
        return code
    if args.command == "issue":
        from .dispatch import Busy, Duplicate
        from .github_issues import IssueRejected
        from .executor import ExecutionError, execute
        try:
            allowed = agent_repositories(tuple(x.strip() for x in os.environ.get("GWAY_AGENT_ALLOWED_REPOS", "").split(",") if x.strip()))
            workspace = os.environ.get("GWAY_AGENT_WORKSPACE_ROOT", "")
            if not workspace:
                raise ExecutionError("workspace_root_missing")
            data = execute(args.ref, repo=args.repo, at=args.at,
                           allowed_repositories=allowed, workspace_root=Path(workspace))
            status = data["status"]
            code = 0 if status == "finished" else 1
            error = None if code == 0 else {"code": "codex_failed", "message": "Codex returned nonzero"}
        except (Busy, Duplicate, IssueRejected, ExecutionError, OSError, ValueError) as exc:
            status = ("busy" if isinstance(exc, Busy) else
                      "duplicate" if isinstance(exc, Duplicate) else "error")
            code = 4 if isinstance(exc, Busy) else (5 if isinstance(exc, Duplicate) else 1)
            error = {"code": getattr(exc, "code", status), "message": str(exc)}
            data = None
        # Reporting failure is retained in the durable outbox; never rerun Codex.
        from .reporting import flush
        try:
            flush()
        except (OSError, ValueError):
            pass
        if args.json:
            print(json_line(envelope("issue", status, data=data, error=error)))
        elif error:
            print(f"agent issue: {error['message']}", file=sys.stderr)
        else:
            print(f"Codex returned for {data['issue']} at {data['at']}; exit {data['exit_code']}")
        return code
    if args.command == "issues":
        from .issue_views import list_issues
        from .github_issues import IssueRejected
        try:
            allowed = agent_repositories(tuple(x.strip() for x in os.environ.get("GWAY_AGENT_ALLOWED_REPOS", "").split(",") if x.strip()))
            data = {"issues": list_issues(allowed_repositories=allowed),
                    "agent": __import__("socket").gethostname().lower()}
            status, error, code = "ok", None, 0
        except (IssueRejected, OSError, ValueError) as exc:
            data, status, code = None, "error", 1
            error = {"code": getattr(exc, "code", "issues_unavailable"),
                     "message": "Issue listing unavailable"}
        if args.json:
            print(json_line(envelope("issues", status, data=data, error=error)))
        elif error:
            print("agent issues: issue listing unavailable", file=sys.stderr)
        else:
            for row in data["issues"]:
                print(f'{row["repository"]}#{row["number"]} {row["state"]} ({row["comments"]} comments): {row["title"]}')
        return code
    if args.command == "notify":
        from .sound_notify import dispatch
        try:
            data = dispatch(args.payload)
            status, error, code = "ok", None, 0
        except ValueError:
            data, status, error, code = None, "error", {"code": "invalid_notification", "message": "Invalid notification"}, 1
        if args.json:
            print(json_line(envelope("notify", status, data=data, error=error)))
        return code
    if args.command == "adopt" and args.verify:
        result = verify()
        if args.json:
            print(json_line(envelope("adopt", "ok" if result["ready"] else "unavailable",
                                     data=result,
                                     error=None if result["ready"] else
                                     {"code": "not_ready", "message": "Agent configuration is not ready"})))
        else:
            print("Agent runtime: " + ("ready" if result["ready"] else "not ready"))
            if not result["ready"]:
                print("Problems: " + ", ".join(result["issues"]))
        return 0 if result["ready"] else 1
    if args.command == "adopt" and args.check:
        data = inspect()
        if args.json:
            print(json_line(envelope("adopt", "ok", data=data)))
        else:
            print("Codex: " + ("available" if data["codex_executable"]["available"] else "unavailable"))
            print("Configuration: " + ("readable" if data["config"]["readable"] else "unavailable"))
            print("Authentication: " + data["authentication"]["status"])
            print("Adoption: read-only check; no changes made")
        return 0
    if args.command == "adopt":
        try:
            result = adopt_config(include_auth=args.include_auth, force=args.force, with_sound=args.with_sound)
        except (OSError, ValueError) as exc:
            if args.json:
                print(json_line(envelope("adopt", "error", error={"code": "adoption_failed", "message": str(exc)})))
            else:
                print(f"agent adopt: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json_line(envelope("adopt", "ok", data=result)))
        else:
            print("Agent configuration copied to " + result["destination"])
        return 0
    if args.json:
        print(json_line(envelope(
            args.command, "not_implemented",
            data=None,
            error={"code": "not_implemented",
                   "message": "Command implementation is not available in A1"})))
    else:
        print(f"agent {args.command}: not implemented (A1 contract only)", file=sys.stderr)
    return NOT_IMPLEMENTED


if __name__ == "__main__":
    raise SystemExit(main())
