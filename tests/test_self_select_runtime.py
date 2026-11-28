from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from gway_agent_ops import self_select_runtime as runtime, self_select_store as store

NOW = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
CANDIDATE = {"repository": "arthexis/example", "number": 9, "state": "open", "kind": "pull_request",
             "draft": False, "updated_at": "2026-11-27T08:00:00Z", "labels": []}
FRESH = {**CANDIDATE, "head_sha": "a" * 40, "title": "Example", "body": "", "comments": 0,
         "url": "https://github.com/arthexis/example/pull/9"}


def test_budget_checks_time_after_live_observation():
    def reader():
        observed = datetime.now(timezone.utc)
        return {name: {"available": True, "source": "test", "remaining_percent": 90,
                "observed_at": observed.isoformat(), "resets_at": (observed + timedelta(hours=hours)).isoformat()}
                for name, hours in (("five_hour", 4), ("weekly", 24))}
    calibration = {"normal_weekly_per_hour": 0.5, "task_weekly_percent": 5,
        "task_five_hour_percent": 20, "task_minutes": 90, "weekly_reserve": 10, "five_hour_reserve": 20}
    assert runtime.budget(reader=reader, profile_reader=lambda: calibration)["spare"] is True


def test_preview_is_read_only_and_respects_budget(tmp_path):
    path = tmp_path / "private" / "db"
    store.create(path=path, now=NOW)
    before = store.inspect(path=path)
    result = runtime.preview(path=path, now=NOW, budget_reader=lambda: {"spare": True},
        discoverer=lambda **k: [CANDIDATE], resolver=lambda *a, **k: FRESH)
    assert result["reason"] == "candidate_available"
    assert result["candidate"]["kind"] == "pull_request"
    assert store.inspect(path=path) == before
    no_budget = runtime.preview(path=path, now=NOW, budget_reader=lambda: {"spare": False, "reason": "budget_unknown"},
        discoverer=lambda **k: (_ for _ in ()).throw(AssertionError("discovered")))
    assert no_budget["reason"] == "budget_unknown"


def test_choose_skips_explicitly_tracked_work(tmp_path):
    from gway_agent_ops.task_ledger import TaskLedger
    path = tmp_path / "private" / "db"
    store.create(path=path, now=NOW)
    with TaskLedger(path) as ledger:
        ledger.db.execute("INSERT INTO tasks(issue,state,created_at,updated_at) VALUES (?,?,?,?)",
                          ("arthexis/example#9", "approved", "2026-11-27", "2026-11-27"))
    second = {**CANDIDATE, "number": 10}
    chosen = runtime.choose([CANDIDATE, second], now=NOW, path=path,
                            resolver=lambda ref, **k: {**FRESH, "number": int(ref.split("#")[-1])})
    assert chosen["number"] == 10


def claimed(path):
    store.create(path=path, now=NOW)
    store.transition("resume", path=path, now=NOW)
    return store.claim(path=path, now=NOW + timedelta(hours=4))


def test_no_budget_finishes_without_discovery_or_dispatch(tmp_path):
    path = tmp_path / "private" / "db"
    c = claimed(path)
    with patch("gway_agent_ops.pickup_runtime._maintenance", return_value=False), \
         patch("gway_agent_ops.admission_status.status", return_value={"available": True}):
        result = runtime.run(c["due_at"], path=path, now=NOW + timedelta(hours=4),
            budget_reader=lambda **k: {"spare": False, "reason": "weekly_surplus_insufficient"},
            discoverer=lambda **k: (_ for _ in ()).throw(AssertionError("discovered")))
    assert result["reason"] == "weekly_surplus_insufficient"
    assert store.inspect(path=path)["claims"][0]["state"] == "finished"


def test_budgeted_pr_is_bound_before_exact_single_submission(tmp_path):
    path = tmp_path / "private" / "db"
    c = claimed(path)
    seen = []
    def submit(ref, at, **kwargs):
        assert store.authorized(ref, at, path=path)
        assert kwargs["pickup_preflight"](FRESH, tmp_path) is None
        seen.append((ref, at, kwargs["source"]))
        return {"state": "requested"}
    with patch("gway_agent_ops.pickup_runtime._maintenance", return_value=False), \
         patch("gway_agent_ops.admission_status.status", return_value={"available": True}):
        result = runtime.run(c["due_at"], path=path, now=NOW + timedelta(hours=4),
            budget_reader=lambda **k: {"spare": True}, discoverer=lambda **k: [CANDIDATE],
            resolver=lambda *a, **k: FRESH, submitter=submit)
    assert result["state"] == "requested"
    assert len(seen) == 1 and seen[0][0] == "arthexis/example#9"
    row = store.inspect(path=path)["claims"][0]
    assert row["state"] == "claimed" and row["target"] == seen[0][0]


def test_busy_retry_reuses_exact_identity_and_unknown_launch_never_retries(tmp_path):
    path = tmp_path / "private" / "db"
    c = claimed(path)
    at = store.stamp(NOW + timedelta(hours=4))
    store.bind(c["due_at"], "arthexis/example#9", at, path=path)
    launched = []
    store.note(c["due_at"], {"state": "launch_unconfirmed"}, path=path)
    runtime.tick(path=path, now=NOW + timedelta(hours=4, minutes=1), hostname=lambda: "gway-001",
                 launcher=lambda claim: launched.append(claim), observer=lambda *a: {"state": "unknown"})
    assert launched == []
    store.note(c["due_at"], {"state": "busy"}, path=path)
    runtime.tick(path=path, now=NOW + timedelta(hours=4, minutes=2), hostname=lambda: "gway-001",
                 launcher=lambda claim: launched.append(claim), observer=lambda *a: {"state": "unknown"})
    assert len(launched) == 1 and launched[0]["dispatch_at"] == at


