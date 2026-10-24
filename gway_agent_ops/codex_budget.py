"""Read observed Codex quota snapshots without invoking Codex or guessing values."""
import json
from datetime import datetime, timezone
from pathlib import Path

from .contracts import budget_window, unavailable_budget


def read_budgets(root=None):
    """Return independently available windows from the latest session rate-limit event."""
    root = Path(root) if root is not None else Path.home() / ".codex" / "sessions"
    result = {"five_hour": unavailable_budget(), "weekly": unavailable_budget()}
    if not root.is_dir():
        return result
    # Only recent session files; bounded scanning keeps status cheap.
    try:
        files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
    except OSError:
        return result
    latest = {}
    for path in files:
        try:
            if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                continue
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if event.get("type") != "event_msg":
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict) or payload.get("type") != "token_count":
                        continue
                    info = payload.get("info")
                    limits = info.get("rate_limits") if isinstance(info, dict) else None
                    if not isinstance(limits, dict):
                        continue
                    timestamp = event.get("timestamp")
                    try:
                        observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                        if observed.tzinfo is None:
                            continue
                    except (AttributeError, ValueError):
                        continue
                    for key, name in (("primary", "five_hour"), ("secondary", "weekly")):
                        window = limits.get(key)
                        if not isinstance(window, dict):
                            continue
                        used = window.get("used_percent")
                        minutes = window.get("window_minutes")
                        if (type(used) not in (int, float) or not 0 <= used <= 100
                                or type(minutes) not in (int, float)
                                or (key == "primary" and minutes != 300)
                                or (key == "secondary" and minutes != 10080)):
                            continue
                        reset = window.get("resets_at")
                        try:
                            reset_at = datetime.fromtimestamp(reset, timezone.utc).isoformat().replace("+00:00", "Z") if type(reset) in (int, float) else None
                        except (OverflowError, ValueError, OSError):
                            reset_at = None
                        if name not in latest or observed > latest[name][0]:
                            latest[name] = (observed, 100 - used, reset_at)
        except (OSError, UnicodeError):
            continue
    now = datetime.now(timezone.utc)
    for name, (observed, remaining, reset) in latest.items():
        # Do not present old session observations as current quota.
        if 0 <= (now - observed).total_seconds() <= 3600:
            result[name] = budget_window(
                remaining_percent=remaining, resets_at=reset,
                source="codex_session_rate_limits",
                observed_at=observed.isoformat().replace("+00:00", "Z"))
    return result
