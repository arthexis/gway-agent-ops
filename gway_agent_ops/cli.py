"""CLI contract only: this foundation never executes Codex."""
import argparse
import re
import sys
from .contracts import envelope, json_line, status_data
from . import __version__
from .adopt import inspect

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
    issue.add_argument("--resend", action="store_true")
    issue.add_argument("--json", action="store_true", help="Machine-readable response")
    for name in ("issues", "status"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    adopt = sub.add_parser("adopt")
    adopt.add_argument("--check", action="store_true")
    adopt.add_argument("--json", action="store_true")
    hook = sub.add_parser("hook")
    hook.add_argument("event", choices=EVENTS)
    hook.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "issue":
        try:
            parse_issue_ref(args.ref, args.repo)
        except ValueError as exc:
            parser.error(str(exc))
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
