# Watchtower reservation mailbox

Replaces PR #131's GitHub coordination backend. No credential or GitHub
coordination write. AgentOps keeps local owner-private SQLite intent state;
Watchtower is the only global reservation authority and uses its existing
pinned relay to fetch/apply bounded data. See GWay Remote #786.

Before any local systemd launch or direct/audit acceptance, the same
repository/issue/frozen UTC identity must have an exact acknowledged grant.
A first call without a grant is safely busy; the one-shot scheduler's
existing bounded busy retry preserves the original timestamp. Direct
operators must retry using the same explicit --at identity, never create
a replacement merely because a grant is pending. No autonomous execution
is performed by the mailbox or Watchtower poller.

After definitive terminal evidence, the worker records a terminal intent
under the local gate; central release is later acknowledged through the
pinned transport. A crash/timeout or missing evidence retains the fence.
The owner can use coordination-reconcile for a settled or definitively
never-launched granted identity; this records a release intent, not an
assertion that Watchtower has acknowledged it. The existing verified
systemd recovery paths are preserved. No TTL or automatic expiration.

Root-owned /etc/gway-mcp-providers/agentops.coordination.json has schema 1,
epoch (32 lowercase hex), members (exact enrolled nodes), and
general_pickup_accepted (false until separately accepted). Absence means
legacy compatibility only before cutover. Removing config with an existing
mailbox blocks admission. Enable only after all execution-capable nodes
have the new receiver/provider/CLI, workers and schedules reconciled, a
seeded central ledger and independent acceptance. Do not enable general
pickup in this PR.

A separately provisioned root-owned agentops.coordination-canary.json
with a different epoch and false acceptance uses the fixed private
coordination-canary mailbox, never an actual dispatch path. This permits
two-node transport acceptance without Codex or production reservations.
