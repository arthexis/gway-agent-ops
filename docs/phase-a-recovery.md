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
  a new timestamp or a retry. The direct executor also leaves a timed-out or
  OSError-uncertain Codex invocation **accepted and unresolved**: Python's
  subprocess timeout kills only its immediate child and gives no durable
  descendant-termination witness. It does not enqueue an "interrupted"
  terminal report in these ambiguous cases.
- The existing agent release REF --at UTC is an explicit *operator action*.
  It requires an unlocked execution slot and a confirmed systemd stop/inactivity
  **only for requests with no matching accepted execution**. Once the journal
  contains an accepted invocation without a terminal report, the command
  fails closed with execution_containment_unverified before stopping the unit:
  a free flock and empty original cgroup cannot disprove escaped descendants.
  A never-accepted request may become request_terminal with
  execution_terminal=false; no Codex result is invented.
- agent observe now exposes findings_state independently from exit code,
  terminality, and delivery confirmation. Success with missing/invalid findings
  is not verified completion. If the process crashed after persisting delivered/
  but before unlinking outbox/, the durable delivered receipt has precedence.
- agent report retries only durable GitHub comments using the existing marker
  for deduplication. It never reruns Codex. Its per-user report.lock
  serializes the GitHub marker-check-and-post phase. A durable report-cursor
  advances even after a failed attempt, so poisoned early entries cannot
  starve later reports. A tick processes at most four reports within an
  80-second soft deadline, with each GitHub API call bounded by its remaining
  time. Failed and deferred items remain queued.
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
The same process-family containment requirement now applies to BOTH
`agent release` and `agent recover-direct`: matched accepted orphans are
never promoted to a terminal state from stale systemd metadata alone.
`agent release` intentionally does not stop a matched accepted orphan
without separately verified containment. This is an admission safety stop,
not a replacement for an operator-controlled service stop or live investigation.

For direct CLI execution, a new explicit `agent recover-direct REF --at UTC`
path is available only when acceptance captured **actual** process membership
in a dedicated user systemd unit named `gway-agent-direct-<digest>.service`
and the original systemd INVOCATION_ID. The command holds the existing
submission and execution locks, reads but does not stop the unit, and
requires matching InvocationID, KillMode=control-group, Restart=no, either
inactive/dead or failed/failed state, TasksCurrent=0, MainPID=0, ControlPID=0
and a matching or cleared ControlGroup. SIGKILL can leave systemd in failed/
failed; this combination proves only that the **original** unit is inactive.

**PRE-READY safety correction:** these necessary checks cannot prove that all
process descendants are gone. An executable running under the same user may
request a new scope from the user systemd manager; after its original worker
dies, the escaped process can continue outside the original cgroup. Even
Restart=no, KillMode=control-group, InvocationID and an empty TasksCurrent
cannot rule it out. Therefore `recover-direct` still validates the journal,
locks and systemd evidence, but returns `direct_containment_unverified` and
does **not** enqueue a terminal report or free admission, even if every unit
check passes. This fail-closed behavior is intentional until a verifiable,
non-escapable execution-containment contract is established. No new authority,
permission registry or production user is introduced in this Draft.

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
  --property=Restart=no "$HOME/.local/bin/agent" issue "$REF" --at "$AT" --json
