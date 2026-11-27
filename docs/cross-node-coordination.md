# Cross-node admission and MCP provenance (#126)

## Safety and trust boundary

The shared reservation covers AgentOps issue and audit workers, not arbitrary
shell sessions, independent Codex invocations or downstream administrative
jobs. The existing trusted-issue, approval, quota, scope, maintenance and
local capacity gates remain in force. MCP provenance is observational and
does not confer authority.

The dedicated private GitHub repository is an operator-selected dependency,
not any application repository. No new token is introduced. Use the existing
authenticated GitHub client on each enrolled worker. A dedicated state-only
branch `agentops-coordination-v1` is initialized separately. Protect it
against deletion and non-fast-forward updates; permit the authorized
coordinators' fast-forward updates. No CI, deployment, issue-label automation
or external writer may operate on this branch. Do not put it in a public repo.

Root-managed `/etc/gway-mcp-providers/agentops.coordination.json` is identical
on all execution-capable nodes:

```json
{"schema":1,"repository":"arthexis/PRIVATE_COORDINATOR","members":["gway-001","gway-004"],"epoch":"REPLACE_WITH_32_HEX_DIGITS","general_pickup_accepted":false}
```

The member list is illustrative; enumerate every execution-capable AgentOps
node before activation. The state-only branch must contain one file,
`agentops-coordination.json`, with schema 1, the same epoch and ordered member
list, `claims` (object) and `history` (list of 64-hex invocation hashes).
Seed history from all existing canonical accepted/request journals and reconcile
all outstanding work before cutover; do not replay historical invocations,
including #782, #128 and #130. The history limit is 4096; exhaustion blocks
new reservations, never silently discards deduplication history. A reviewed
migration is required before exhaustion. Never reset the epoch or history to
clear a contention error.

## Admission

Before creating a systemd request marker, persist a local private intent
with a random reservation token, then acquire a repository-keyed shared claim.
The global invocation hash uses casefolded issue reference and frozen UTC
identity. A direct CLI/audit worker passes the same boundary before acceptance;
a systemd worker verifies the exact previously acquired claim. Each CAS
candidate is a commit with the exact observed ref head as its parent; only a
non-forced ref update acknowledged by GitHub admits work. On an uncertain
write, the durable local intent permits read-back of the identical claim,
never adoption of a foreign reservation. No time-based release.

After definitive process return with durable terminal report (integer exit
code), the worker may release its exact claim while holding its local
repository lock. Verified systemd stop and explicit direct recovery use
the same exact-owner release. A timeout, crash, missing evidence or ambiguous
release retains the fence. An owner can run `agent coordination-reconcile REF
--at EXACT_UTC --json` for a definitive terminal local claim; this never
launches. A lost response after the reservation but before local launch
requires reconciliation of the original intent, never a new identity or
manual remote deletion. Stale releases must not delete another owner's claim.

## Controlled rollout

Configuration absence is deliberately legacy/manual compatibility; it is
NOT global exclusion. Do not set `general_pickup_accepted` until every
execution-capable node is updated, idle at cutover, identically configured,
and cross-node contention/timeout/replay/release acceptance passes. Do not
enable general pickup during rollout. Validate that old binaries, timers,
or direct entry points cannot bypass the new gate. Verify the dedicated repo
is private, the ref is protected, and both nodes have the existing necessary
access, without generating credentials. A malformed configuration or a
failed API denies a new coordinated admission. Separate, explicit rollout
approval and a protected private state repository are needed.

## Correlation

The existing authenticated MCP node envelope carries a server-generated
32-hex ID into provider JSON-RPC, independent of untrusted tool arguments.
AgentOps retains `submission_trace_id` on requests and
`schedule_trace_id` on scheduled dispatches; `mcp_trace_id` is the current
call's ID. Replaying a schedule/request never rewrites the original origin.
The ID joins the sanitized Watchtower ingress journal; older records have
unknown provenance and are not backfilled. OAuth principal is a pseudonymous
authenticated principal when available, not a ChatGPT conversation identifier.
See the companion GWay Remote change. No credentials, raw arguments or raw
client RPC ID are logged. Existing UTC lifecycle timestamps remain unchanged.
