"""Bounded recurring scheduler integrated with the existing single executor.

One-shot schedules have their own ledger and are unchanged. Recurrence is an
explicitly activated policy, never a substitute for per-issue approval. Claims
are durable before GitHub or systemd I/O, and uncertain claims are NEVER
automatically retried.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

from . import recurring_store
from .codex_budget import read_budgets
from .github_issues import resolve, IssueRejected
from .nonblocking import submit
from .observation import observe
from .recurring_policy import RecurringPolicy
from .scheduler import _approved, ledger_path
from .task_ledger import TaskLedger

MAX_DEFINITIONS = 8
MAX_CANDIDATES = 50
MAX_DISPATCH = 2
MAX_PAGES = 3
BUSY_DELAY = timedelta(minutes=2)
TERMINAL = {"finished", "codex_failed", "interrupted", "released"}


def _policy(definition):
    return RecurringPolicy(definition["repository"],
        timedelta(seconds=definition["every_seconds"]), definition["issue"],
        definition["title_prefix"], definition["title_regex"])


def _stamp(now):
    return recurring_store._instant(now)


def _fingerprint(issue):
    return hashlib.sha256(json.dumps(
        [issue.get("title"), issue.get("body")],
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _budget_allowed(budget):
    # Do not infer entitlement from a missing/stale rate-limit report.
    return all(isinstance(budget.get(k), dict) and budget[k].get("available") is True
               and type(budget[k].get("remaining_percent")) in (int, float)
               and budget[k]["remaining_percent"] > 0 for k in ("five_hour", "weekly"))


def discover(repository, *, runner=subprocess.run, cap=MAX_CANDIDATES):
    """Bounded, deterministic GitHub discovery with no PRs or cross-repo search."""
    if not RecurringPolicy.parse(repository, "1h", title_prefix="test").repository == repository:
        raise ValueError("invalid_repository")
    if type(cap) is not int or not 1 <= cap <= MAX_CANDIDATES:
        raise ValueError("invalid_discovery_cap")
    result = []
    for page in range(1, MAX_PAGES + 1):
        try:
            response = runner(["gh", "api", f"repos/{repository}/issues",
                               "-f", "state=open", "-f", "per_page=100",
                               "-f", f"page={page}", "--method", "GET"],
                capture_output=True, text=True, timeout=20, check=False,
                env={k: v for k, v in os.environ.items()
                     if k not in ("GH_TOKEN", "GITHUB_TOKEN")})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("recurring_discovery_unavailable") from exc
        if response.returncode != 0:
            raise ValueError("recurring_discovery_unavailable")
        try:
            batch = json.loads(response.stdout)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid_discovery_payload") from exc
        if type(batch) is not list or len(batch) > 100:
            raise ValueError("invalid_discovery_payload")
        # Reject incomplete collections rather than dispatching from a
        # misleading prefix of a large issue list.
        if page == MAX_PAGES and len(batch) == 100:
            raise ValueError("discovery_page_limit")
        for item in batch:
            if not isinstance(item, dict):
                raise ValueError("invalid_discovery_payload")
            if "pull_request" in item or item.get("state") != "open":
                continue
            number = item.get("number")
            if type(number) is not int or number <= 0 or type(item.get("title")) is not str:
                raise ValueError("invalid_discovery_payload")
            if (not isinstance(item.get("user"), dict)
                    or type(item["user"].get("login")) is not str):
                raise ValueError("invalid_discovery_payload")
            result.append({"ref": f"{repository}#{number}", "title": item["title"],
                           "state": "open", "creator": item["user"]["login"],
                           "body": item.get("body") or ""})
        if len(batch) < 100:
            break
    result.sort(key=lambda issue: int(issue["ref"].split("#")[1]))
    # Never silently truncate a candidate set, which could starve later issues.
    if len(result) > cap:
        raise ValueError("discovery_candidate_limit")
    return result


def _closed_or_missing(exc):
    return isinstance(exc, IssueRejected) and exc.code == "issue_not_open"


def _update_issue(db, did, ref, state, now, **values):
    fields = {"state": state, "updated_at": _stamp(now), **values}
    set_sql = ",".join(f"{key}=?" for key in fields)
    db.execute(f"UPDATE recurring_issue_state SET {set_sql} "
               "WHERE definition_id=? AND issue=?",
               (*fields.values(), did, ref))


def _set_definition(db, did, state, now, error=None):
    db.execute("""UPDATE recurring_definitions
        SET state=?,last_error=?,last_evaluated_at=?,updated_at=? WHERE id=?""",
        (state, error, _stamp(now), _stamp(now), did))


def _refresh_terminal(definition, row, issue, *, now, observer, path):
    """Only durable terminal receipts can advance a recurring due instant."""
    at = row.get("last_invocation_at")
    if not at:
        return False
    try:
        receipt = observer(row["issue"], at)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if type(receipt) is not dict or receipt.get("state") not in TERMINAL:
        return False
    if receipt["state"] == "finished" and receipt.get("delivery_confirmed") is not True:
        # A Codex process exit alone does not prove that its durable findings
        # were delivered; do not schedule the next invocation prematurely.
        return False
    did, ref = definition["id"], row["issue"]
    stamp = _stamp(now)
    progress = _fingerprint(issue)
    unchanged = progress == row["progress_fingerprint"]
    no_progress = row["no_progress_count"] + 1 if unchanged else 0
    count = row["run_count"]
    policy = _policy(definition)
    due = datetime.fromisoformat(row["next_due"].replace("Z", "+00:00"))
    next_due = _stamp(policy.next_due(due, now=now))
    terminal = (count >= definition["max_runs"] or
                no_progress >= definition["no_progress_limit"] or
                receipt["state"] != "finished")
    reason = ("max_runs" if count >= definition["max_runs"] else
              "no_progress" if no_progress >= definition["no_progress_limit"] else
              "execution_" + receipt["state"] if receipt["state"] != "finished" else None)
    with TaskLedger(path) as ledger:
        db = ledger.db
        db.execute("BEGIN IMMEDIATE")
        try:
            current = db.execute("""SELECT * FROM recurring_issue_state
                WHERE definition_id=? AND issue=?""", (did, ref)).fetchone()
            if (current is None or current["state"] != "pending"
                    or current["last_invocation_at"] != at):
                db.execute("COMMIT")
                return False
            _update_issue(db, did, ref, "paused" if terminal else "pending", now,
                next_due=next_due, last_invocation_at=None,
                last_receipt=json.dumps({"state": receipt["state"], "exit_code": receipt.get("exit_code")}),
                progress_fingerprint=progress, no_progress_count=no_progress,
                claim_at=None, next_attempt_at=None, last_error=reason)
            recurring_store._audit(db, did, ref, "terminal", stamp, reason or receipt["state"])
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return True


def _claim(definition, issue, *, now, path):
    did, ref, stamp = definition["id"], issue["ref"], _stamp(now)
    with TaskLedger(path) as ledger:
        db = ledger.db
        db.execute("BEGIN IMMEDIATE")
        try:
            current = db.execute("""SELECT * FROM recurring_issue_state
                WHERE definition_id=? AND issue=?""", (did, ref)).fetchone()
            gate = db.execute("SELECT state FROM recurring_definitions WHERE id=?",
                              (did,)).fetchone()
            if (not gate or gate["state"] != "active" or not current
                    or current["state"] != "pending"
                    or current["last_invocation_at"] is not None
                    or current["next_due"] > stamp
                    or current["next_attempt_at"] and current["next_attempt_at"] > stamp):
                db.execute("COMMIT")
                return None
            approval = _approved(db, ref)
            # Freeze once, BEFORE I/O; a crash leaves 'claimed' and is not
            # silently re-dispatched with a different UTC identity.
            at = current["claim_at"] or stamp
            _update_issue(db, did, ref, "claimed", now, claim_at=at,
                          approval_event_id=approval, last_error=None)
            recurring_store._audit(db, did, ref, "claimed", stamp)
            db.execute("COMMIT")
            return {**dict(current), "claim_at": at, "approval_event_id": approval}
        except BaseException:
            db.execute("ROLLBACK")
            raise


def _dispatch(definition, issue, *, now, resolver, submitter, observer, path, budget_reader):
    did, ref, stamp = definition["id"], issue["ref"], _stamp(now)
    if not _budget_allowed(budget_reader()):
        return {"issue": ref, "state": "budget_deferred"}
    try:
        claim = _claim(definition, issue, now=now, path=path)
    except ValueError:
        return {"issue": ref, "state": "approval_required"}
    if claim is None:
        return None
    state, code = "uncertain", "launch_unconfirmed"
    try:
        fresh = resolver(ref)
        if (fresh.get("state") != "open" or fresh.get("creator", "").casefold() != "arthexis"
                or not _policy(definition).matches({"ref": ref, "state": "open",
                                                     "title": fresh.get("title")})):
            code = "untrusted_issue"
        else:
            with TaskLedger(path) as ledger:
                db = ledger.db
                db.execute("BEGIN IMMEDIATE")
                try:
                    _approved(db, ref, expected=claim["approval_event_id"])
                    result = submitter(ref, claim["claim_at"],
                                       source="scheduled", trigger="recurring-due")
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            code = result.get("state", "launch_unconfirmed")
            if code == "already_requested":
                prior = observer(ref, claim["claim_at"])
                code = "requested" if (prior.get("source") == "scheduled"
                        and prior.get("trigger") == "recurring-due") else "identity_conflict"
            if code == "requested":
                state = "pending"
            elif code == "busy":
                state = "pending"  # definitive no-submit response, frozen ID reused
            else:
                state = "uncertain"
    except (IssueRejected, ValueError, OSError, RuntimeError, KeyError, TypeError):
        # Cannot prove a post-claim failure did not send an external request.
        code = "preflight_or_launch_uncertain"
    with TaskLedger(path) as ledger:
        db = ledger.db
        db.execute("BEGIN IMMEDIATE")
        try:
            current = db.execute("""SELECT state,claim_at FROM recurring_issue_state
                WHERE definition_id=? AND issue=?""", (did, ref)).fetchone()
            if current and current["state"] == "claimed" and current["claim_at"] == claim["claim_at"]:
                if code == "busy":
                    _update_issue(db, did, ref, state, now,
                        next_attempt_at=_stamp(now + BUSY_DELAY),
                        last_receipt=json.dumps({"code": "busy_deferred"}))
                elif state == "pending":
                    _update_issue(db, did, ref, state, now,
                        last_invocation_at=claim["claim_at"], run_count=claim["run_count"] + 1,
                        progress_fingerprint=_fingerprint(issue),
                        last_receipt=json.dumps({"code": code}), next_attempt_at=None)
                else:
                    _update_issue(db, did, ref, state, now,
                        last_receipt=json.dumps({"code": code}), last_error=code)
                recurring_store._audit(db, did, ref, state, stamp, code)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return {"issue": ref, "state": state, "code": code}


def tick(*, now=None, resolver=resolve, discoverer=discover, submitter=submit,
         observer=observe, budget_reader=read_budgets, path=None, limit=MAX_DISPATCH):
    """Bounded scheduler pass; user-systemd invokes this through schedule-tick."""
    if type(limit) is not int or not 1 <= limit <= MAX_DISPATCH:
        raise ValueError("invalid_recurring_limit")
    now = now or datetime.now(timezone.utc)
    stamp = _stamp(now)
    path = ledger_path(path)
    try:
        snapshot = recurring_store.snapshot(path=path)
    except (FileNotFoundError, ValueError):
        # An absent pre-release recurring schema means zero active policies.
        return {"processed": 0, "results": []}
    definitions = [d for d in snapshot["definitions"] if d["state"] == "active"]
    results = []
    for definition in definitions[:MAX_DEFINITIONS]:
        did, policy = definition["id"], _policy(definition)
        try:
            if policy.issue is not None:
                try:
                    fresh = resolver(policy.issue)
                    candidates = [{"ref": policy.issue, "state": fresh["state"],
                                   "title": fresh["title"], "body": fresh.get("body", ""),
                                   "creator": fresh["creator"]}]
                except IssueRejected as exc:
                    if not _closed_or_missing(exc):
                        raise
                    with TaskLedger(path) as ledger:
                        db = ledger.db
                        db.execute("BEGIN IMMEDIATE")
                        _set_definition(db, did, "completed", now)
                        db.execute("""UPDATE recurring_issue_state SET state='completed',
                            updated_at=? WHERE definition_id=? AND state IN ('pending','paused')""",
                            (stamp, did))
                        recurring_store._audit(db, did, policy.issue, "closed", stamp)
                        db.execute("COMMIT")
                    results.append({"definition_id": did, "state": "completed"})
                    continue
            else:
                # Discover the entire bounded repository page set first; on
                # GitHub errors do not infer closure from missing candidates.
                candidates = [i for i in discoverer(policy.repository) if policy.matches(i)]
                selected = {candidate["ref"] for candidate in candidates}
                for previous in snapshot["issues"]:
                    if previous["definition_id"] != did or previous["issue"] in selected:
                        continue
                    if previous["state"] in ("completed", "claimed", "uncertain"):
                        continue
                    ref = previous["issue"]
                    try:
                        live = resolver(ref)
                        closed = live.get("state") != "open"
                    except IssueRejected as exc:
                        if not _closed_or_missing(exc):
                            raise
                        closed = True
                    with TaskLedger(path) as ledger:
                        db = ledger.db
                        db.execute("BEGIN IMMEDIATE")
                        try:
                            row = db.execute("""SELECT state FROM recurring_issue_state
                                WHERE definition_id=? AND issue=?""", (did, ref)).fetchone()
                            if row and row["state"] in ("pending", "paused"):
                                if closed:
                                    _update_issue(db, did, ref, "completed", now,
                                                  last_error="issue_closed")
                                    recurring_store._audit(db, did, ref, "closed", stamp)
                                elif row["state"] == "pending":
                                    _update_issue(db, did, ref, "paused", now,
                                                  last_error="selector_mismatch")
                                    recurring_store._audit(db, did, ref, "mismatch", stamp)
                            db.execute("COMMIT")
                        except BaseException:
                            db.execute("ROLLBACK")
                            raise
            for issue in candidates:
                if len(results) >= limit:
                    return {"processed": len(results), "results": results}
                if issue.get("creator", "").casefold() != "arthexis" or not policy.matches(issue):
                    continue
                ref = issue["ref"]
                recurring_store.record_issue(did, ref, now, title=issue.get("title"),
                                             path=path, now=now)
                with TaskLedger(path) as ledger:
                    db = ledger.db
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        pending = db.execute("""SELECT state,last_error FROM recurring_issue_state
                            WHERE definition_id=? AND issue=?""", (did, ref)).fetchone()
                        if (pending and pending["state"] == "paused"
                                and pending["last_error"] == "selector_mismatch"):
                            _update_issue(db, did, ref, "pending", now,
                                          next_due=stamp, last_error=None)
                        db.execute("COMMIT")
                    except BaseException:
                        db.execute("ROLLBACK")
                        raise
                with TaskLedger(path) as ledger:
                    db = ledger.db
                    state = db.execute("""SELECT * FROM recurring_issue_state
                        WHERE definition_id=? AND issue=?""", (did, ref)).fetchone()
                    current = dict(state)
                if current["state"] == "pending" and current["last_invocation_at"]:
                    _refresh_terminal(definition, current, issue, now=now, observer=observer, path=path)
                    continue
                if current["state"] == "pending" and current["next_due"] <= stamp:
                    outcome = _dispatch(definition, issue, now=now, resolver=resolver,
                                        submitter=submitter, observer=observer, path=path,
                                        budget_reader=budget_reader)
                    if outcome is not None and outcome["state"] not in (
                            "approval_required", "budget_deferred"):
                        results.append({"definition_id": did, **outcome})
        except (IssueRejected, OSError, ValueError, RuntimeError, KeyError, TypeError):
            # No unattended auto-retry after an uncertain claim. Outage during
            # discovery preserves the definition and every pending request.
            results.append({"definition_id": did, "state": "deferred",
                            "code": "recurring_evaluation_unavailable"})
    return {"processed": len(results), "results": results}
