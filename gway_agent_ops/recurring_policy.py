"""Pure recurring schedule policy foundation; no persistence or dispatch.

The existing one-shot scheduler remains authoritative until a later increment
integrates this policy with its private ledger and single-executor admission.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .dispatch import ISSUE

REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
INTERVAL = re.compile(r"^([1-9][0-9]{0,5})(m|h|d)$")
MIN_INTERVAL = timedelta(minutes=15)
MAX_INTERVAL = timedelta(days=30)
MAX_PREFIX = 128
MAX_REGEX = 128


@dataclass(frozen=True)
class DailyCalendarPolicy:
    """Calculate daily wall-clock slots without replaying missed dates.

    Ambiguous wall times use the first occurrence; nonexistent wall times are
    skipped.  The returned instant is always UTC.  This class does not claim
    or dispatch work: durable local-date deduplication belongs in the ledger.
    """

    hour: int
    minute: int
    timezone_name: str

    def __post_init__(self):
        if (type(self.hour) is not int or not 0 <= self.hour <= 23 or
                type(self.minute) is not int or not 0 <= self.minute <= 59):
            raise ValueError("invalid_calendar_time")
        if type(self.timezone_name) is not str or not self.timezone_name:
            raise ValueError("invalid_calendar_timezone")
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("invalid_calendar_timezone") from exc

    def next_due(self, *, now: datetime) -> datetime:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("timezone_required")
        zone = ZoneInfo(self.timezone_name)
        current = now.astimezone(timezone.utc)
        day = current.astimezone(zone).date()
        # A civil-time gap can remove a slot, but cannot remove an unbounded
        # number of consecutive dates. Fail closed if timezone data is unusual.
        for offset in range(370):
            local = datetime.combine(day + timedelta(days=offset),
                                     time(self.hour, self.minute), zone)
            slot = local.astimezone(timezone.utc)
            if slot.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
                continue
            if slot > current:
                return slot
        raise ValueError("calendar_slot_unavailable")


@dataclass(frozen=True)
class RecurringPolicy:
    repository: str
    every: timedelta
    issue: str | None = None
    title_prefix: str | None = None
    title_regex: str | None = None

    @classmethod
    def parse(cls, target: str, every: str, *, title_prefix: str | None = None,
              title_regex: str | None = None):
        if type(target) is not str or len(target) > 200:
            raise ValueError("invalid_recurring_target")
        if type(every) is not str:
            raise ValueError("invalid_recurring_interval")
        match = INTERVAL.fullmatch(every)
        if match is None:
            raise ValueError("invalid_recurring_interval")
        quantity, unit = match.groups()
        duration = timedelta(**{dict(m="minutes", h="hours", d="days")[unit]: int(quantity)})
        if not MIN_INTERVAL <= duration <= MAX_INTERVAL:
            raise ValueError("recurring_interval_out_of_range")
        is_issue = ISSUE.fullmatch(target) is not None
        if is_issue:
            if title_prefix is not None or title_regex is not None:
                raise ValueError("selector_conflict")
            return cls(target.split("#", 1)[0], duration, issue=target)
        if REPOSITORY.fullmatch(target) is None:
            raise ValueError("invalid_recurring_target")
        if (title_prefix is None) == (title_regex is None):
            raise ValueError("selector_required")
        if title_prefix is not None:
            if (type(title_prefix) is not str or not title_prefix
                    or len(title_prefix) > MAX_PREFIX or title_prefix != title_prefix.strip()
                    or any(ord(c) < 32 or ord(c) == 127 for c in title_prefix)):
                raise ValueError("title_prefix_required")
            return cls(target, duration, title_prefix=title_prefix)
        # Limit the regex dialect to bounded, anchored patterns without groups,
        # alternation or backreferences. Match only bounded issue titles.
        if (type(title_regex) is not str or not title_regex.startswith("^")
                or len(title_regex) > MAX_REGEX or len(title_regex) <= 1
                or any(ord(c) < 32 or ord(c) == 127 for c in title_regex)
                or any(c in title_regex for c in "(){}|")
                or title_regex.count("*") + title_regex.count("+") > 2
                or "\\1" in title_regex or "\\2" in title_regex):
            raise ValueError("unsafe_title_regex")
        try:
            re.compile(title_regex)
        except re.error as exc:
            raise ValueError("unsafe_title_regex") from exc
        return cls(target, duration, title_regex=title_regex)

    def matches(self, issue: dict) -> bool:
        """Filter discovery candidates; NOT a trust or execution authorization."""
        if not isinstance(issue, dict) or issue.get("state") != "open" or issue.get("is_pull_request"):
            return False
        ref = issue.get("ref")
        if type(ref) is not str or ISSUE.fullmatch(ref) is None:
            return False
        if ref.split("#", 1)[0] != self.repository:
            return False
        if self.issue is not None:
            return ref == self.issue
        title = issue.get("title")
        if type(title) is not str or len(title) > 256:
            return False
        if self.title_prefix is not None:
            return title.startswith(self.title_prefix)
        return re.match(self.title_regex, title) is not None

    def next_due(self, previous_due: datetime, *, now: datetime) -> datetime:
        """Skip missed slots; never replay a backlog of unattended intervals."""
        if (previous_due.tzinfo is None or previous_due.utcoffset() is None
                or now.tzinfo is None or now.utcoffset() is None):
            raise ValueError("timezone_required")
        due = previous_due.astimezone(timezone.utc)
        current = now.astimezone(timezone.utc)
        if due > current:
            return due
        elapsed = current - due
        slots = elapsed // self.every + 1
        return due + slots * self.every
