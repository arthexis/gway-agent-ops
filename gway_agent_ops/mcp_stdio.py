"""MCP stdio provider owned by AgentOps.

GWay Remote authenticates and authorizes federated tool calls; this provider
preserves live issue, approval, and execution gates.
"""
from __future__ import annotations

import json
import os
import re
import sys

from .admission_status import status as admission_status
from .codex_budget import read_budgets
from .contracts import status_data
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
    {"name": "schedule", "description": "Persist a future one-shot issue invocation; requires gway:execute.",
     "inputSchema": {"type": "object", "properties": {"ref": {"type": "string"},
                     "run_at": {"type": "string"}, "at": {"type": "string"}}, "required": ["ref", "run_at"],
                     "additionalProperties": False}},
    {"name": "scheduled_list", "description": "List scheduled tasks, without dispatch.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "scheduled_get", "description": "Read a schedule by opaque identifier.",
     "inputSchema": {"type": "object", "properties": {"schedule_id": {"type": "string"}},
                     "required": ["schedule_id"], "additionalProperties": False}},
    {"name": "schedule_cancel", "description": "Cancel an unclaimed schedule; requires gway:execute.",
     "inputSchema": {"type": "object", "properties": {"schedule_id": {"type": "string"}},
                     "required": ["schedule_id"], "additionalProperties": False}},
    {"name": "observe", "description": "Observe an existing frozen issue invocation.",
     "inputSchema": {"type": "object", "properties": {"ref": {"type": "string"},
                     "at": {"type": "string"}}, "required": ["ref", "at"],
                     "additionalProperties": False}},
)
TOOLS = tuple({**tool, "annotations": {"readOnlyHint": tool["name"] not in ("submit", "schedule", "schedule_cancel")}} for tool in TOOLS)
SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}


def call_tool(name, args):
    schema = SCHEMAS.get(name)
    if schema is None or type(args) is not dict:
        raise ValueError("invalid_tool")
    if not set(schema.get("required", ())).issubset(args) or not set(args).issubset(schema.get("properties", {})):
        raise ValueError("invalid_arguments")
    if "ref" in args and (type(args["ref"]) is not str or not REF.fullmatch(args["ref"])):
        raise ValueError("invalid_issue_ref")
    if name == "issues":
        return {"issues": list_issues()}
    if name == "status":
        admission = admission_status()
        data = status_data(active=not admission["available"], **read_budgets())
        data["admission"] = admission
        return data
    if name == "preview":
        return issue_preview(args["ref"])
    if name == "submit":
        if type(args["at"]) is not str:
            raise ValueError("invalid_timestamp")
        return issue_submit(args["ref"], args["at"],
                            source="remote-api", trigger="mcp-submit")
    if name in ("schedule", "scheduled_list", "scheduled_get", "schedule_cancel"):
        from . import scheduler
        if name == "schedule":
            if type(args["run_at"]) is not str:
                raise ValueError("invalid_run_at")
            return scheduler.create(args["ref"], args["run_at"], request_at=args.get("at"))
        if name == "scheduled_list":
            return {"schedules": scheduler.list_schedules()}
        if name == "scheduled_get":
            return scheduler.inspect(args["schedule_id"])
        return scheduler.cancel(args["schedule_id"])
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
    "invalid_run_at", "timezone_required", "run_at_out_of_range",
    "invalid_schedule_id", "schedule_not_found", "schedule_not_cancellable",
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