def test_tick_claims_once_and_waits_for_delivered_terminal_result(tmp_path):
    path = tmp_path / "private" / "db"
    store.create(path=path, now=NOW)
    store.transition("resume", path=path, now=NOW)
    launches = []
    due = NOW + timedelta(hours=4)
    claim = runtime.tick(path=path, now=due, hostname=lambda: "gway-001", launcher=launches.append)
    assert len(launches) == 1 and claim["due_at"] == store.stamp(due)
    runtime.tick(path=path, now=due + timedelta(minutes=1), hostname=lambda: "gway-001", launcher=launches.append)
    assert len(launches) == 1
    at = store.stamp(due)
    store.bind(claim["due_at"], "arthexis/example#9", at, path=path)
    runtime.tick(path=path, now=due + timedelta(minutes=2), hostname=lambda: "gway-001",
                 observer=lambda *a: {"state": "finished", "delivery_confirmed": False,
                                      "source": "self-select", "trigger": "four-hour-budget"})
    assert store.inspect(path=path)["claims"][0]["state"] == "claimed"
    runtime.tick(path=path, now=due + timedelta(minutes=3), hostname=lambda: "gway-001",
                 observer=lambda *a: {"state": "finished", "delivery_confirmed": True,
                                      "source": "self-select", "trigger": "four-hour-budget"})
    assert store.inspect(path=path)["claims"][0]["state"] == "finished"


def test_stopped_unbound_worker_can_be_settled_but_bound_unknown_cannot(tmp_path):
    path = tmp_path / "private" / "db"
    c = claimed(path)
    runtime.tick(path=path, now=NOW + timedelta(hours=4, minutes=36), hostname=lambda: "gway-001",
                 stopped_probe=lambda _: True)
    assert store.inspect(path=path)["claims"][0]["state"] == "failed"


def test_changed_pr_head_is_rejected_inside_submission_gate(tmp_path):
    path = tmp_path / "private" / "db"
    c = claimed(path)
    def submit(ref, at, **kwargs):
        assert kwargs["pickup_preflight"]({**FRESH, "head_sha": "b" * 40}, tmp_path) == "candidate_changed"
        return {"state": "candidate_changed"}
    with patch("gway_agent_ops.pickup_runtime._maintenance", return_value=False), \
         patch("gway_agent_ops.admission_status.status", return_value={"available": True}):
        result = runtime.run(c["due_at"], path=path, now=NOW + timedelta(hours=4),
            budget_reader=lambda **k: {"spare": True}, discoverer=lambda **k: [CANDIDATE],
            resolver=lambda *a, **k: FRESH, submitter=submit)
    assert result["state"] == "blocked"
    assert store.inspect(path=path)["claims"][0]["state"] == "claimed"


def test_claim_to_real_submission_and_local_terminal_receipt(tmp_path):
    import json
    from unittest.mock import Mock
    from gway_agent_ops.nonblocking import submit
    from gway_agent_ops.observation import observe
    from gway_agent_ops.reporting import enqueue
    path = tmp_path / "private" / "db"
    dispatch = tmp_path / "dispatch"
    c = claimed(path)
    runner = Mock(return_value=Mock(returncode=0))
    def submitter(ref, at, **kwargs):
        return submit(ref, at, state_dir=dispatch, runner=runner, **kwargs)
    with patch.dict("os.environ", {"GWAY_AGENT_LEDGER_PATH": str(path)}), \
         patch("gway_agent_ops.pickup_runtime._maintenance", return_value=False), \
         patch("gway_agent_ops.admission_status.status", return_value={"available": True}), \
         patch("gway_agent_ops.self_select_target.resolve", return_value=FRESH), \
         patch("gway_agent_ops.nonblocking._launch_prerequisites", return_value="/usr/bin/agent"), \
         patch("gway_agent_ops.coordination.reserve_submission", return_value=None):
        outcome = runtime.run(c["due_at"], path=path, now=NOW + timedelta(hours=4),
            budget_reader=lambda **k: {"spare": True}, discoverer=lambda **k: [CANDIDATE],
            resolver=lambda *a, **k: FRESH, submitter=submitter)
    assert outcome["state"] == "requested"
    assert observe(outcome["target"], outcome["dispatch_at"], state_dir=dispatch)["source"] == "self-select"
    (dispatch / "accepted.jsonl").write_text(json.dumps({"issue": outcome["target"], "at": outcome["dispatch_at"]}) + "\n")
    with patch("gway_agent_ops.activity_feed.publish"), patch("gway_agent_ops.lcd_notification.enabled", return_value=False):
        enqueue(outcome["target"], outcome["dispatch_at"], state_dir=dispatch,
                status="finished", exit_code=0, local_only=True, findings_state="missing_or_invalid")
    runtime.tick(path=path, now=NOW + timedelta(hours=4, minutes=1), hostname=lambda: "gway-001",
                 observer=lambda ref, at: observe(ref, at, state_dir=dispatch))
    assert store.inspect(path=path)["claims"][0]["state"] == "finished"
