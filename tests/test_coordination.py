"""No GitHub, systemd or Codex calls: exercise the shared CAS contract."""
import base64
from copy import deepcopy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from unittest.mock import Mock, patch

import pytest

from gway_agent_ops import coordination as c
from gway_agent_ops.dispatch import Busy, admission, timestamp
from gway_agent_ops.nonblocking import submit
from gway_agent_ops.observation import observe
from gway_agent_ops.reporting import enqueue, _key

REF = "arthexis/example#1"
AT = "2026-10-13T15:00:00Z"
CONFIG = {"schema": 1, "epoch": "a"*32, "repository": "arthexis/private-coordinator",
          "members": ["gway-001", "gway-004"], "general_pickup_accepted": False}


class Store:
    def __init__(self, barrier=None):
        self.lock = Lock()
        self.head = 0
        self.data = {"schema": 1, "epoch": CONFIG["epoch"], "members": CONFIG["members"],
                     "claims": {}, "history": []}
        self.barrier = barrier
        self.lose_response = False

    def read(self):
        with self.lock:
            value = self.head, deepcopy(self.data)
        if self.barrier:
            self.barrier.wait(timeout=5)
        return value

    def write(self, parent, data):
        with self.lock:
            if parent != self.head:
                raise c.CoordinationError("coordination_unavailable")
            self.head += 1
            self.data = deepcopy(data)
        if self.lose_response:
            raise c.CoordinationError("coordination_unavailable")


def coordinator(store, node="gway-001"):
    return c.Coordinator(CONFIG, node=node, store=store)


def test_simultaneous_cross_node_claims_cannot_both_win():
    store = Store(Barrier(2))
    def attempt(node):
        try:
            return coordinator(store, node).acquire(REF, AT)
        except c.CoordinationError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, CONFIG["members"]))
    assert sum(x is not None for x in results) == 1
    assert len(store.data["claims"]) == len(store.data["history"]) == 1


def test_repo_casefold_foreign_claim_and_tombstone():
    store = Store()
    one, four = coordinator(store), coordinator(store, "gway-004")
    first = one.acquire(REF, AT)
    assert first
    assert four.acquire("ARTHEXIS/EXAMPLE#2", AT) is None
    with pytest.raises(c.CoordinationError):
        four.verify(REF, AT, first)
    with pytest.raises(c.CoordinationError):
        four.release(REF, AT, first)
    assert four.acquire("arthexis/other#1", AT)
    one.release(REF, AT, first)
    assert four.acquire("ARTHEXIS/EXAMPLE#1", AT) is None
    second = four.acquire("arthexis/example#2", AT)
    assert second
    one.release(REF, AT, first)
    assert four.verify("arthexis/example#2", AT, second)


def test_lost_cas_response_is_reconciled_only_by_original_intent(tmp_path, monkeypatch):
    store = Store()
    one = coordinator(store)
    monkeypatch.setattr(c, "configured", lambda: one)
    store.lose_response = True
    with pytest.raises(c.CoordinationError):
        c.reserve_submission(REF, timestamp(AT), root=tmp_path)
    assert len(list((tmp_path/"coordination-intents").glob("*.json"))) == 1
    assert coordinator(store, "gway-004").acquire(REF, AT) is None
    store.lose_response = False
    claim = c.reserve_submission(REF, timestamp(AT), root=tmp_path)
    assert one.verify(REF, AT, claim)
    assert store.head == 1


