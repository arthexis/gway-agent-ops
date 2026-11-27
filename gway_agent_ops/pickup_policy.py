"""Pure, fail-closed policy primitives for automatic issue pickup (#126).

This module is intentionally independent of manual dispatch and performs no I/O.
Its callers must supply fresh, complete, authenticated GitHub event evidence and
authoritative node identity/budget observations; search results alone are not
authorization. It is not a scheduler or a dispatch entry point.
"""
from datetime import datetime, timedelta, timezone
import math
import re

NODE = re.compile(r"gway-[0-9]{3}", re.ASCII)
HOLD = "on-hold"
OWNER = "arthexis"
OWNER_ID = 780437


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("invalid_timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("invalid_timestamp")
    return result.astimezone(timezone.utc)


def assignment(issue, node, events, *, approved_actors=(OWNER,)):
    """Return (reason, generation) for a verified issue and complete event list.

    Generation is the immutable GitHub event ID of the current assignment.
    On-hold removal does not generate a new assignment. Never infer permission
    from the current label alone; the latest matching label event must be an
    authorized labeling event. All malformed or ambiguous evidence fails closed.
    """
    if not isinstance(node, str) or not NODE.fullmatch(node):
        return "invalid_node", None
    if (not isinstance(issue, dict) or issue.get("state") != "open"
            or issue.get("creator") != OWNER or "pull_request" in issue):
        return "untrusted_issue", None
    labels = issue.get("labels")
    if not isinstance(labels, list) or any(not isinstance(x, str) for x in labels):
        return "invalid_labels", None
    if any(x.casefold() == HOLD for x in labels):
        return "on_hold", None
    targets = [x for x in labels if NODE.fullmatch(x.casefold())]
    if len(targets) > 1:
        return "conflicting_assignment", None
    if targets != [node]:
        return "not_assigned", None
    if not isinstance(events, list):
        return "unverified_assignment", None
    matches = []
    seen = set()
    try:
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("invalid_event")
            if event.get("event") not in ("labeled", "unlabeled"):
                continue
            label = event["label"]["name"]
            if not isinstance(label, str):
                raise ValueError("invalid_label")
            if label.casefold() != node:
                continue
            event_id = event["id"]
            if type(event_id) is not int or event_id <= 0 or event_id in seen:
                raise ValueError("invalid_event_id")
            seen.add(event_id)
            actor = event["actor"]["login"]
            actor_id = event["actor"].get("id")
            if not isinstance(actor, str) or not actor or type(actor_id) is not int or actor_id <= 0:
                raise ValueError("invalid_actor")
            matches.append((_instant(event["created_at"]), event_id, event["event"], actor, label, actor_id))
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return "unverified_assignment", None
    if not matches:
        return "unverified_assignment", None
    _, event_id, action, actor, label, actor_id = max(matches)
    if action != "labeled" or label != node:
        return "unverified_assignment", None
    if (actor.casefold() != OWNER or actor_id != OWNER_ID
            or actor.casefold() not in {x.casefold() for x in approved_actors}):
        return "unauthorized_assignment", None
    return "eligible", str(event_id)


def budget_decision(budget, *, now, max_age=timedelta(minutes=5)):
    """Both windows must have fresh, positive authoritative observations.

    A reported reset time is a hint, not evidence of replenishment.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone_required")
    now = now.astimezone(timezone.utc)
    if not isinstance(budget, dict):
        return "budget_unknown"
    exhausted = False
    for window in ("five_hour", "weekly"):
        try:
            item = budget[window]
            if item["available"] is not True or not item.get("source"):
                return "budget_unknown"
            observed = _instant(item["observed_at"])
            percent = item["remaining_percent"]
            if (type(percent) not in (int, float) or not math.isfinite(percent)
                    or not 0 <= percent <= 100):
                return "budget_unknown"
            if observed > now or now - observed > max_age:
                return "budget_stale"
            if _instant(item["resets_at"]) <= now:
                return "budget_stale"
            exhausted = exhausted or percent == 0
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            return "budget_unknown"
    return "budget_exhausted" if exhausted else "eligible"
