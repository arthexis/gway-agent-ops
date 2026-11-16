# AgentOps #80 — Existing federated authorization contract

## Decision: keep the existing simplified security model

No additional OAuth principal envelope, `FederatedAuthContextV1` integration,
per-user cancellation ownership, SSH identity, or secondary authorization service
is required for one-shot scheduling. GWay Remote / Watchtower is the OAuth
authority. AgentOps runs as its installation user, retains its own issue
approval and dispatch state, and does not infer remote human identities.

This is the contract **already in use** for other federated AgentOps writes,
including `submit`. Adding a second principal policy only to deferred writes
would diverge from that model without a stated product requirement.

## Enforced boundaries

1. Watchtower verifies the bearer and requires `gway:execute` for
   `schedule` and `schedule_cancel`, just as for `submit`.
2. The pinned, authenticated federation transport passes the authorized scope,
   not a caller-supplied scope. The node rechecks the current registered tool
   declaration before calling the stdio provider. Write tools advertise
   `readOnlyHint=False`; `scheduled_list`/`scheduled_get` remain reads.
3. The AgentOps scheduler requires existing task approval, a trusted open
   GitHub issue, one frozen submission identity, durable SQLite claim, bounded
   admission, and revalidation at dispatch. Scheduling alone never grants a
   deploy permission or supersedes an issue's restrictions.
4. The scheduler service/timer remains opt-in and **off** until separately
   accepted. Never turn read-only discovery into an executor.
5. Cancellation is an administrative `gway:execute` operation before claim;
   it is not a promise to terminate running Codex. No per-human owner model
   is introduced.
6. Old `submit`, `preview`, `observe`, report receipts, CLI local operations
   and recovery retain their existing authorization boundaries.

## Acceptance limited to existing functionality

- Hermetic gateway tests: read-only bearer is denied for `schedule` and
  `schedule_cancel` before federation invocation; execute bearer reaches the
  authorized route; live declaration changes and untrusted scope injection
  fail closed. No live bearer or node calls in these tests.
- Hermetic provider tests: scheduler MCP writes route to existing
  `scheduler.create` / `scheduler.cancel` without fabricated principal;
  strict argument schemas and read-only scheduled queries remain intact.
- Scheduler tests: durable single claim, timezones, cancel-vs-claim and
  approve/veto races, busy executor and uncertain launch; no implicit retries.
- Before Ready: validate current HEAD CI, main drift, review threads, and
  actual deployment/health behavior separately. No deployment or timer
  activation is authorized by this document.

`FederatedAuthContextV1` remains an independent pure contract in
`arthexis/gway-remote`, not a prerequisite for AgentOps #80.
