from datetime import datetime, timedelta, timezone
from pathlib import Path

from gway_agent_ops import pickup_runtime as runtime, pickup_store as store
from gway_agent_ops.pickup_budget import parse_limits

REF = "arthexis/gway-agent-ops#126"
NOW = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)
CONFIG = {"schema":1, "node":"gway-001","mode":"canary",
          "cutover": (NOW-timedelta(minutes=10)).isoformat(),
          "issue":REF,"generation":"12"}


def setup(tmp_path):
    labels = ["gway-001"]
    events = [{"id":12,"event":"labeled","label":{"name":"gway-001"},
               "actor":{"login":"arthexis"},"created_at":(NOW-timedelta(minutes=1)).isoformat()}]
    def resolve(ref):
        return {"state":"open","creator":"arthexis","labels":labels[:]}
    quota = {"rateLimits": {
        "primary":{"usedPercent":100,"windowDurationMins":300,"resetsAt":NOW.timestamp()+3600},
        "secondary":{"usedPercent":30,"windowDurationMins":10080,"resetsAt":NOW.timestamp()+86400}}}
    calls = []
    def submit(ref,at,**kwargs):
        reason = kwargs["pickup_preflight"](resolve(ref),Path("/unused"))
        if reason is None:
            calls.append(at)
        return {"state": reason or "requested"}
    args = {"path":tmp_path/"private"/"tasks.sqlite3",
            "config_reader":lambda:CONFIG,"candidate_reader":lambda node:[REF],
            "resolver":resolve,"events_reader":lambda ref:events[:],
            "budget_reader":lambda:parse_limits(quota,observed=NOW),
            "submitter":submit,"observer":lambda *args:{"state":"unknown"},
            "workspace_check":lambda ref:True,"maintenance_check":lambda:False,
            "overlap_check":lambda *args:None,"clock":lambda:NOW}
    return args,labels,events,quota,calls


def test_disabled_is_noop(tmp_path):
    args,_,_,_,calls = setup(tmp_path)
    args["config_reader"] = lambda:None
    assert runtime.tick(**args)["mode"] == "disabled"
    assert not calls and not args["path"].exists()


def test_defers_then_dispatches_once_after_reset(tmp_path):
    args,labels,events,quota,calls = setup(tmp_path)
    assert runtime.tick(**args)["state"] == "budget_exhausted"
    row = store.snapshot(path=args["path"])[0]
    assert row["state"] == "deferred" and not calls
    quota["rateLimits"]["primary"]["usedPercent"] = 0
    assert runtime.tick(**args)["state"] == "requested"
    assert calls == [row["dispatch_at"]]
    assert runtime.tick(**args)["state"] == "requested"
    assert len(calls) == 1
    args["observer"] = lambda ref,at:{"issue":ref,"at":at,"state":"finished",
        "source":runtime.SOURCE,"trigger":runtime.TRIGGER,"delivery_confirmed":True}
    assert runtime.tick(**args)["state"] == "terminal"
    assert len(calls) == 1


def test_hold_and_foreign_history_never_submit(tmp_path):
    args,labels,events,quota,calls = setup(tmp_path)
    labels.append("on-hold")
    assert runtime.tick(**args)["state"] == "on_hold"
    labels.pop()
    events.append({"id":9,"event":"unlabeled","label":{"name":"gway-004"}})
    assert runtime.tick(**args)["state"] == "cross_node_reconciliation_required"
    assert not calls


def test_manual_overlap_does_not_dispatch(tmp_path):
    args,labels,events,quota,calls = setup(tmp_path)
    quota["rateLimits"]["primary"]["usedPercent"] = 0
    args["overlap_check"] = lambda *args:"already_handled"
    assert runtime.tick(**args)["state"] == "handled"
    assert not calls


def test_preflight_hold_after_claim_is_definitive(tmp_path):
    args,labels,events,quota,calls = setup(tmp_path)
    quota["rateLimits"]["primary"]["usedPercent"] = 0
    original = args["submitter"]
    def submit(*a,**kw):
        labels.append("on-hold")
        return original(*a,**kw)
    args["submitter"] = submit
    assert runtime.tick(**args)["state"] == "deferred"
    assert not calls
