# Read-only Agent Ops audit receipts

The operator must explicitly invoke an audit; a normal `agent issue` remains
a writable code-work command and does **not** produce an audit receipt.

Example (only after separate authorization for the named target):

```sh
agent audit arthexis/example#19 --scope T1-T4 --at 2026-10-25T18:00:00Z --json
agent observe arthexis/example#19 --at 2026-10-25T18:00:00Z --json
agent report --json
```

The dispatch issue must be trusted, allowlisted, and labeled exactly `gway-001`;
the checkout must be clean before the audit begins. An audit uses `codex exec
--sandbox read-only` and checks Git status again after execution. Any
modification invalidates the audit result; Agent Ops never commits or deploys
those changes automatically. Read-only Codex sandboxing is the invocation guard,
not a replacement for system-level verification of production permissions.

Each accepted audit attempt has a unique issue-and-UTC-timestamp identity.
The model must return a single JSON receipt with exactly `version=1`,
`issue`, `at`, `scope`, `checks` (T1/T2/T3/T4 with values
PASS/BLOCKED/UNKNOWN) and a short plain-text `summary`.
Unknown checks must remain UNKNOWN. Unrecognized fields, oversized data,
private network addresses, markup, and secret-shaped content are rejected.
Only a validated envelope is retained in the private outbox; prompts, raw
stdout/stderr and source diffs are not journaled or posted.

Observation separates `state` and `execution_terminal` from
`receipt_state`, `receipt_retained`, `delivery_attempted`,
and `delivery_confirmed`. Exit code zero without a valid receipt is
`receipt_missing` or `receipt_invalid`, not audit PASS. Running
`agent report` retries only durable report comments; it never reruns Codex.
Receipt posts use the existing deterministic marker to avoid duplicate posts.

**Reconciliation:** read `agent observe ... --json`. If execution is
terminal but receipt absent/invalid, do not treat it as a passing audit.
If the receipt is retained but delivery unconfirmed, run `agent report`
and observe again. A failed delivery keeps the outbox record for later
retry. A new execution, even for the same target, requires new explicit
authorization and a new timestamp. In particular this work does **not**
authorize rerunning gway-remote#258 or deploying anything on GWAY-001.
