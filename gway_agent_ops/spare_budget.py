"""Read-only, calibrated estimate of quota likely to expire unused.

Percentages from the two windows have different denominators.  The caller
supplies independently measured estimates; this module never infers a reset.
"""
from datetime import datetime, timedelta, timezone
import math

from .pickup_policy import budget_decision, _instant


def estimate(budget, *, now, normal_weekly_per_hour=None,
             task_weekly_percent=None, task_five_hour_percent=None,
             task_minutes=None, weekly_reserve=10, five_hour_reserve=20):
    result = {"spare": False, "reason": "budget_unknown", "budget": budget}
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone_required")
    now = now.astimezone(timezone.utc)
    status = budget_decision(budget, now=now)
    if status != "eligible":
        result["reason"] = status
        return result
    estimates = (normal_weekly_per_hour, task_weekly_percent,
                 task_five_hour_percent, task_minutes)
    if any(x is None for x in estimates):
        result["reason"] = "calibration_required"
        return result
    numbers = estimates + (weekly_reserve, five_hour_reserve)
    if any(type(x) not in (int, float) or not math.isfinite(x) or x < 0
           for x in numbers):
        raise ValueError("invalid_estimate")
    if (not task_weekly_percent or not task_five_hour_percent or not task_minutes
            or weekly_reserve > 100 or five_hour_reserve > 100):
        raise ValueError("invalid_estimate")
    weekly = budget["weekly"]
    short = budget["five_hour"]
    weekly_reset = _instant(weekly["resets_at"])
    short_reset = _instant(short["resets_at"])
    hours = (weekly_reset - now).total_seconds() / 3600
    short_hours = (short_reset - now).total_seconds() / 3600
    if hours > 168 or short_hours > 5:
        result["reason"] = "invalid_reset_horizon"
        return result
    normal = normal_weekly_per_hour * hours
    surplus = weekly["remaining_percent"] - normal - weekly_reserve
    # Scenario only: later windows start full, last five hours, and allow
    # sequential tasks. Actual reset timing and headroom must be reobserved.
    def slots(minutes, remaining):
        return min(max(0, math.ceil(minutes / task_minutes) - 1),
                   math.floor(max(0, remaining - five_hour_reserve) / task_five_hour_percent))
    current_minutes = max(0, (min(short_reset, weekly_reset) - now).total_seconds() / 60)
    current_slots = slots(current_minutes, short["remaining_percent"])
    future_slots = 0
    start = short_reset
    while start < weekly_reset:
        end = min(start + timedelta(hours=5), weekly_reset)
        future_slots += slots((end - start).total_seconds() / 60, 100)
        start = end
    future_capacity = future_slots * task_weekly_percent
    current_capacity = current_slots * task_weekly_percent
    result.update({"weekly_hours_to_reset": hours,
                   "forecast_normal_weekly_percent": normal,
                   "estimated_expiring_weekly_percent": max(0, surplus),
                   "weekly_task_percent": task_weekly_percent,
                   "five_hour_task_percent": task_five_hour_percent,
                   "weekly_reserve_percent": weekly_reserve,
                   "five_hour_reserve_percent": five_hour_reserve,
                   "task_minutes": task_minutes,
                   "scenario_current_task_slots": current_slots,
                   "scenario_future_task_slots": future_slots,
                   "scenario_current_weekly_capacity_percent": current_capacity,
                   "scenario_future_weekly_capacity_percent": future_capacity,
                   "scenario_weekly_surplus_at_risk_if_deferred_percent": max(0, surplus - future_capacity),
                   "scenario_assumption": "future five-hour windows refill to 100 percent; recheck after reset",
                   "budget": budget})
    # A task spanning a reset needs a new observation, not assumed replenishment.
    end = now + timedelta(minutes=task_minutes)
    if end >= min(weekly_reset, short_reset):
        result["reason"] = "task_crosses_reset"
    elif short["remaining_percent"] - five_hour_reserve < task_five_hour_percent:
        result["reason"] = "five_hour_reserve"
    elif surplus < task_weekly_percent:
        result["reason"] = "weekly_surplus_insufficient"
    else:
        result.update(spare=True, reason="estimated_surplus")
    return result
