"""Guarded native scheduler pickup with a pinned canary and explicit enable gate.

No label writer, new credential, daemon, or execution transport. A candidate
is only admitted through the existing nonblocking submission gate. Cross-node
reassignment remains fail-closed; general activation awaits fleet acceptance.
"""
from datetime import datetime, timezone
import json
import os
import sqlite3
from pathlib import Path

from . import pickup_store as store
from .dispatch import ISSUE
from .github_issues import resolve
from .pickup_activation import load
from .pickup_budget import read_live_budgets
from .pickup_evidence import candidates, label_events
from .pickup_policy import NODE, _instant, assignment, budget_decision
from .scheduler import ledger_path
from .nonblocking import submit
from .observation import observe

SOURCE = "automatic-pickup"
TRIGGER = "node-label"


def _now():
    return datetime.now(timezone.utc)


def _identity(config, data, events):
    reason, generation = assignment(data, config["node"], events)
    if reason != "eligible":
        return reason, None
    if generation != config["generation"]:
        return "assignment_changed", None
    chosen = next(e for e in events if e.get("id") == int(generation))
    assigned = _instant(chosen["created_at"])
    if assigned <= _instant(config["cutover"]):
        return "pre_cutover_assignment", None
    # Any evidence of assignment to another node requires independent
    # reconciliation; no node-local journal can establish remote completion.
    for event in events:
        if event.get("event") not in ("labeled", "unlabeled"):
            continue
        label = event["label"]["name"]
        if NODE.fullmatch(label.casefold()) and label.casefold() != config["node"]:
            return "cross_node_reconciliation_required", None
    return "eligible", assigned


def _workspace(ref):
    from .workspace import _directory, _match, _validate
    root = Path(os.environ.get("GWAY_AGENT_WORKSPACE_ROOT", ""))
    if not root.is_absolute():
        return False
    repository = ref.split("#", 1)[0]
    owner, name = repository.split("/")
    try:
        _directory(root)
        parent = _match(root, owner)
        checkout = _match(parent, name) if parent else None
        if checkout is None:
            return False
        _validate(checkout, repository)
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _maintenance():
    from .daily_health_audit import maintenance_state
    home = Path.home()
    return any(maintenance_state(p) == "held" for p in (
        home / ".local/state/maintenance.lock",
        home / ".local/share/gway-agent-ops/.deploy.lock"))


def _overlap(ref, assigned, root, path):
    """Runs under the existing submission lock; local history is authoritative."""
    records = []
    accepted = root / "accepted.jsonl"
    requests = root / "requests"
    if accepted.is_symlink() or requests.is_symlink():
        raise ValueError("unsafe_dispatch_history")
    if accepted.exists():
        if accepted.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("pickup_history_limit")
        records.extend(json.loads(line) for line in accepted.read_text(encoding="utf-8").splitlines())
    if requests.exists():
        for i, entry in enumerate(requests.glob("*.json")):
            if i >= 1000:
                raise ValueError("pickup_history_limit")
            if entry.is_symlink() or entry.stat().st_size > 16384:
                raise ValueError("unsafe_dispatch_history")
            records.append(json.loads(entry.read_text(encoding="utf-8")))
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("issue"), str)
                or not ISSUE.fullmatch(record["issue"])):
            raise ValueError("invalid_dispatch_history")
        if record["issue"].casefold() == ref.casefold() and _instant(record["at"]) >= assigned:
            return "already_handled"
    # Preserve explicitly approved work. Conservative exclusion of any active
    # recurring policy in this repository avoids selector/claim races.
    from urllib.parse import quote
    ledger = ledger_path(path)
    # Claim already initialized the private ledger. Read-only connection avoids
    # inversion with the scheduled path (ledger write transaction -> submit lock).
    store.snapshot(path=path)
    db = sqlite3.connect("file:" + quote(str(ledger.absolute()), safe="/") + "?mode=ro",
                         uri=True, timeout=5)
    try:
        task = db.execute("SELECT state FROM tasks WHERE issue=? COLLATE NOCASE", (ref,)).fetchone()
        if task and task[0] in ("approved", "vetoed", "running"):
            return "explicit_approval"
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "schedules" in tables and db.execute(
            """SELECT 1 FROM schedules WHERE issue=? COLLATE NOCASE
               AND state NOT IN ('cancelled','finished','failed','interrupted','blocked') LIMIT 1""",
            (ref,)).fetchone():
            return "explicit_schedule"
        if "recurring_definitions" in tables and db.execute(
            """SELECT 1 FROM recurring_definitions WHERE state='active'
               AND repository=? COLLATE NOCASE AND (issue IS NULL OR issue=? COLLATE NOCASE) LIMIT 1""",
            (ref.split("#",1)[0],ref)).fetchone():
            return "explicit_schedule"
    finally:
        db.close()
    return None


