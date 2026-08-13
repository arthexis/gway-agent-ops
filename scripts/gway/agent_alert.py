#!/usr/bin/env python3
"""Local urgent-alert broker for agent attention requests."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


VERSION = "0.1.0"
DEFAULT_ROOT = Path(os.environ.get("AGENT_ALERT_ROOT", "/home/arthe/agent-alerts"))
REQUEST_ID_RE = re.compile(r"\b(20\d{6}T\d{6}Z-[0-9a-f]{8})\b", re.IGNORECASE)


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_request_id() -> str:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def root_path(value: str | Path | None = None) -> Path:
    return Path(value or DEFAULT_ROOT).expanduser().resolve()


def ensure_dirs(root: Path) -> None:
    for child in ("requests", "responses", "outbox", "logs"):
        (root / child).mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def request_path(root: Path, request_id: str) -> Path:
    return root / "requests" / f"{request_id}.json"


def response_path(root: Path, request_id: str) -> Path:
    return root / "responses" / f"{request_id}.json"


def outbox_path(root: Path, request_id: str) -> Path:
    return root / "outbox" / f"{request_id}.txt"


def pending_requests(root: Path) -> list[dict[str, Any]]:
    ensure_dirs(root)
    pending: list[dict[str, Any]] = []
    for path in sorted((root / "requests").glob("*.json")):
        try:
            request = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        request_id = str(request.get("id") or path.stem)
        if not response_path(root, request_id).exists():
            pending.append(request)
    return pending


def format_whatsapp_body(request: dict[str, Any]) -> str:
    title = str(request.get("title") or "Agent needs attention")
    message = str(request.get("message") or "")
    request_id = str(request["id"])
    agent = str(request.get("agent") or "agent")
    severity = str(request.get("severity") or "urgent")
    return "\n".join(
        [
            f"[{severity.upper()}] {title}",
            f"Agent: {agent}",
            f"ID: {request_id}",
            "",
            message,
            "",
            f"Reply with: {request_id} <answer>",
        ]
    )


def normalize_whatsapp_address(value: str) -> str:
    value = value.strip()
    if value.startswith("whatsapp:"):
        return value
    return f"whatsapp:{value}"


def send_file_outbox(root: Path, request: dict[str, Any], body: str, reason: str = "") -> dict[str, Any]:
    payload = body
    if reason:
        payload += f"\n\n[file-outbox reason: {reason}]\n"
    atomic_write(outbox_path(root, str(request["id"])), payload)
    return {"provider": "file", "status": "queued", "path": str(outbox_path(root, str(request["id"])))}


def send_twilio(root: Path, request: dict[str, Any], body: str) -> dict[str, Any]:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.environ.get("TWILIO_WHATSAPP_FROM", "").strip()
    to_number = os.environ.get("AGENT_WHATSAPP_TO", os.environ.get("TWILIO_WHATSAPP_TO", "")).strip()
    missing = [
        name
        for name, value in (
            ("TWILIO_ACCOUNT_SID", account_sid),
            ("TWILIO_AUTH_TOKEN", auth_token),
            ("TWILIO_WHATSAPP_FROM", from_number),
            ("AGENT_WHATSAPP_TO", to_number),
        )
        if not value
    ]
    if missing:
        return send_file_outbox(root, request, body, "missing Twilio env: " + ", ".join(missing))

    url = f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(account_sid)}/Messages.json"
    form = urllib.parse.urlencode(
        {
            "To": normalize_whatsapp_address(to_number),
            "From": normalize_whatsapp_address(from_number),
            "Body": body,
        }
    ).encode("utf-8")
    auth = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    request_obj = urllib.request.Request(
        url,
        data=form,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=30) as response:
            response_body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", "replace")
        result = {"provider": "twilio", "status": "error", "http_status": exc.code, "body": error_body}
        atomic_write_json(root / "outbox" / f"{request['id']}.send.json", result)
        return result
    except OSError as exc:
        result = {"provider": "twilio", "status": "error", "error": str(exc)}
        atomic_write_json(root / "outbox" / f"{request['id']}.send.json", result)
        return result

    result = {"provider": "twilio", "status": "sent", "http_status": status, "body": response_body}
    atomic_write_json(root / "outbox" / f"{request['id']}.send.json", result)
    return result


def dispatch_notification(root: Path, request: dict[str, Any], provider: str) -> dict[str, Any]:
    body = format_whatsapp_body(request)
    if provider == "auto":
        provider = os.environ.get("AGENT_ALERT_PROVIDER", "file").strip().lower() or "file"
    if provider == "twilio":
        return send_twilio(root, request, body)
    if provider == "file":
        return send_file_outbox(root, request, body)
    raise SystemExit(f"unsupported provider: {provider}")


def create_request(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    ensure_dirs(root)
    request_id = new_request_id()
    response = response_path(root, request_id)
    request = {
        "id": request_id,
        "created_at": utc_now(),
        "agent": args.agent,
        "severity": args.severity,
        "title": args.title,
        "message": args.message,
        "request_path": str(request_path(root, request_id)),
        "response_path": str(response),
        "status": "pending",
    }
    atomic_write_json(request_path(root, request_id), request)
    notification = dispatch_notification(root, request, args.provider) if not args.no_send else {"status": "skipped"}
    print(f"id={request_id}")
    print(f"request_path={request_path(root, request_id)}")
    print(f"response_path={response}")
    print(f"notification_provider={notification.get('provider', args.provider)}")
    print(f"notification_status={notification.get('status')}")
    if notification.get("path"):
        print(f"notification_path={notification['path']}")
    if args.wait:
        return wait_for_response(args, request_id=request_id)
    return 0


def write_response(root: Path, request_id: str, body: str, sender: str, source: str, raw: Any = None) -> Path:
    ensure_dirs(root)
    if not request_path(root, request_id).exists():
        raise SystemExit(f"unknown request id: {request_id}")
    payload: dict[str, Any] = {
        "id": request_id,
        "received_at": utc_now(),
        "from": sender,
        "source": source,
        "message": body,
    }
    if raw is not None:
        payload["raw"] = raw
    path = response_path(root, request_id)
    atomic_write_json(path, payload)
    return path


def choose_request_id(root: Path, text: str) -> str:
    match = REQUEST_ID_RE.search(text)
    if match:
        return match.group(1)
    pending = pending_requests(root)
    if len(pending) == 1:
        return str(pending[0]["id"])
    if not pending:
        raise SystemExit("no pending requests; include the request id in the response")
    raise SystemExit("multiple pending requests; include the request id in the response")


def wait_for_response(args: argparse.Namespace, request_id: str | None = None) -> int:
    root = root_path(args.root)
    ensure_dirs(root)
    rid = request_id or args.id
    deadline = time.monotonic() + args.timeout if args.timeout else None
    path = response_path(root, rid)
    while True:
        if path.exists():
            response = read_json(path)
            print(f"response_path={path}")
            print(f"response_from={response.get('from', '')}")
            print(f"response_message={response.get('message', '')}")
            return 0
        if deadline is not None and time.monotonic() >= deadline:
            print(f"timeout_waiting_for={rid}", file=sys.stderr)
            return 124
        time.sleep(args.interval)


def respond(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    path = write_response(root, args.id, args.message, args.sender, "manual")
    print(f"response_path={path}")
    return 0


def list_requests(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    ensure_dirs(root)
    for request in pending_requests(root):
        print(f"{request['id']}\t{request.get('severity', '')}\t{request.get('agent', '')}\t{request.get('title', '')}")
    return 0


def show_request(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    request = read_json(request_path(root, args.id))
    response_file = response_path(root, args.id)
    if response_file.exists():
        request["response"] = read_json(response_file)
        request["status"] = "responded"
    print(json.dumps(request, indent=2, sort_keys=True))
    return 0


class AlertWebhookHandler(BaseHTTPRequestHandler):
    server_version = "agent-alert-webhook/0.1"

    @property
    def alert_root(self) -> Path:
        return self.server.alert_root  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        line = f"{utc_now()} {self.address_string()} {fmt % args}\n"
        with (self.alert_root / "logs" / "webhook.log").open("a", encoding="utf-8") as handle:
            handle.write(line)

    def write_text(self, status: int, body: str, content_type: str = "text/plain") -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self.write_text(200, "ok\n")
            return
        if parsed.path == "/meta":
            query = urllib.parse.parse_qs(parsed.query)
            expected = os.environ.get("AGENT_ALERT_META_VERIFY_TOKEN", "")
            token = query.get("hub.verify_token", [""])[0]
            challenge = query.get("hub.challenge", [""])[0]
            if expected and token == expected and challenge:
                self.write_text(200, challenge)
                return
            self.write_text(403, "verification failed\n")
            return
        self.write_text(404, "not found\n")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/twilio":
            self.handle_twilio()
            return
        if parsed.path == "/meta":
            self.handle_meta()
            return
        self.write_text(404, "not found\n")

    def handle_twilio(self) -> None:
        body = self.read_body().decode("utf-8", "replace")
        fields = urllib.parse.parse_qs(body)
        message = fields.get("Body", [""])[0].strip()
        sender = fields.get("From", [""])[0].strip()
        try:
            request_id = choose_request_id(self.alert_root, message)
            path = write_response(self.alert_root, request_id, message, sender, "twilio", fields)
        except SystemExit as exc:
            self.write_text(400, f"<Response><Message>{exc}</Message></Response>\n", "application/xml")
            return
        self.write_text(200, f"<Response><Message>Captured response for {path.stem}</Message></Response>\n", "application/xml")

    def handle_meta(self) -> None:
        try:
            payload = json.loads(self.read_body().decode("utf-8", "replace"))
            value = payload["entry"][0]["changes"][0]["value"]
            message_obj = value["messages"][0]
            message = message_obj.get("text", {}).get("body", "").strip()
            sender = message_obj.get("from", "")
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            self.write_text(400, f"invalid meta payload: {exc}\n")
            return
        try:
            request_id = choose_request_id(self.alert_root, message)
            path = write_response(self.alert_root, request_id, message, sender, "meta", payload)
        except SystemExit as exc:
            self.write_text(400, f"{exc}\n")
            return
        self.write_text(200, f"captured response for {path.stem}\n")


def run_webhook_server(args: argparse.Namespace) -> int:
    root = root_path(args.root)
    ensure_dirs(root)
    server = ThreadingHTTPServer((args.host, args.port), AlertWebhookHandler)
    server.alert_root = root  # type: ignore[attr-defined]
    print(f"listening=http://{args.host}:{args.port}", flush=True)
    print(f"twilio_webhook=http://{args.host}:{args.port}/twilio", flush=True)
    print(f"meta_webhook=http://{args.host}:{args.port}/meta", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create urgent agent alerts and capture operator responses.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="alert state directory")
    parser.add_argument("--version", action="version", version=f"agent-alert {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ask = subparsers.add_parser("ask", help="create an urgent attention request")
    ask.add_argument("message", help="message sent to the operator")
    ask.add_argument("--title", default="Agent needs urgent attention")
    ask.add_argument("--agent", default=os.environ.get("AGENT_NAME", os.environ.get("USER", "agent")))
    ask.add_argument("--severity", default="urgent")
    ask.add_argument("--provider", choices=("auto", "file", "twilio"), default="auto")
    ask.add_argument("--no-send", action="store_true", help="write request only; do not dispatch notification")
    ask.add_argument("--wait", action="store_true", help="wait for a response file after creating the request")
    ask.add_argument("--timeout", type=float, default=0, help="seconds to wait; 0 means forever")
    ask.add_argument("--interval", type=float, default=1.0, help="wait polling interval")
    ask.set_defaults(func=create_request)

    wait = subparsers.add_parser("wait", help="wait for a response to an existing request")
    wait.add_argument("id")
    wait.add_argument("--timeout", type=float, default=0, help="seconds to wait; 0 means forever")
    wait.add_argument("--interval", type=float, default=1.0, help="wait polling interval")
    wait.set_defaults(func=wait_for_response)

    response = subparsers.add_parser("respond", help="write a response file for a request")
    response.add_argument("id")
    response.add_argument("message")
    response.add_argument("--from", dest="sender", default=os.environ.get("USER", "operator"))
    response.set_defaults(func=respond)

    pending = subparsers.add_parser("pending", help="list requests without responses")
    pending.set_defaults(func=list_requests)

    show = subparsers.add_parser("show", help="show request and response JSON")
    show.add_argument("id")
    show.set_defaults(func=show_request)

    server = subparsers.add_parser("webhook-server", help="receive WhatsApp webhook responses")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8787)
    server.set_defaults(func=run_webhook_server)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
