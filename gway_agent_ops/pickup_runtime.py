"""Guarded native scheduler pickup, initially restricted to a pinned canary.

No label writer, new credential, daemon, or execution transport. A candidate
is only admitted through the existing nonblocking submission gate. Cross-node
reassignment remains fail-closed; general activation awaits fleet acceptance.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from . import pickup_store as store
from .dispatch import ISSUE
from .github_issues import resolve
from .pickup_activation import load
from .pickup_budget import read_live_budgets
from .pickup_evidence import candidates, label_events
from .pickup_policy import NODE, _instant, assignment, budget_decision
from .scheduler import ledger_path
from .task_ledger import TaskLedger
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
        records.extend(json.loads(line) for line in accepted.read_text(encoding="utf-8").splitlines())
    if requests.exists():
        for entry in requests.glob("*.json"):
            if entry.is_symlink():
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
    with TaskLedger(ledger_path(path)) as ledger:
        db = ledger.db
        task = ledger.get(ref)
        if task and task["state"] in ("approved", "vetoed", "running"):
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
    return None


def tick(*, path=None, config_reader=load, candidate_reader=candidates,
         resolver=resolve, events_reader=label_events, budget_reader=read_live_budgets,
         submitter=submit, observer=observe, workspace_check=_workspace,
         maintenance_check=_maintenance, overlap_check=_overlap, clock=_now):
    """One existing scheduler tick, at most one explicitly pinned assignment."""
    config = config_reader()
    if config is None:
        return {"mode": "disabled", "processed": 0}
    ref, node, generation = config["issue"], config["node"], config["generation"]
    rows = store.snapshot(path=path)
    for row in rows:
        if row["state"] not in ("claimed","requested","uncertain"):
            continue
        try:
            evidence = observer(row["issue"], row["dispatch_at"])
            if (evidence.get("state") in store.TERMINAL
                    and evidence.get("source") == SOURCE
                    and evidence.get("trigger") == TRIGGER):
                store.reconcile(row["issue"], row["node"], row["generation"],
                                row["dispatch_at"], evidence, path=path, now=clock())
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            pass
    row = next((r for r in store.snapshot(path=path)
                if r["issue"].casefold() == ref.casefold() and r["generation"] == generation), None)
    if row and row["state"] != "deferred":
        return {"mode":"canary","processed":0,"state":row["state"],"issue":ref}
    if ref.casefold() not in {r.casefold() for r in candidate_reader(node)}:
        return {"mode":"canary","processed":0,"state":"not_discovered_or_held","issue":ref}
    reason, assigned = _identity(config, resolver(ref), events_reader(ref))
    if reason != "eligible":
        return {"mode":"canary","processed":0,"state":reason,"issue":ref}
    budget = budget_reader()
    decision = budget_decision(budget, now=clock())
    if decision != "eligible":
        store.defer(ref,node,generation,decision,path=path,now=clock())
        return {"mode":"canary","processed":0,"state":decision,"issue":ref,
                "budget":budget}
    for check, reason in ((maintenance_check, "maintenance"),):
        if check():
            store.defer(ref,node,generation,reason,path=path,now=clock())
            return {"mode":"canary","processed":0,"state":reason,"issue":ref}
    if not workspace_check(ref):
        store.defer(ref,node,generation,"workspace_unavailable",path=path,now=clock())
        return {"mode":"canary","processed":0,"state":"workspace_unavailable","issue":ref}
    claim = store.claim(ref,node,generation,path=path,now=clock())
    if claim is None:
        return {"mode":"canary","processed":0,"state":"claim_unavailable","issue":ref}
    at = claim["dispatch_at"]

    def preflight(fresh, root):
        if config_reader() != config:
            return "assignment_changed"
        current, latest = _identity(config, fresh, events_reader(ref))
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
    return {"mode":"canary","processed":int(row["state"] == "requested"),
            "state":row["state"],"issue":ref,"dispatch_at":at,"reason":row["reason"]}