def tick(*, path=None, config_reader=load, candidate_reader=candidates,
         resolver=resolve, events_reader=label_events, budget_reader=read_live_budgets,
         submitter=submit, observer=observe, workspace_check=_workspace,
         maintenance_check=_maintenance, overlap_check=_overlap, clock=_now):
    """One existing scheduler tick, at most one discovered assignment."""
    config = config_reader()
    if config is None:
        return {"mode": "disabled", "processed": 0}
    mode, node = config["mode"], config["node"]
    rows = store.snapshot(path=path)
    pending = [r for r in rows if r["state"] in ("claimed","requested","uncertain")]
    for row in pending[:20]:
        try:
            evidence = observer(row["issue"], row["dispatch_at"])
            if (evidence.get("state") in store.TERMINAL
                    and evidence.get("source") == SOURCE
                    and evidence.get("trigger") == TRIGGER):
                store.reconcile(row["issue"], row["node"], row["generation"],
                                row["dispatch_at"], evidence, path=path, now=clock())
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error):
            pass
    rows = store.snapshot(path=path)
    if mode == "enabled":
        canary = next((r for r in rows if r["node"] == node
            and r["issue"].casefold() == config["canary_issue"].casefold()
            and r["generation"] == config["canary_generation"]), None)
        if not canary or canary["state"] != "terminal" or canary["reason"] != "finished":
            return {"mode":mode,"processed":0,"state":"canary_not_accepted"}
        proof = observer(canary["issue"],canary["dispatch_at"])
        if (proof.get("issue") != canary["issue"] or proof.get("at") != canary["dispatch_at"]
                or proof.get("source") != SOURCE or proof.get("trigger") != TRIGGER
                or proof.get("state") != "finished" or proof.get("exit_code") != 0
                or proof.get("delivery_confirmed") is not True):
            return {"mode":mode,"processed":0,"state":"canary_not_accepted"}
        from .coordination import general_pickup_ready
        if not general_pickup_ready():
            return {"mode":mode,"processed":0,"state":"coordination_not_accepted"}
    refs = candidate_reader(node)
    if mode == "canary":
        ref, generation = config["issue"], config["generation"]
        # Reconcile and show a terminal canary even if it is no longer labeled.
        row = next((r for r in rows if r["issue"].casefold() == ref.casefold()
                    and r["generation"] == generation), None)
        if row and row["state"] != "deferred":
            return {"mode":mode,"processed":0,"state":row["state"],"issue":ref}
        if ref.casefold() not in {r.casefold() for r in refs}:
            return {"mode":mode,"processed":0,"state":"not_discovered_or_held","issue":ref}
    else:
        ref = store.next_candidate(node,refs,path=path)
        if ref is None:
            return {"mode":mode,"processed":0,"state":"no_candidates"}
        generation = None
    issue, events = resolver(ref), events_reader(ref)
    if generation is None:
        reason, generation = assignment(issue,node,events)
        if reason != "eligible":
            return {"mode":mode,"processed":0,"state":reason,"issue":ref}
    pinned = {**config,"issue":ref,"generation":generation}
    reason, assigned = _identity(pinned, issue, events)
    if reason != "eligible":
        return {"mode":mode,"processed":0,"state":reason,"issue":ref}
    row = next((r for r in rows if r["issue"].casefold() == ref.casefold()
                and r["generation"] == generation), None)
    if row and row["state"] != "deferred":
        return {"mode":mode,"processed":0,"state":row["state"],"issue":ref}
    budget = budget_reader()
    decision = budget_decision(budget, now=clock())
    if decision != "eligible":
        store.defer(ref,node,generation,decision,path=path,now=clock())
        return {"mode":mode,"processed":0,"state":decision,"issue":ref,
                "budget":budget}
    for check, reason in ((maintenance_check, "maintenance"),):
        if check():
            store.defer(ref,node,generation,reason,path=path,now=clock())
            return {"mode":mode,"processed":0,"state":reason,"issue":ref}
    if not workspace_check(ref):
        store.defer(ref,node,generation,"workspace_unavailable",path=path,now=clock())
        return {"mode":mode,"processed":0,"state":"workspace_unavailable","issue":ref}
    claim = store.claim(ref,node,generation,path=path,now=clock())
    if claim is None:
        return {"mode":mode,"processed":0,"state":"claim_unavailable","issue":ref}
    at = claim["dispatch_at"]

    def preflight(fresh, root):
        if config_reader() != config:
            return "assignment_changed"
        # Fetch events and then refresh current labels again under the local
        # submission gate. GitHub itself has no atomic external claim API.
        latest_events = events_reader(ref)
        current, latest = _identity(pinned, resolver(ref), latest_events)
        if current != "eligible" or latest != assigned:
            return "on_hold" if current == "on_hold" else "assignment_changed"
        if budget_decision(budget,now=clock()) != "eligible":
            return "budget_deferred"
        if maintenance_check():
            return "maintenance"
        if not workspace_check(ref):
            return "workspace_unavailable"
        return overlap_check(ref,assigned,root,path)

    outcome = "launch_unconfirmed"
    try:
        result = submitter(ref,at,source=SOURCE,trigger=TRIGGER,pickup_preflight=preflight)
        outcome = result.get("state","launch_unconfirmed")
        if outcome == "already_requested":
            evidence = observer(ref,at)
            outcome = "requested" if (evidence.get("source")==SOURCE
                and evidence.get("trigger")==TRIGGER) else "launch_unconfirmed"
        elif outcome == "assignment_changed":
            outcome = "already_handled"
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        pass
    row = store.record_submission(ref,node,generation,at,outcome,path=path,now=clock())
    return {"mode":mode,"processed":int(row["state"] == "requested"),
            "state":row["state"],"issue":ref,"dispatch_at":at,"reason":row["reason"]}
