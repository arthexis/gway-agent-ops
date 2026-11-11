# AgentOps Phase A — trusted administrative executor (Draft)

AgentOps performs trusted administrative work on the node under its existing
execution identity. It is not a container, untrusted-workload sandbox or
promise to terminate all child processes. It can legitimately install lasting
systemd services, configure hardware, run Ansible, deploy, test rollbacks and
start separately managed jobs. Watchtower remains the authority over which
nodes are executable; this PR creates no additional authorization authority.

**Result scope:** \`finished\` means the *Codex process* returned zero;
\`codex_failed\` means Codex returned nonzero; \`interrupted\` means an explicit
operator reconciliation of an uncertain/stopped Codex invocation. None attests
that deployed services, hardware state, independent jobs or rollbacks have
finished. Terminal reports and receipts explicitly carry
\`admin_work_state=not_attested\`. Codex process status, findings validation,
delivery status and independent administrative work are separate dimensions.
No new generic job ledger is introduced; external work can be inspected using
the existing service, Ansible or GWay Remote mechanism.

## At-most-once, durability and normal completion

- The private adopted `CODEX_HOME` and its existing policy are passed to
  `codex exec -` unchanged. AgentOps reconstructs `XDG_RUNTIME_DIR` only
  if `/run/user/<current-uid>/bus` exists as a UNIX socket inside a
  current-user-owned, private, non-symlink runtime directory. This restores
  same-user `systemctl --user` connectivity for non-login administrative
  contexts without changing node permissions, config files or service state.
  No arbitrary caller-supplied bus path, DBus address or ambient tokens are
  passed to Codex. The same verified bus is also used for the *launching*
  `systemd-run --user` process: `agent submit` no longer fails preflight
  solely because a non-login executor omitted ambient XDG_RUNTIME_DIR.
  A missing or unsafe bus fails preflight before recording a new request;
  existing at-most-once handling still governs ambiguous launches.
- A request is fsynced under the existing locks before launching Codex. The
  frozen identity is \`(issue, normalized UTC timestamp)\`; a duplicate identity
  is never run twice, including after a restart.
- \`agent submit\` fsyncs a request marker before \`systemd-run\`. Launch
  timeout/failure becomes \`launch_unconfirmed\`; no automatic retry. An
  interrupted marker update also remains uncertain, not a reexecution permit.
- On Codex return code zero or nonzero, the executor fsyncs a terminal
  \`finished\` or \`codex_failed\` report **before** releasing admission.
  A normal Codex return is enough to close *this invocation*, not to prove
  completion of any independent administrative effects.
- Durable outbox and delivered receipts are accepted as terminal only when
  the identity-bound filename, request identity, deduplication marker and
  terminal status are consistent. A mismatched/partial report fails closed
  in observation, reentrant enqueue and report delivery; it cannot unlock
  admission or post comments to a different issue.
- On subprocess timeout, ambiguous \`OSError\`, crash or failed report fsync,
  accepted remains unresolved, blocks the next issue and never fabricates a
  finished report. The operator must reconcile it explicitly. The legacy
  `reporting.recover()` entrypoint now validates the accepted journal only:
  it never synthesizes `interrupted` without an explicit operator action.
  Corrupt or symlinked journals fail closed.
- Outbox delivery has an existing marker-based GitHub deduplication protocol,
  serialized report lock, durable receipt and bounded retries. It does not
  execute Codex. The opt-in outbox-only systemd service/timer included in
  this Draft is **not installed or activated**.

## Explicit operator reconciliation — no containment promise

\`agent release REF --at UTC\` applies only to a systemd-backed submission.
Holding the existing submission and execution locks, it validates the
request and acceptance journals, asks systemd to stop the matching Codex
unit, and confirms it reports inactive/failed or verified collected/absent.

- Never-accepted request: write durable request-only \`released\`; no invented
  Codex execution or report.
- Previously accepted Codex request: first fsync an \`interrupted\` report,
  then write the durable \`released\` marker. An outbox write failure blocks
  release. The original identity cannot run again, even after operator
  reconciliation.
- \`interrupted\` describes uncertain Codex completion, not host quiescence.
  Independent systemd services, Ansible jobs and hardware operations may
  continue and require separate inspection.

\`agent recover-direct REF --at UTC\` is a separate **explicit** action only
for a direct Codex invocation with a dedicated inspectable systemd unit
recorded at admission. It never stops a unit or reexecutes Codex. It holds
both existing locks, validates the original \`InvocationID\`, unit and
cgroup, requires \`Restart=no\`, \`KillMode=control-group\`, empty original
unit and no active main/control PIDs, then fsyncs \`interrupted\`.
Unsupervised/legacy direct invocations, collected units without identity,
restarted units, active locks, uncertain evidence and invalid journals stay
blocked. This is evidence that the **original Codex unit** stopped, not that
all spawned administrative work ended.

An operator should inspect external work via its own native lifecycle
(systemd unit, Ansible evidence, deployment receipt, hardware response)
before scheduling potentially conflicting new work. Releasing admission
is not a blanket safety certification.

## Why no container or absolute descendant containment

The cgroup v2 \`cgroup.events populated=0\` bit reports only whether the
original cgroup subtree has processes. It does not prove no process has
moved to another scope; it is not the definition of an agent run's success.
User-systemd bus access and separately managed services are legitimate for
administrative operations. \`KillMode=control-group\`, \`Restart=no\` and
\`InvocationID\` are useful to supervise an invocation and avoid mistaking
a restarted unit for the original, not to establish host-wide quiescence.
\`ProtectControlGroups=\` is not available in per-user systemd units and
must not be assumed as a security mechanism here.

An offline detached-grandchild example demonstrates that a descendant
may remain alive; under the revised contract this is **not** grounds for
declaring the Codex process unfinished. The safety properties are durable
at-most-once execution, identifiable process outcome, and explicit operator
handling of uncertainty—not absolute cgroup isolation.

## PRE-READY criteria and current restrictions

Offline acceptance: normal zero/nonzero return persists Codex terminality
with external work not attested; duplicates never reexecute; accepted journal
and outbox failures stay blocked; timeout/launch uncertainty stay blocked;
explicit operator reconciliation for accepted/unaccepted requests is durable,
idempotent and rejects active/changed units; report dedup and post-crash
receipts work; no claim is made about independent host jobs.

No live Codex, actual issue dispatch, GitHub agent report posting, timer
installation, merge, deployment, GWay Remote change, service/permission/
credential/OAuth change is authorized in this Draft.

**Adopted agent configuration:** `runtime_context()` supplies a private
`CODEX_HOME` referencing the already adopted Codex config and auth files.
AgentOps now invokes `codex exec -` **without** `--sandbox`, `--config`,
or approval-policy overrides. The configuration file and existing account
privileges remain unchanged. A read-only check on gway-001 confirmed both
the adopted `codex-home/config.toml` and the manual
`~/.codex/config.toml` use `sandbox_mode = "danger-full-access"` and
`approval_policy = "never"` with private ownership. The CLI no longer
overrides that known administrative policy with `workspace-write`.
Permissions come from the existing node/account and Codex configuration,
not from a new AgentOps registry. Future config adjustments, if any, remain
outside this PR and require separate authorization.

Additional PRE-READY tasks: verify real deployed versions of CLI and MCP,
inspect pending outbox/journals in read-only mode, coordinate provider timeout
and runtime paths with GWay Remote, and independently authorize any live
validation. The known \`gway-001\` CLI and MCP runtime point at different
release SHAs. Keep this PR Draft pending final acceptance.
