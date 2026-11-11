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
  for deduplication. It never reruns Codex. Its per-user report.lock
  serializes the GitHub marker-check-and-post phase. A tick processes at
  most four reports within an 80-second soft deadline, with each GitHub API
  call bounded by its remaining time. Failed and deferred items remain queued.
- An opt-in user systemd oneshot + calendar timer is provided in
  systemd/gway-agent-report.{service,timer}; no installation, enablement,
  or service restart is performed by this PR. The 120-second service timeout
  is larger than the outbox soft deadline plus one bounded GitHub API call.
  CI only inspects these declarative units.

## Unresolved safety boundaries — keep this PR Draft

The accepted-journal recovery helper is unreachable for some orphans:
admission() refuses unresolved accepted work before executor.execute() can call
recover(). This increment intentionally does NOT invoke recover() blindly from
status, submit, or report: without proof of an inactive worker it could
misclassify a still-running task as interrupted.

The explicit release operation requires a systemd-backed request marker.
For direct CLI execution, a new explicit `agent recover-direct REF --at UTC`
path is available only when acceptance captured **actual** process membership
in a dedicated user systemd unit named `gway-agent-direct-<digest>.service`
and the original systemd INVOCATION_ID. The command holds the existing
submission and execution locks, reads but does not stop the unit, and
requires matching InvocationID, KillMode=control-group, inactive/dead status,
TasksCurrent=0, MainPID=0, ControlPID=0 and a matching or cleared ControlGroup.
Only then may it enqueue an interrupted report; it never reruns Codex.

Bare interactive/legacy direct invocations have no such provenance and **must
remain blocked**, even when a PID is absent. A collected unit without
InvocationID evidence also remains blocked; PID reuse, uncertain descendants
and daemon restarts are not acceptable grounds for release. To make future
direct invocations recoverable, start `agent issue REF --at UTC` under a
dedicated **uncollected** user systemd service using the derived identity
below, with KillMode=control-group (no new daemon or provider registry):

```sh
REF='arthexis/example#19'
AT='2026-11-11T15:00:00Z'
UNIT="$(python3 -c 'import hashlib,sys; from gway_agent_ops.dispatch import timestamp; x=sys.argv[1]+"\n"+timestamp(sys.argv[2]); print("gway-agent-direct-"+hashlib.sha256(x.encode()).hexdigest()[:32])' "$REF" "$AT")"
systemd-run --user --wait --unit="$UNIT" --property=KillMode=control-group \
  "$HOME/.local/bin/agent" issue "$REF" --at "$AT" --json
```

This command is documentation only, not executed or activated here. The
dedicated unit must remain inspectable by systemd for later recovery; an
unloaded/garbage-collected unit cannot furnish the necessary identity proof.
A privileged child escaping the service cgroup is outside this guarantee.
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
2. Verify direct CLI supervised recovery in a disposable user systemd
   environment, including retained InvocationID and no descendant processes;
   legacy unsupervised invocations remain blocked.
3. Independently authorize publication and enabling of the provided
   outbox-only systemd service/timer on gway-001; verify real retry behavior,
   concurrency and journald evidence without Codex replay.
4. Inspect deployed SHA, user systemd bus, pending requests/outbox on gway-001
   in read-only mode; authorize any disposable live dispatch separately.
5. Coordinate pinned CLI/MCP revision and provider timeout with GWay Remote
   without introducing a second registry or admission authority.

No merge, deployment or production action is authorized by this Draft.
