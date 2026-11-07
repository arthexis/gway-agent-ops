"""Read-only MCP stdio provider owned by AgentOps.

No mutation tools are registered. GWay Remote applies independent source
registration, release pinning, namespacing and OAuth authorization.
"""
from __future__ import annotations

import json
import os
import re
import sys

from .admission_status import status as admission_status
from .executor import ExecutionError, preview as issue_preview
from .issue_views import list_issues
from .github_issues import IssueRejected
from .observation import observe
from .nonblocking import submit as issue_submit

MAX_MESSAGE = 65536
REF = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")
TOOLS = (
    {"name": "issues", "description": "List trusted issues and eligibility without dispatch.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "status", "description": "Inspect AgentOps admission state without dispatch.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "preview", "description": "Preview the trusted issue prompt; does not execute.",
     "inputSchema": {"type": "object", "properties": {"ref": {"type": "string"}},
                     "required": ["ref"], "additionalProperties": False}},
    {"name": "submit", "description": "Request one trusted issue with frozen UTC identity; requires gway:execute.",
     "inputSchema": {"type": "object", "properties": {"ref": {"type": "string"},
                     "at": {"type": "string"}}, "required": ["ref", "at"],
                     "additionalProperties": False}, "annotations": {"readOnlyHint": False}},
    {"name": "observe", "description": "Observe an existing frozen issue invocation.",
     "inputSchema": {"type": "object", "properties": {"ref": {"type": "string"},
                     "at": {"type": "string"}}, "required": ["ref", "at"],
                     "additionalProperties": False}},
)
TOOLS = tuple({**tool, "annotations": {"readOnlyHint": tool["name"] != "submit"}} for tool in TOOLS)
SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}


def _repositories():
    from .repository_registry import agent_repositories
    return agent_repositories(tuple(x.strip() for x in os.environ.get("GWAY_AGENT_ALLOWED_REPOS", "").split(",") if x.strip()))


def call_tool(name, args):
    schema = SCHEMAS.get(name)
    if schema is None or type(args) is not dict:
        raise ValueError("invalid_tool")
    if set(args) != set(schema.get("required", ())):
        raise ValueError("invalid_arguments")
    if "ref" in args and (type(args["ref"]) is not str or not REF.fullmatch(args["ref"])):
        raise ValueError("invalid_issue_ref")
    if name == "issues":
        return {"issues": list_issues(allowed_repositories=_repositories())}
    if name == "status":
        return {"admission": admission_status()}
    if name == "preview":
        return issue_preview(args["ref"], allowed_repositories=_repositories())
    if name == "submit":
        if type(args["at"]) is not str:
            raise ValueError("invalid_timestamp")
        return issue_submit(args["ref"], args["at"], allowed_repositories=_repositories(),
                            source="remote-api", trigger="mcp-submit")
    if name == "observe":
        if type(args["at"]) is not str:
            raise ValueError("invalid_timestamp")
        return observe(args["ref"], args["at"])
    raise ValueError("invalid_tool")


_SAFE_CODES = frozenset({
    "repository_allowlist_missing", "invalid_repository_allowlist",
    "repository_not_allowed", "github_unavailable", "issue_not_open",
    "not_an_issue", "creator_not_allowed", "target_label_missing",
    "invalid_issue_ref", "invalid_arguments", "invalid_tool",
    "invalid_timestamp", "invalid_github_response", "invalid_issue",
    "invalid_issue_labels", "repository_mismatch", "issue_mismatch",
    "agent_cli_not_found", "agent_workspace_not_configured",
    "agent_workspace_unavailable", "agent_workspace_unsafe",
    "agent_systemd_run_unavailable", "agent_user_bus_unavailable",
})


def _safe_error(exc):
    """Stable machine-readable error without exception text or credentials."""
    code = getattr(exc, "code", None)
    if code is None and type(exc) is ValueError:
        code = str(exc)
    if code not in _SAFE_CODES:
        code = "provider_unavailable"
    return {"content": [{"type": "text", "text": json.dumps({"error": code})}],
            "isError": True}


def handle(request):
    if type(request) is not dict or request.get("jsonrpc") != "2.0":
        raise ValueError("invalid_request")
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    if "id" not in request:
        raise ValueError("invalid_request")
    if method == "initialize":
        result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "gway-agent-ops", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": list(TOOLS)}
    elif method == "tools/call":
        params = request.get("params")
        if type(params) is not dict or type(params.get("name")) is not str:
            raise ValueError("invalid_arguments")
        try:
            data = call_tool(params["name"], params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
        except (ValueError, RuntimeError, OSError) as exc:
            result = _safe_error(exc)
    else:
        raise ValueError("method_not_found")
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def main():
    # stdio protocol: stdout is exclusively JSON-RPC, never diagnostics.
    for raw in sys.stdin.buffer:
        if len(raw) > MAX_MESSAGE:
            continue
        try:
            request = json.loads(raw)
            reply = handle(request)
        except (ValueError, TypeError, UnicodeError):
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "Invalid request"}}
        if reply is not None:
            encoded = json.dumps(reply, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_MESSAGE:
                encoded = json.dumps({"jsonrpc": "2.0", "id": reply.get("id"),
                                      "error": {"code": -32603, "message": "Response too large"}})
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