def test_prelaunch_shared_reservation_and_worker_adoption(tmp_path, monkeypatch):
    store = Store()
    one = coordinator(store)
    monkeypatch.setattr(c, "configured", lambda: one)
    runner = Mock(return_value=Mock(returncode=0))
    with patch("gway_agent_ops.nonblocking.resolve", return_value={"labels": []}), \
         patch("gway_agent_ops.nonblocking._launch_prerequisites", return_value="/usr/bin/agent"), \
         patch("gway_agent_ops.nonblocking._same_user_runtime_dir", return_value=None):
        result = submit(REF, AT, state_dir=tmp_path, runner=runner, trace_id="b"*32)
    assert result["state"] == "requested"
    assert runner.call_count == 1
    assert observe(REF, AT, state_dir=tmp_path)["submission_trace_id"] == "b"*32
    marker = tmp_path/"requests"/(_key(REF, timestamp(AT))+".json")
    claim = json.loads(marker.read_text())["coordination_claim"]
    assert coordinator(store, "gway-004").acquire("arthexis/example#2", AT) is None
    with admission(REF, AT, state_dir=tmp_path) as row:
        assert row["coordination_claim"] == claim
        assert store.head == 1
        enqueue(REF, timestamp(AT), state_dir=tmp_path, status="finished", exit_code=0)
    assert store.data["claims"] == {}
    assert coordinator(store, "gway-004").acquire(REF, AT) is None


def test_worker_timeout_or_exception_keeps_fence(tmp_path, monkeypatch):
    store = Store()
    monkeypatch.setattr(c, "configured", lambda: coordinator(store))
    with pytest.raises(RuntimeError):
        with admission(REF, AT, state_dir=tmp_path):
            raise RuntimeError("uncertain worker")
    assert coordinator(store, "gway-004").acquire("arthexis/example#2", AT) is None


def test_no_configuration_does_not_adopt_fenced_marker(tmp_path, monkeypatch):
    store = Store()
    monkeypatch.setattr(c, "configured", lambda: coordinator(store))
    claim = c.reserve_submission(REF, timestamp(AT), root=tmp_path)
    marker = tmp_path/"marker.json"
    marker.write_text(json.dumps({"issue": REF, "at": timestamp(AT), "coordination_claim": claim}))
    monkeypatch.setattr(c, "configured", lambda: None)
    with pytest.raises(c.CoordinationError):
        c.reserve_admission(REF, timestamp(AT), marker=marker)


def test_github_wire_uses_exact_parent_nonforced_update():
    store = Store()
    state = deepcopy(store.data)
    raw = json.dumps(state).encode()
    replies = [
        {"private": True, "full_name": CONFIG["repository"]},
        {"ref": "refs/heads/"+c.BRANCH, "object": {"type": "commit", "sha": "a"*40}},
        {"type": "file", "encoding": "base64", "size": len(raw),
         "content": base64.b64encode(raw).decode()},
        {"sha": "b"*40}, {"sha": "c"*40},
        {"ref": "refs/heads/"+c.BRANCH, "object": {"sha": "c"*40}},
    ]
    runner = Mock(side_effect=[Mock(returncode=0, stdout=json.dumps(x)) for x in replies])
    wire = c.GitHubRefStore(CONFIG, runner=runner)
    assert coordinator(wire).acquire(REF, AT)
    calls = runner.call_args_list
    assert json.loads(calls[4].kwargs["input"])["parents"] == ["a"*40]
    assert json.loads(calls[5].kwargs["input"]) == {"sha": "c"*40, "force": False}
    assert "PATCH" in calls[5].args[0]


def test_public_repository_cannot_receive_state():
    runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps(
        {"private": False, "full_name": CONFIG["repository"]})))
    with pytest.raises(c.CoordinationError):
        coordinator(c.GitHubRefStore(CONFIG, runner=runner)).acquire(REF, AT)
    assert runner.call_count == 1


def test_general_pickup_gate_requires_explicit_acceptance(monkeypatch):
    monkeypatch.setattr(c, "load_config", lambda: None)
    assert not c.general_pickup_ready()
    monkeypatch.setattr(c, "load_config", lambda: CONFIG)
    assert not c.general_pickup_ready()
    monkeypatch.setattr(c, "load_config", lambda: {**CONFIG, "general_pickup_accepted": True})
    assert c.general_pickup_ready()
