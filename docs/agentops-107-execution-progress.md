# AgentOps execution progress (#107)

AgentOps records **runner-only** progress alongside its existing durable
acceptance/outbox ledgers. It does not launch another model, poll Codex, send
GitHub comments, read command output, or change scopes.

## Read-only contract

The existing MCP `agentops__observe({ref, at})` reports the same state as
before. When an accepted identity has a valid, private local progress record,
an additional `progress` object is included. The same object appears on the
matching `admission.reservations[]` entry in `agentops__status`. No new tool
or approval step is required.

An example (illustrative, not actual live data):

```json
{
  "state": "accepted",
  "progress": {
    "phase": "executing",
    "phase_source": "runner",
    "functional_phase": "unknown",
    "heartbeat_at": "2026-11-20T16:50:00Z",
    "phase_changed_at": "2026-11-20T16:42:00Z",
    "last_progress_at": "2026-11-20T16:42:00Z",
    "elapsed_seconds": 480,
    "heartbeat_age_seconds": 12,
    "freshness": "fresh",
    "summary": "Runner waiting for the Codex invocation"
  }
}
```

**Interpretation:** `preparing` means the accepted runner is preparing,
`executing` means it entered the existing blocking Codex invocation,
`finalizing` means the immediate Codex child returned and the runner is
persisting the terminal report, and `uncertain` means a timeout/spawn
failure without terminal evidence. The recorder emits one heartbeat every
60 seconds while its Python supervisor is running.

This heartbeat **does not** verify Codex progress, process descendants,
external host effects or completion. No percentage/ETA is claimed.
`functional_phase` remains `unknown` until a future separately validated
source of task-level events is introduced. `last_progress_at` is the last
**runner phase transition**, not a claim of task advancement. Terminal
states and outbox receipts always take precedence over telemetry. Readers
of old invocations receive no `progress` key. A stale or invalid snapshot
never grants capacity or marks an invocation terminal.

## Safety and persistence

Snapshot filename is SHA-256 of the existing `issue + "\\n" + at`
identity in a mode-0700 `progress/` directory, with mode-0600 atomic
files. The snapshot schema has fixed keys, bounded bytes and enum values;
no prompts, raw output, logs, tokens, arguments or personal data are
persisted. Readers check owner, symlinks, size, schema, identity and UTC
timestamps and never mutate disk. An invalid snapshot reports
`freshness=invalid`; a missing snapshot is omitted. A heartbeat more than
150 seconds old is `stale` (not proof that the worker has stopped).
Disk-write failures disable optional telemetry but never abort an
already-authorized invocation.

The CLI/MCP contracts and authorization are unchanged. Snapshots are removed only after a durable terminal report or an explicit
administrative release. Interrupted/uncertain identities retain their single
identity-bound snapshot for diagnosis until reconciled. Active files are never
pruned by age or arbitrary quotas. This PR makes no deployments, changes to timers or
Android/UI surfaces.
