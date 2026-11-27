"""Fresh Codex quota reads over the installed app-server's local stdio RPC.

No inference, login, thread creation, token extraction or raw RPC logging.
Use the same protected runtime context as AgentOps execution. Failure is
unavailable quota, never an estimate of replenishment.
"""
from datetime import datetime, timezone
import json
import os
import select
import subprocess
import time

from .contracts import budget_window, unavailable_budget
from .runtime import resolve_codex_binary, runtime_context

MAX_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 15


def _unavailable():
    return {name: unavailable_budget() for name in ("five_hour", "weekly")}


def parse_limits(result, *, observed):
    """Normalize only the two established quota windows; discard all else."""
    values = _unavailable()
    if not isinstance(result, dict) or not isinstance(result.get("rateLimits"), dict):
        return values
    limits = result["rateLimits"]
    for key, name, minutes in (("primary", "five_hour", 300),
                               ("secondary", "weekly", 10080)):
        try:
            raw = limits[key]
            used = raw["usedPercent"]
            reset = raw["resetsAt"]
            if (type(used) not in (int, float) or not 0 <= used <= 100
                    or raw["windowDurationMins"] != minutes
                    or type(reset) not in (int, float)):
                continue
            values[name] = budget_window(
                remaining_percent=100 - used,
                resets_at=datetime.fromtimestamp(reset, timezone.utc).isoformat(),
                source="codex_app_server_rate_limits",
                observed_at=observed.astimezone(timezone.utc).isoformat())
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
            continue
    return values


def read_live_budgets():
    """Bounded RPC over a private child process, with sanitized results only."""
    process = None
    try:
        context = runtime_context()
        process = subprocess.Popen(
            [resolve_codex_binary(), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=context["environment"], start_new_session=True)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        pending = b""
        total = 0

        def send(message):
            process.stdin.write(json.dumps(message).encode() + b"\n")
            process.stdin.flush()

        def receive(id):
            nonlocal pending, total
            while time.monotonic() < deadline:
                if b"\n" not in pending:
                    if not select.select([process.stdout], [], [],
                                         max(0, deadline - time.monotonic()))[0]:
                        break
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        break
                    pending += chunk
                    continue
                line, pending = pending.split(b"\n", 1)
                message = json.loads(line)
                if isinstance(message, dict) and message.get("id") == id:
                    if "error" in message or not isinstance(message.get("result"), dict):
                        raise ValueError("rpc_unavailable")
                    return message["result"]
            raise ValueError("rpc_unavailable")

        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "gway_agent_ops",
                                        "title": "GWay AgentOps", "version": "0.1"}}})
        receive(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        return parse_limits(receive(2), observed=datetime.now(timezone.utc))
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
        return _unavailable()
    finally:
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            except OSError:
                pass
