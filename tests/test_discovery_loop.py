"""Persistent discovery loop remains restart-safe and never dispatches."""
import pytest
from gway_agent_ops.discovery_worker import run_forever

def test_loop_reopens_ledger_and_stops_after_three_ticks():
    opened, closed, calls, reports = [], [], [], []
    class Ledger:
        def __enter__(self):
            opened.append(1)
            return self
        def __exit__(self, *args):
            closed.append(1)
    def iteration(ledger):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("transient failure")
        return {"discovered": 0, "reconciled": 0, "errors": []}
    run_forever(ledger_path="unused", interval=5,
                stop=lambda: len(calls) >= 3,
                sleep=lambda interval: None,
                open_ledger=lambda path: Ledger(), iteration=iteration,
                report=reports.append)
    assert len(opened) == len(closed) == len(calls) == 3
    assert reports[1]["errors"] == ["discovery_iteration_failed"]

@pytest.mark.parametrize("interval", [0, 4, 3601, True])
def test_invalid_poll_interval(interval):
    with pytest.raises(ValueError):
        run_forever(ledger_path="unused", interval=interval, stop=lambda: True)
