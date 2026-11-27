"""Native mailbox tests: no GitHub, network, Codex or systemd."""
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from gway_agent_ops import coordination as c
from gway_agent_ops.dispatch import Busy, admission, timestamp
from gway_agent_ops.nonblocking import submit
from gway_agent_ops.observation import observe
from gway_agent_ops.reporting import enqueue, _key

REF="arthexis/example#1"
AT="2026-10-13T15:00:00Z"
CONFIG={"schema":1,"epoch":"a"*32,"members":["gway-001","gway-004"],
        "general_pickup_accepted":False}


@pytest.fixture
def mailbox(tmp_path,monkeypatch):
    box=c.Mailbox(tmp_path,CONFIG,node="gway-001")
    monkeypatch.setattr(c,"configured",lambda root=c.DEFAULT_STATE:box)
    return box


def grant(box):
    row=box.snapshot()[0]
    box.apply([{**row,"state":"reserved"}])
    return row


def test_prelaunch_requires_grant_and_keeps_trace(mailbox):
    root=mailbox.root
    runner=Mock(return_value=Mock(returncode=0))
    with patch("gway_agent_ops.nonblocking.resolve",return_value={"labels":[]}), \
         patch("gway_agent_ops.nonblocking._launch_prerequisites",return_value="/usr/bin/agent"), \
         patch("gway_agent_ops.nonblocking._same_user_runtime_dir",return_value=None):
        first=submit(REF,AT,state_dir=root,runner=runner,trace_id="b"*32)
        assert first["state"]=="busy"
        assert runner.call_count==0
        assert observe(REF,AT,state_dir=root)["state"]=="unknown"
        original=grant(mailbox)
        result=submit(REF,AT,state_dir=root,runner=runner,trace_id="b"*32)
    assert result["state"]=="requested" and runner.call_count==1
    assert observe(REF,AT,state_dir=root)["submission_trace_id"]=="b"*32
    marker=root/"requests"/(_key(REF,timestamp(AT))+".json")
    claim=json.loads(marker.read_text())["coordination_claim"]
    assert claim["token"]==original["token"]
    with admission(REF,AT,state_dir=root) as row:
        assert row["coordination_claim"]==claim
        enqueue(REF,timestamp(AT),state_dir=root,status="finished",exit_code=0)
    assert mailbox.snapshot()==[{**original,"state":"terminal"}]
    mailbox.apply([{**original,"state":"released"}])
    assert mailbox.reserve(REF,AT) is None
    assert mailbox.snapshot()==[]


def test_direct_worker_requires_same_fence(mailbox):
    with pytest.raises(Busy):
        with admission(REF,AT,state_dir=mailbox.root):
            pytest.fail("ungranted worker")
    original=grant(mailbox)
    with pytest.raises(RuntimeError):
        with admission(REF,AT,state_dir=mailbox.root):
            raise RuntimeError("worker uncertain")
    assert mailbox.snapshot()==[]
    assert mailbox.reserve("arthexis/example#2",AT) is None
    assert mailbox.snapshot()==[]
    # Reservation retained. A terminal acknowledgement cannot be fabricated by
    # a late grant or a different token.
    with pytest.raises(c.CoordinationError):
        mailbox.apply([{**original,"state":"released"}])


def test_delayed_grant_cannot_resurrect_terminal(mailbox):
    assert mailbox.reserve(REF,AT) is None
    original=grant(mailbox)
    claim=mailbox.reserve(REF,AT)
    mailbox.terminal(REF,AT,claim)
    mailbox.apply([{**original,"state":"reserved"}])
    assert mailbox.reserve(REF,AT) is None
    assert mailbox.snapshot()==[{**original,"state":"terminal"}]
    mailbox.apply([{**original,"state":"released"}])
    mailbox.apply([{**original,"state":"reserved"}])
    assert mailbox.reserve(REF,AT) is None


def test_local_replay_does_not_create_second_intent(mailbox):
    mailbox.reserve(REF,AT)
    first=mailbox.snapshot()[0]
    assert mailbox.reserve("ARTHEXIS/EXAMPLE#1",AT) is None
    assert mailbox.snapshot()==[first]
    mailbox.apply([{**first,"state":"replayed"}])
    assert mailbox.reserve(REF,AT) is None
    assert mailbox.snapshot()==[]


def test_epoch_and_token_binding(mailbox):
    mailbox.reserve(REF,AT)
    first=mailbox.snapshot()[0]
    with pytest.raises(c.CoordinationError):
        mailbox.apply([{**first,"token":"0"*32,"state":"reserved"}])
    with pytest.raises(c.CoordinationError):
        c.Mailbox(mailbox.root,{**CONFIG,"epoch":"0"*32},node="gway-001").snapshot()
    with pytest.raises(c.CoordinationError):
        c.exchange({"operation":"snapshot","epoch":"0"*32},root=mailbox.root)
    assert mailbox.snapshot()==[first]


def test_removing_config_does_not_bypass_existing_mailbox(tmp_path,monkeypatch):
    c.Mailbox(tmp_path,CONFIG,node="gway-001").reserve(REF,AT)
    monkeypatch.setattr(c,"load_config",lambda:None)
    with pytest.raises(c.CoordinationError):
        c.configured(tmp_path)


def test_general_pickup_gate(monkeypatch):
    monkeypatch.setattr(c,"load_config",lambda:None)
    assert not c.general_pickup_ready()
    monkeypatch.setattr(c,"load_config",lambda:CONFIG)
    assert not c.general_pickup_ready()
    monkeypatch.setattr(c,"load_config",lambda:{**CONFIG,"general_pickup_accepted":True})
    assert c.general_pickup_ready()


def test_owner_retires_grant_without_launch(mailbox):
    mailbox.reserve(REF,AT)
    original=grant(mailbox)
    assert c.reconcile_terminal(REF,AT,root=mailbox.root)["state"]=="coordination_release_recorded"
    assert mailbox.snapshot()==[{**original,"state":"terminal"}]
    assert observe(REF,AT,state_dir=mailbox.root)["state"]=="unknown"
