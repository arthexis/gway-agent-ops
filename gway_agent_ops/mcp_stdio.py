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
from .executor import preview as issue_preview
from .issue_views import list_issues
from .observation import observe

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
    {"name": "observe", "description": "Observe an existing frozen issue invocation.",
     "inputSchema": {"type": "object", "properties": {"ref": {"type": "string"},
                     "at": {"type": "string"}}, "required": ["ref", "at"],
                     "additionalProperties": False}},
)
TOOLS = tuple({**tool, "annotations": {"readOnlyHint": True}} for tool in TOOLS)
SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}


def _repositories():
    from .repository_registry import agent_repositories
    return tuple(agent_repositories())


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
    if name == "observe":
        if type(args["at"]) is not str:
            raise ValueError("invalid_timestamp")
        return observe(args["ref"], args["at"])
    raise ValueError("invalid_tool")


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
        except (ValueError, RuntimeError, OSError):
            result = {"content": [{"type": "text", "text": "AgentOps read-only operation unavailable"}],
                      "isError": True}
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
