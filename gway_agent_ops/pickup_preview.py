"""Read-only automatic pickup preflight; never claims, submits, or enables."""
from datetime import datetime, timezone
import socket

from .github_issues import resolve, IssueRejected
from .pickup_budget import read_live_budgets
from .pickup_evidence import candidates, label_events
from .pickup_policy import NODE, assignment, budget_decision


def preview(*, node_reader=socket.gethostname, candidate_reader=candidates,
            resolver=resolve, events_reader=label_events,
            budget_reader=read_live_budgets, now=None):
    """Inspect current node candidates and fresh quota; no dispatch authority."""
    node = node_reader()
    if not isinstance(node, str) or not NODE.fullmatch(node):
        raise ValueError("invalid_node")
    refs = candidate_reader(node)
    rows = []
    for ref in refs:
        try:
            current = resolver(ref)
            reason, generation = assignment(current, node, events_reader(ref))
        except (IssueRejected, OSError, ValueError, RuntimeError, KeyError, TypeError):
            reason, generation = "verification_unavailable", None
        rows.append({"issue": ref, "assignment": reason, "generation": generation})
    budget = budget_reader() if any(row["assignment"] == "eligible" for row in rows) else None
    decision = budget_decision(budget, now=now or datetime.now(timezone.utc))
    return {"node": node, "mode": "preview_only", "budget": budget,
            "budget_decision": decision, "candidates": rows}
