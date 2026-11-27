import json
from types import SimpleNamespace

import pytest

from gway_agent_ops.github_issues import IssueRejected
from gway_agent_ops.pickup_evidence import candidates, label_events


def event(id):
    return {"id": id, "event": "labeled"}


def test_complete_events():
    pages = iter([list(map(event, range(1, 101))), [event(101)]])
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(next(pages)))
    assert len(label_events("arthexis/gway-agent-ops#126", runner=runner)) == 101
    assert calls[0][-1].endswith("page=1")
    assert calls[1][-1].endswith("page=2")


@pytest.mark.parametrize("payload", [[event(1), event(1)], [{"id": False}], {"message": "error"}])
def test_bad_events_fail_closed(payload):
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
    with pytest.raises(IssueRejected, match="pickup_events_unavailable"):
        label_events("arthexis/gway-agent-ops#126", runner=runner)


def test_page_bound():
    counter = iter(range(1, 301))
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=0,
            stdout=json.dumps([event(next(counter)) for _ in range(100)]))
    with pytest.raises(IssueRejected, match="pickup_events_incomplete"):
        label_events("arthexis/gway-agent-ops#126", runner=runner)


def test_candidates():
    def searcher(*, label):
        assert label == "gway-001"
        return [{"repository": "arthexis/repo", "number": i, "labels": labels}
                for i, labels in [(1, ["gway-001"]), (2, ["gway-001", "on-hold"]),
                                  (3, ["gway-004"])]]
    assert candidates("gway-001", searcher=searcher) == ["arthexis/repo#1"]
