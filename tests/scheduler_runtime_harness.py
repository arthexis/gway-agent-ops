"""Opt-in disposable scheduler harness. Never dispatches a real AgentOps job.

Use an isolated git checkout at the expected revision and a private scratch ledger.
For systemd runtime testing, a uniquely named transient unit may invoke this
script with 'tick'; never invoke the installed scheduler unit.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

EXPECTED_HEAD = "4e7507240c3386270a466be7115926996be1835b"
REF = "arthexis/gway-agent-ops#87"


def trusted(_ref):
    return {"state": "open", "creator": "arthexis", "labels": []}


def verify(checkout, ledger):
    checkout = Path(checkout).resolve(strict=True)
    ledger = Path(ledger).absolute()
    head = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    if head != EXPECTED_HEAD:
        raise RuntimeError("wrong_source_head")
    if subprocess.check_output(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            text=True).strip():
        raise RuntimeError("dirty_frozen_source")
    if not (checkout / "gway_agent_ops" / "scheduler.py").is_file():
        raise RuntimeError("missing_scheduler_source")
    if ledger.is_symlink() or ledger.parent.is_symlink():
        raise RuntimeError("unsafe_test_ledger")
    if not ledger.parent.is_dir():
        raise RuntimeError("test_directory_must_preexist")
    st = ledger.parent.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise RuntimeError("unsafe_test_directory")
    actual = ledger.resolve()
    production = (Path.home() / ".local/state/gway-agent-ops/tasks.sqlite3").resolve()
    if actual == production or actual == checkout or checkout in actual.parents:
        raise RuntimeError("production_or_checkout_ledger_forbidden")
    sys.path.insert(0, str(checkout))
    from gway_agent_ops import scheduler
    if Path(scheduler.__file__).resolve().parent != checkout / "gway_agent_ops":
        raise RuntimeError("source_not_frozen")
    return scheduler


def execute(args):
    scheduler = verify(args.checkout, args.ledger)
    from gway_agent_ops.task_ledger import TaskLedger
    now = datetime.now(timezone.utc)
    if args.action == "prepare":
        with TaskLedger(args.ledger) as task:
            task.discover(REF)
            task.decide(REF, "approve", actor="runtime-harness")
        record = scheduler.create(
            REF, (now + timedelta(seconds=1)).isoformat(),
            ledger_path=args.ledger, now=now, resolver=trusted)
        print(json.dumps({"prepared": record["schedule_id"], "source": EXPECTED_HEAD}))
    elif args.action == "tick":
        def fake_submit(ref, at, **kwargs):
            marker = Path(args.ledger).with_suffix(".fake_calls")
            with marker.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "issue": ref, "at": at, "source": kwargs.get("source")}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if args.crash_after_fake:
                os._exit(71)
            return {"state": "requested"}
        result = scheduler.tick(
            ledger_path=args.ledger, now=now + timedelta(minutes=2),
            resolver=trusted, submitter=fake_submit)
        print(json.dumps(result))
    elif args.action == "inspect":
        print(json.dumps(scheduler.list_schedules(ledger_path=args.ledger)))


def timeout_cleanup_probe(timeout=0.3):
    """Real process-group timeout/cleanup probe with synthetic children only."""
    child = subprocess.Popen([
        sys.executable, "-c",
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "time.sleep(60)"],
        start_new_session=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    try:
        try:
            child.wait(timeout=timeout)
            raise RuntimeError("probe_exited_early")
        except subprocess.TimeoutExpired:
            pass
    finally:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=2)
    try:
        os.killpg(child.pid, 0)
    except ProcessLookupError:
        return {"timeout": True, "process_group_gone": True}
    raise RuntimeError("descendants_not_quiescent")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("prepare", "tick", "inspect", "timeout-probe"))
    p.add_argument("--checkout", required=True)
    p.add_argument("--ledger", required=True)
    p.add_argument("--crash-after-fake", action="store_true")
    args = p.parse_args(argv)
    if args.crash_after_fake and args.action != "tick":
        p.error("--crash-after-fake only valid for tick")
    if args.action == "timeout-probe":
        print(json.dumps(timeout_cleanup_probe()))
    else:
        execute(args)


if __name__ == "__main__":
    main()
