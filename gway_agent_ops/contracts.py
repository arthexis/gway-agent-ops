"""Versioned wire shapes for the on-demand agent CLI.

These are presentation contracts, not a scheduler or a source of issue state.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
STATES = frozenset(("open", "sent", "attended"))


def envelope(command: str, status: str, data: Any = None,
             error: dict | None = None) -> dict:
    if not command or not status:
        raise ValueError("command and status are required")
    if error is not None and (not isinstance(error, dict) or not error.get("code")):
        raise ValueError("error requires code")
    return {"schema_version": SCHEMA_VERSION, "command": command,
            "status": status, "data": data, "error": error}


def unavailable_budget() -> dict:
    return {"remaining_percent": None, "resets_at": None,
            "source": None, "observed_at": None, "available": False}


def budget_window(*, remaining_percent: float | None = None,
                  resets_at: str | None = None, source: str | None = None,
                  observed_at: str | None = None) -> dict:
    if remaining_percent is None:
        return unavailable_budget()
    if type(remaining_percent) not in (int, float) or not 0 <= remaining_percent <= 100:
        raise ValueError("remaining percent must be 0..100")
    if not source or not observed_at:
        raise ValueError("available quota requires a source and observation time")
    for value in (resets_at, observed_at):
        if value is not None:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("quota timestamps must include timezone")
    return {"remaining_percent": remaining_percent, "resets_at": resets_at,
            "source": source, "observed_at": observed_at, "available": True}


def status_data(*, active: bool = False, five_hour: dict | None = None,
                weekly: dict | None = None) -> dict:
    if type(active) is not bool:
        raise ValueError("active must be boolean")
    return {"active": active, "budget": {
        "five_hour": unavailable_budget() if five_hour is None else five_hour,
        "weekly": unavailable_budget() if weekly is None else weekly}}


def issue_row(*, repository: str, number: int, title: str, state: str,
              comments: int, sent_at: str | None = None, labels: list[str] | None = None) -> dict:
    if (not repository or "/" not in repository or
            type(number) is not int or number < 1 or
            state not in STATES or type(comments) is not int or comments < 0):
        raise ValueError("invalid issue row")
    if state == "sent" and not sent_at:
        raise ValueError("sent rows require dispatch time")
    if sent_at is not None:
        dt = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("dispatch time must include timezone")
    return {"repository": repository, "number": number, "title": title,
            "url": f"https://github.com/{repository}/issues/{number}",
            "state": state, "comments": comments, "sent_at": sent_at,
            "labels": sorted(set(labels or []))}


def json_line(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
