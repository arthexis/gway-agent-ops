# AgentOps Phase A — safe recovery (Draft)

This increment preserves **at-most-once execution**. It never dispatches work,
adds a queue, changes OAuth, restarts services or configures production.
All new tests use isolated directories and mocked systemd/GitHub interfaces.

## Durable submission, observation, and recovery

- A request is fsynced before systemd-run. A confirmed launch stays
  requested. Nonzero launch or timeout is persisted as launch_unconfirmed.
  A crash before the update leaves requested intentionally ambiguous.
- The same (issue, UTC timestamp) cannot launch twice. Other issues remain busy
  while a request is unresolved. Neither a timeout nor a reconnect authorizes
  a new timestamp or a retry.
- The existing agent release REF --at UTC is an explicit *operator action*.
  It requires an unlocked execution slot and confirmed systemd stop/inactivity.
  When an accepted invocation has no terminal report, it durably enqueues an
  interrupted report **before** marking the slot released. Unaccepted requests
  become request_terminal with execution_terminal=false; no Codex result is
  invented.
- agent observe now exposes findings_state independently from exit code,
  terminality, and delivery confirmation. Success with missing/invalid findings
  is not verified completion.
- agent report retries only durable GitHub comments using the existing marker
  for deduplication. It never reruns Codex.

## Unresolved safety boundaries — keep this PR Draft

The accepted-journal recovery helper is unreachable for some orphans:
admission() refuses unresolved accepted work before executor.execute() can call
recover(). This increment intentionally does NOT invoke recover() blindly from
status, submit, or report: without proof of an inactive worker it could
misclassify a still-running task as interrupted.

The explicit release operation requires a systemd-backed request marker.
Direct CLI invocations with no request marker remain blocked after a crash.
Unconfirmed systemd status and active execution locks also remain blocked.
An unknown is-active result needs independent not-found/inactive evidence from
systemd show; a bare unknown is never enough. No automatic resubmission or
optimistic status promotion is allowed.

A confirmed systemd stop does not prove whether Codex completed its intended
changes before the interruption. The report says interrupted, never finished,
and never automatically closes, merges, or updates a GitHub issue beyond a
separately invoked report delivery.

## Acceptance needed before Ready

1. CI: offline tests for uncertainty, idempotency, busy admission, running
   locks, uncertain unit stop, explicit stopped-unit reconciliation, retained
   report, nonduplicated GitHub delivery, findings metadata and direct orphan.
2. Design a guarded, evidence-based reconciliation for direct CLI orphans.
3. Add supervised outbox-only retry with bounded backoff, without Codex replay.
4. Inspect deployed SHA, user systemd bus, pending requests/outbox on gway-001
   in read-only mode; authorize any disposable live dispatch separately.
5. Coordinate pinned CLI/MCP revision and provider timeout with GWay Remote
   without introducing a second registry or admission authority.

No merge, deployment or production action is authorized by this Draft.
