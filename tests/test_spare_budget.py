from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
import json
import tempfile
from pathlib import Path

from gway_agent_ops.spare_budget import estimate

NOW = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)


def budget(short=80, weekly=60, short_hours=4, weekly_hours=24):
    def window(percent, hours):
        return {"available": True, "source": "test", "remaining_percent": percent,
                "observed_at": NOW.isoformat(),
                "resets_at": (NOW + timedelta(hours=hours)).isoformat()}
    return {"five_hour": window(short, short_hours), "weekly": window(weekly, weekly_hours)}


CALIBRATION = dict(normal_weekly_per_hour=1, task_weekly_percent=5,
                   task_five_hour_percent=15, task_minutes=30)


class SpareBudgetTests(unittest.TestCase):
    def test_expiring_surplus_uses_separate_window_units(self):
        result = estimate(budget(), now=NOW, **CALIBRATION)
        self.assertTrue(result["spare"])
        self.assertEqual(result["estimated_expiring_weekly_percent"], 26)

    def test_absent_calibration_and_unavailable_quota_fail_closed(self):
        self.assertEqual(estimate(budget(), now=NOW)["reason"], "calibration_required")
        self.assertFalse(estimate({}, now=NOW, **CALIBRATION)["spare"])

    def test_future_reset_scenario_separates_task_costs(self):
        result = estimate(budget(short=80, short_hours=4, weekly_hours=10), now=NOW, **CALIBRATION)
        self.assertEqual(result["scenario_current_task_slots"], 4)
        self.assertEqual(result["scenario_future_task_slots"], 6)
        self.assertEqual(result["scenario_future_weekly_capacity_percent"], 30)
        self.assertEqual(result["scenario_weekly_surplus_at_risk_if_deferred_percent"], 10)

    def test_limited_time_can_make_spending_now_urgent(self):
        result = estimate(budget(short=80, short_hours=2, weekly_hours=3), now=NOW, **CALIBRATION)
        self.assertEqual(result["scenario_future_task_slots"], 1)
        self.assertEqual(result["scenario_weekly_surplus_at_risk_if_deferred_percent"], 42)

    def test_no_slot_for_task_ending_at_reset(self):
        result = estimate(budget(short_hours=.5), now=NOW, **CALIBRATION)
        self.assertEqual(result["scenario_current_task_slots"], 0)
        self.assertEqual(result["reason"], "task_crosses_reset")

    def test_reserves_and_forecast_block(self):
        self.assertEqual(estimate(budget(short=34), now=NOW, **CALIBRATION)["reason"], "five_hour_reserve")
        self.assertEqual(estimate(budget(weekly=38), now=NOW, **CALIBRATION)["reason"], "weekly_surplus_insufficient")

    def test_never_assume_replenishment_for_task_crossing_either_reset(self):
        for values in (dict(short_hours=.25), dict(weekly_hours=.25)):
            self.assertEqual(estimate(budget(**values), now=NOW, **CALIBRATION)["reason"], "task_crosses_reset")

    def test_stale_invalid_and_exhausted_fail_closed(self):
        old = budget()
        old["weekly"]["observed_at"] = (NOW - timedelta(minutes=6)).isoformat()
        self.assertEqual(estimate(old, now=NOW, **CALIBRATION)["reason"], "budget_stale")
        self.assertFalse(estimate(budget(weekly=0), now=NOW, **CALIBRATION)["spare"])
        with self.assertRaises(ValueError):
            estimate(budget(), now=NOW, **{**CALIBRATION, "task_minutes": float("nan")})

    def test_cli_uses_live_reader_and_returns_safe_default(self):
        from gway_agent_ops.cli import main
        from gway_agent_ops.contracts import unavailable_budget
        output = StringIO()
        missing = {name: unavailable_budget() for name in ("five_hour", "weekly")}
        with patch("gway_agent_ops.pickup_budget.read_live_budgets", return_value=missing), \
             patch("gway_agent_ops.cli.load_runtime_environment"), redirect_stdout(output):
            self.assertEqual(main(["spare-budget", "--json"]), 0)
        data = json.loads(output.getvalue())["data"]
        self.assertFalse(data["spare"])
        self.assertEqual(data["reason"], "budget_unknown")

    def test_profile_is_loaded_and_conflicting_flags_rejected(self):
        from gway_agent_ops.cli import main
        profile = {**CALIBRATION, "weekly_reserve": 10, "five_hour_reserve": 20}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "profile.json"
            path.write_text(json.dumps(profile))
            path.chmod(0o600)
            output = StringIO()
            with patch("gway_agent_ops.pickup_budget.read_live_budgets", return_value=budget()), \
                 patch("gway_agent_ops.cli.load_runtime_environment"), \
                 patch("gway_agent_ops.spare_budget.estimate", return_value={"spare": True}) as calc, \
                 redirect_stdout(output):
                self.assertEqual(main(["spare-budget", "--profile", str(path), "--json"]), 0)
                self.assertEqual(calc.call_args.kwargs["task_minutes"], 30)
                self.assertEqual(main(["spare-budget", "--profile", str(path), "--task-minutes", "20", "--json"]), 1)


if __name__ == "__main__":
    unittest.main()