```

This command is a diagnostic example only; it is not executed or activated
here and **does not authorize or enable automatic direct-CLI recovery**. The
dedicated unit must remain inspectable by systemd for later recovery; an
unloaded/garbage-collected unit cannot furnish the necessary identity proof.
A synthetic offline escaped-child case now asserts fail-closed behavior with
the original unit reporting inactive and empty. The test is **not** a live
systemd proof. PRE-READY still requires an independently verifiable
containment/descendant contract (and disposable systemd integration tests)
before allowing direct-CLI orphan release.
Unconfirmed systemd status and active execution locks also remain blocked.
An unknown is-active result needs independent not-found/inactive evidence from
systemd show; a bare unknown is never enough. No automatic resubmission or
optimistic status promotion is allowed.

A confirmed systemd stop does not prove whether Codex completed its intended
changes before interruption, or whether all descendants ended. Neither
recovery path fabricates an interrupted or finished report while containment
remains unverified. A normal report of finished Codex work also does not
establish that detached subprocesses stopped: that is a separate
post-execution acceptance boundary. No recovery operation closes, merges,
or updates a GitHub issue.

## Acceptance needed before Ready

1. CI: offline tests for uncertainty, idempotency, busy admission, running
   locks, uncertain unit stop, explicit unaccepted-request cancellation,
   accepted orphan refusal, retained report, nonduplicated GitHub delivery,
   findings metadata and direct orphan.
2. Design and independently validate a non-escapable worker containment
   boundary, then test real systemd direct-CLI recovery in a disposable
   environment. Until then even fully matching unit metadata is insufficient
   to release direct-CLI orphan admission. Unsupervised legacy calls remain
   blocked. Do not assume TasksCurrent=0 proves descendant termination.
3. Independently authorize publication and enabling of the provided
   outbox-only systemd service/timer on gway-001; verify real retry behavior,
   concurrency and journald evidence without Codex replay.
4. Inspect deployed SHA, user systemd bus, pending requests/outbox on gway-001
   in read-only mode; authorize any disposable live dispatch separately.
5. Coordinate pinned CLI/MCP revision and provider timeout with GWay Remote
   without introducing a second registry or admission authority.

No merge, deployment or production action is authorized by this Draft.

## Minimal containment contract (design only; not deployed)

A reliable automated admission release needs a boundary established **before**
the worker is started, by the existing node-owned executor and systemd, not by
a new approval registry or by observing a historical PID after a crash.

1. **Execution lifetime:** the trusted executor creates one identity-bound
   systemd unit per accepted request, keeps all Codex descendants in a
   non-delegated execution group, and disables service restart.
2. **No scope escape:** either the runtime prevents same-user access to the
   user systemd bus/other cgroup-migration paths, or an independently verifiable
   confined execution identity is used. Merely setting KillMode=control-group,
   Restart=no, NoNewPrivileges=yes or checking TasksCurrent=0 is insufficient.
   Do not assume user-bus isolation without testing the actual runtime.
3. **Terminal witness:** after a crash, the trusted supervisor must prove the
   original invocation identity, empty cgroup **and the containment invariant**
   using evidence independent of Codex; otherwise preserve the durable
   accepted reservation. PID/start time alone cannot prove ancestry after
   PID reuse, and a detached child can survive unit termination.
4. **Offline acceptance:** simulate original-cgroup-empty/escaped-scope-live,
   new InvocationID, restart policy, SIGKILL, corrupt journals, missing units,
   and races while maintaining at-most-once. In a separately authorized
   disposable lab, test the user-bus and cgroup boundary with a synthetic
   child that attempts to migrate scopes; do not use live Codex.

An isolated Linux subprocess test proves the *narrower* negative property that
a Python worker's timed-out parent may exit while a grandchild in another
process session remains alive. This is not a demonstration of systemd cgroup
escape, nor a positive kernel containment certification. The remaining
required positive acceptance includes a real disposable user systemd test of
the proposed boundary, with an intentionally adversarial same-user child
attempting `systemd-run --user --scope`, cgroup migration and detached
processes. The trusted supervisor must disallow such migration (or attest
an equivalent kernel-enforced boundary) before enabling recovery. Merely
observing a process tree or matching an InvocationID is insufficient.

No kernel/user/permission changes, service installation or runtime deployment
are included in this PR. Until the boundary is proved, both accepted-orphan
recovery interfaces remain intentionally unavailable. Normal Codex return
also lacks proof that no detached descendants survive: review the positive
result-reporting path before marking the whole feature Ready.

### Acceptance evidence matrix — proposal, never an automatic grant

| Proof obligation | Evidence that must come from an independent, node-owned supervisor | If unavailable |
| --- | --- | --- |
| Pre-spawn identity | Durable request identity + original unit and invocation bound **before** Codex starts, not environment variables written by Codex | Keep accepted slot |
| Kernel-owned process boundary | Non-delegated cgroup, no worker cgroupfs mutation/migration rights, no usable user-systemd bus, and no privileged or IPC helper able to start another scope | Keep accepted slot |
| Descendant lifetime | Exact original cgroup, held by the supervisor through shutdown, with cgroup v2 \`cgroup.events: populated 0\` **after** completion; recursively covers nested cgroups but never a process that escaped the hierarchy | Keep accepted slot |
| Supervisor continuity | Matching InvocationID, no unexpected restart or collected/changed unit, no active process, and the same trusted containment policy in force throughout the lifetime | Keep accepted slot |
| Durable terminal observation | An atomic, fsynced terminal witness generated by the supervisor **after** the above checks, independent of Codex stdout and request markers; rechecked under the existing admission lock | Keep accepted slot |

The Linux cgroup v2 definition of \`populated\` is recursive over the original
subtree; it does **not** prevent or detect migration to an unrelated cgroup.
See https://docs.kernel.org/admin-guide/cgroup-v2.html#unpopulated-notification .
A \`systemd-run --user\` worker running under an account that can access its
own user manager does **not** satisfy the proposed no-escape obligation merely
because \`Delegate=no\` or \`KillMode=control-group\`. It must be demonstrably
prevented from requesting a new scope, migrating cgroups or invoking a
privileged helper that can migrate on its behalf.

The acceptance harness must be **separate from production**. It should attempt
scope migration through the user bus; new process sessions (\`setsid\`);
orphaned grandchildren; \`SIGKILL\`; unit collection/restart; PID reuse; and
execution-journal tampering. The expected outcome is either demonstrably
denied migration or fail-closed non-release. Reboot/restart and successful
Codex exit paths need the same witness as timeouts. No offline model or unit
property checklist should be construed as a real kernel-enforcement test.

**Current PR implementation:** neither \`recover-direct\` nor \`agent release\`
possesses this witness and both refuse accepted-orphan release. The direct
executor now also preserves the accepted slot on uncertain subprocess timeout
or OSError. The *normal exit-code* reporting path still lacks a descendants
witness, so the feature is **not Ready**, even with all unit tests passing.

