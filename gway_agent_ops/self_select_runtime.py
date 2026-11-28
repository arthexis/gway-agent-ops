"""Opt-in, bounded scheduler for the owner's self-selecting AgentOps task."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys

from . import self_select_store as store
from .dispatch import timestamp
from .environment import _regular_private_file
from .pickup_budget import read_live_budgets
from .self_select_discovery import discover
from .self_select_policy import select
from .self_select_target import resolve
from .spare_budget import estimate

PROFILE = Path.home() / ".config/gway-agent-ops/spare-budget.json"
FIELDS = frozenset(("normal_weekly_per_hour", "task_weekly_percent", "task_five_hour_percent",
                    "task_minutes", "weekly_reserve", "five_hour_reserve"))


def profile(path=PROFILE):
    path = Path(path)
    _regular_private_file(path)
    if path.stat().st_size > 4096:
        raise ValueError("profile_too_large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("invalid_profile")
    return value


def budget(*, now=None, reader=read_live_budgets, profile_reader=profile):
    return estimate(reader(), now=now or datetime.now(timezone.utc), **profile_reader())


def choose(candidates, *, now, path=None, resolver=resolve):
    """Choose the first current candidate without a conflicting local task."""
    from .self_select_overlap import check as overlap
    from .github_issues import IssueRejected
    remaining = list(candidates)
    while remaining:
        item = select(remaining, now=now)
        if item is None:
            return None
        remaining.remove(item)
        ref = item["repository"] + "#" + str(item["number"])
        if overlap(ref, path=path):
            continue
        try:
            fresh = resolver(ref, now=now)
        except IssueRejected as exc:
            if exc.code == "target_ineligible":
                continue
            raise
        if (fresh["kind"], fresh["updated_at"]) == (item["kind"], item["updated_at"]):
            return fresh
    return None


def preview(*, now=None, budget_reader=budget, discoverer=discover, resolver=resolve, path=None):
    """Read-only selection preview; does not create a claim or start a task."""
    now = now or datetime.now(timezone.utc)
    allowance = budget_reader()
    result = {"mode": "preview_only", "budget": allowance, "candidate": None}
    if allowance.get("spare") is not True:
        result["reason"] = allowance.get("reason", "budget_unknown")
        return result
    candidates = discoverer(now=now)
    fresh = choose(candidates, now=now, path=path, resolver=resolver)
    result["discovered_count"] = len(candidates)
    if fresh is None:
        result["reason"] = "no_candidate"
        return result
    result["candidate"] = {key: fresh[key] for key in ("repository", "number", "kind", "updated_at", "draft", "head_sha", "url")}
    result["reason"] = "candidate_available"
    return result


def run(due_at, *, path=None, now=None, budget_reader=budget, discoverer=discover,
        resolver=resolve, submitter=None):
    """Run one durable claim; ambiguous launches remain claimed for reconciliation."""
    from .nonblocking import submit
    from .pickup_runtime import _maintenance
    from .admission_status import status
    from .self_select_overlap import check as overlap
    submitter = submitter or submit
    now = now or datetime.now(timezone.utc)
    snapshot = store.inspect(path=path)
    claim = next((x for x in snapshot["claims"] if x["due_at"] == due_at and x["state"] == "claimed"), None)
    if not claim or not snapshot["schedule"] or snapshot["schedule"]["state"] != "active":
        raise ValueError("claim_unavailable")
    try:
        # The live reader timestamps its observation after this worker's
        # start. Let it choose a time after the read, not the earlier start.
        allowance = budget_reader()
        if allowance.get("spare") is not True:
            reason = allowance.get("reason", "budget_unknown")
            if claim["target"]:
                # A coordination intent may already exist. Preserve its exact
                # identity and wait for budget instead of orphaning it.
                store.note(due_at, {"state": "busy", "reason": reason}, path=path)
                return {"state": "busy", "reason": reason}
            store.finish(due_at, "finished", {"state": "skipped", "reason": reason}, path=path)
            return {"state": "skipped", "reason": reason}
        if _maintenance() or not status()["available"]:
            if claim["target"]:
                store.note(due_at, {"state": "busy", "reason": "capacity_unavailable"}, path=path)
                return {"state": "busy", "reason": "capacity_unavailable"}
            store.finish(due_at, "finished", {"state": "skipped", "reason": "capacity_unavailable"}, path=path)
            return {"state": "skipped", "reason": "capacity_unavailable"}
        if claim["target"] is None:
            fresh = choose(discoverer(now=now), now=now, path=path, resolver=resolver)
            if fresh is None:
                store.finish(due_at, "finished", {"state": "skipped", "reason": "no_candidate"}, path=path)
                return {"state": "skipped", "reason": "no_candidate"}
            ref = fresh["repository"] + "#" + str(fresh["number"])
            at = timestamp(now.isoformat())
            store.bind(due_at, ref, at, path=path)
        else:
            ref, at = claim["target"], claim["dispatch_at"]
            fresh = resolver(ref, now=now)

        def preflight(current, root):
            if not store.authorized(ref, at, path=path):
                return "self_select_claim_unavailable"
            if (current["kind"], current["updated_at"], current["head_sha"]) != (fresh["kind"], fresh["updated_at"], fresh["head_sha"]):
                return "candidate_changed"
            if _maintenance() or budget_reader().get("spare") is not True:
                return "budget_deferred"
            return overlap(ref, path=path)

        outcome = submitter(ref, at, source="self-select", trigger="four-hour-budget", pickup_preflight=preflight)
        state = outcome.get("state", "launch_unconfirmed")
        if state == "already_requested":
            from .observation import observe
            receipt = observe(ref, at)
            if receipt.get("source") != "self-select" or receipt.get("trigger") != "four-hour-budget":
                store.note(due_at, {"state": "blocked", "reason": "identity_provenance_mismatch", "target": ref}, path=path)
                return {"state": "blocked", "reason": "identity_provenance_mismatch", "target": ref}
        if state in ("requested", "already_requested", "launch_unconfirmed", "busy"):
            # Retain the frozen identity until observed or explicitly reconciled.
            store.note(due_at, {"state": state, "target": ref, "dispatch_at": at}, path=path)
            return {"state": state, "target": ref, "dispatch_at": at}
        # Earlier retries may have left a shared coordination intent even if
        # this attempt did not launch. Keep the bound identity for audit.
        saved = "busy" if state in ("budget_deferred",) else "blocked"
        store.note(due_at, {"state": saved, "reason": state, "target": ref}, path=path)
        return {"state": saved, "reason": state, "target": ref}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
        # If identity is already bound, the operation might have reached the
        # submission gate. Never discard or retry an uncertain invocation.
        current = next((x for x in store.inspect(path=path)["claims"] if x["due_at"] == due_at), None)
        if current and current["target"] is None:
            store.finish(due_at, "failed", {"state": "failed", "reason": "preflight_unavailable"}, path=path)
        raise


def unit_name(due_at):
    return "gway-self-select-" + hashlib.sha256(due_at.encode()).hexdigest()[:24]


def stopped(due_at):
    try:
        response = subprocess.run(["systemctl", "--user", "show", unit_name(due_at) + ".service",
            "--property=LoadState", "--property=ActiveState", "--property=SubState"],
            capture_output=True, text=True, timeout=5, check=False)
        values = dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)
        return (response.returncode == 0 and values.get("LoadState") in ("loaded", "not-found")
                and values.get("ActiveState") in ("inactive", "failed")
                and values.get("SubState") in ("dead", "failed"))
    except (OSError, subprocess.SubprocessError):
        return False


def tick(*, path=None, now=None, hostname=socket.gethostname, launcher=None, observer=None, stopped_probe=stopped):
    """Claim or reconcile once; never execute work in the timer process."""
    from .observation import observe
    from .scheduler import ledger_path
    now = now or datetime.now(timezone.utc)
    if hostname() != "gway-001":
        return None
    snapshot = store.inspect(path=path)
    if not snapshot["schedule"] or snapshot["schedule"]["state"] != "active":
        return None
    def launch(c):
        if launcher is not None:
            return launcher(c)
        unit = unit_name(c["due_at"])
        return subprocess.run(["systemd-run", "--user", "--quiet", "--collect", "--unit=" + unit,
            "--property=RuntimeMaxSec=1800", "--property=Nice=10", sys.executable,
            "-m", "gway_agent_ops.self_select_runtime", c["due_at"], str(ledger_path(path))],
            timeout=15, check=True)

    pending = next((x for x in snapshot["claims"] if x["state"] == "claimed"), None)
    if pending and pending["target"]:
        evidence = (observer or observe)(pending["target"], pending["dispatch_at"])
        if evidence.get("state") in ("finished", "codex_failed", "interrupted", "released", "preacceptance_failed"):
            if evidence.get("source") != "self-select" or evidence.get("trigger") != "four-hour-budget":
                return None
            if (evidence.get("state") != "finished" or evidence.get("delivery_confirmed") is True
                    or (evidence.get("report_scope") == "local" and evidence.get("execution_terminal") is True)):
                store.finish(pending["due_at"], "finished", {"state": evidence["state"], "target": pending["target"],
                    "dispatch_at": pending["dispatch_at"], "exit_code": evidence.get("exit_code"),
                    "findings_state": evidence.get("findings_state"), "report_scope": evidence.get("report_scope")}, path=path)
        elif evidence.get("state") == "unknown":
            # Only a definitive busy result is retryable, with the original
            # identity. Unknown after any other outcome is never retried.
            recorded = json.loads(pending["result"]) if pending["result"] else {}
            if recorded.get("state") == "busy":
                try:
                    launch(pending)
                except (OSError, subprocess.SubprocessError):
                    pass
        return None
    if pending:
        from .pickup_policy import _instant
        # No target was ever bound, so a positively stopped worker cannot
        # have submitted a target. Bound or ambiguous identities stay claimed.
        if now - _instant(pending["claimed_at"]) >= timedelta(minutes=35) and stopped_probe(pending["due_at"]):
            store.finish(pending["due_at"], "failed", {"state": "failed", "reason": "worker_stopped_before_binding"}, path=path)
        return None
    claim = store.claim(path=path, now=now)
    if claim is None:
        return None
    launch(claim)
    return claim


if __name__ == "__main__":
    from .environment import load_runtime_environment
    load_runtime_environment()
    run(sys.argv[1], path=Path(sys.argv[2]))
