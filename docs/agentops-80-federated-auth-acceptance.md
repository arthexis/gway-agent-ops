# AgentOps #80 — Federated authorization acceptance proposal (READ-ONLY design)

This is a proposal for a **separate GWay Remote change**. Nothing in this
document enables or modifies Remote's federation. No provider is installed.

## Verified existing read/execute classification

Current `arthexis/gway-remote` main contains:
- `mcp_tool_scope.required_scope`: `readOnlyHint=True` and non-destructive is
  read; missing or false annotations are execute-required.
- `mcp_bearer_gateway.Handler._process_authenticated`: derives `has_exec`
  from the verified bearer, discovers each node-owned route, and rejects an
  execute-classified route with a `gway:execute` challenge **before**
  `federation.call`.
- `mcp_gateway_federation.FederatedProviders.call`: forwards the authorized
  caller scope to the transport.
- `mcp_node_providers.provider_call` and
  `mcp_source_stdio.call_source_tool`: re-check the live tool declaration
  and effective scope before invoking the source.
- AgentOps tools `schedule` / `schedule_cancel` advertise
  `readOnlyHint=False`. `scheduled_list` / `scheduled_get` advertise
  `readOnlyHint=True`.

This establishes a code-level **scope-denial path**, not a live OAuth test or
a per-principal identity binding. Current federation forwards
`authorized_scope` only, not an authenticated principal to the AgentOps
provider.

## Required separate Remote PR before enabling scheduling

1. From the actual OAuth verifier, derive a verified stable principal ID,
   effective scope, and a request identity. Do not trust a principal, scope,
   or digest in client JSON, provider arguments or an untrusted SSH payload.
2. Extend the authenticated pinned federation transport with a versioned,
   transport-bound principal context. The node must authenticate the peer
   and verify the principal binding **before** provider dispatch. Avoid
   treating a plain JSON `FederatedAuthContextV1` as proof: that module is
   currently a pure codec, not an authorization service.
3. AgentOps' scheduled write endpoint must bind a schedule to the trusted
   principal, not merely to `getpass.getuser()`. Cancellation must check the
   authenticated principal and explicit operator privilege against the
   persisted creator/approval provenance. CLI-only owner authority remains
   separate from remotely authenticated authority.
4. Add exact-tool hermetic tests:
   - `gway:read` + `agentops__schedule` => denied before provider invoke;
   - `gway:read` + `agentops__schedule_cancel` => denied before invoke;
   - `gway:read` + `agentops__scheduled_list/get` => read-only accepted;
   - `gway:execute` + scheduled write => permitted past the gateway
     only after verified principal binding;
   - forged principal/scope/arguments, stale catalog, missing node
     attestation, or lost context => denied;
   - no service / node execution in tests.
5. Revalidate both node-owned declaration and Watchtower effective catalog
   on the same pinned release. Ensure old providers remain compatible.

**Gate:** do not enable the optional AgentOps systemd timer, expose new
write tools to remote callers, or mark PR #81 Ready until this separate
contract is accepted and integration-tested.

## SQLite veto linearization on AgentOps

The scheduled issue is durably `claimed` before external I/O. After the
live GitHub preflight, the scheduler holds `BEGIN IMMEDIATE` on the
*same TaskLedger SQLite database used by operator decisions* while
rechecking the exact approval event and invoking the bounded
`nonblocking.submit` launcher. A concurrent operator veto must therefore
either commit **before** the lock (launch denied), or wait until dispatch
admission completes. This is a real SQLite writer serialization boundary,
not merely a second read check.

Tradeoff: GitHub/systemd launcher latency can block veto writes and cause
a busy timeout. The veto must report an explicit lock failure, not success;
operators retry after the short admission window. If the process crashes,
the earlier committed claim still prevents silent relaunch.

This design intentionally does **not** hold the lock for the entire Codex
execution, cannot retract a job already admitted, and does not claim to
stop externally controlled work.
