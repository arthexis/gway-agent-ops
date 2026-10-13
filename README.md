# GWAY Agent Ops

Source archive for local agent/approval helpers that should not remain as
ad-hoc state under the GWAY field workspace.

## Contents

- `scripts/gway/agent_alert.py`: file/Twilio-backed urgent attention bridge.
- `bin/agent-alert`: repo-local launcher for the agent alert script.
- `scripts/gway/operator-log.sh`: shared operator event logging helper.
- `AGENT_ALERTS.md`: original agent-alert usage notes.

Runtime request, response, outbox, and log directories are intentionally not
tracked.

## Validation

```bash
PYTHONPATH=. python3 -m pytest tests
python3 -m py_compile scripts/gway/agent_alert.py tests/*.py
bash -n scripts/gway/operator-log.sh
git diff --check
```

## On-demand agent CLI (A1 foundation)

Install locally with `python3 -m pip install .` (prefer an isolated environment),
then run `agent --help` or `agent issue --help`.

Initial command grammar: `agent issue OWNER/REPO#NUMBER` (or `NUMBER --repo OWNER/REPO`),
`agent issues --json`, `agent status --json`, `agent adopt --check`,
and `agent hook EVENT`. Execution, account adoption, sound dispatch, and JSON
responses are **not yet implemented**: these commands return exit code 3
and print a clear diagnostic. They do not start Codex or read credentials.
Malformed inputs return argparse exit code 2. Existing `bin/agent-alert`
and operator-log scripts remain unchanged.

A1 does not add a daemon, queue, polling or priority-based budget admission.

## A1 JSON protocol (version 1)

`agent issue REF --json`, `agent issues --json`, `agent status --json`,
`agent adopt --json` and `agent hook EVENT --json` emit one JSON response
envelope on stdout with keys `schema_version`, `command`, `status`,
`data`, and `error`. Command implementations are not available until
subsequent chunks: exit code 3 and `status=not_implemented` are intentional.
No fake issue list or Codex quota data is returned.

The versioned data contracts in `gway_agent_ops.contracts` reserve
`budget.five_hour` and `budget.weekly` independently, each containing
`remaining_percent`, `resets_at`, `source`, `observed_at` and `available`.
Unavailable usage is represented as `null`, not zero. Issue rows include
`repository`, `number`, `title`, canonical `url`, `state`
(`open`, `sent`, or `attended`), cumulative `comments`, and `sent_at`.
Agent Ops will own actual providers and issue retrieval in later chunks.

## A2.1: read-only adoption inspection

Run `agent adopt --check` (or `agent adopt --check --json`) to inspect
Codex executable availability, `CODEX_HOME` / `~/.codex` configuration
and auth-file **metadata**, and API-key presence without reading or printing
secrets. This command never provisions identity or copies files; regular
`agent adopt` still fails explicitly until A2.2. Adoption will create a
separate agent-owned configuration copy, not a live link.

## A2.2: copy-based provisioning

Run `agent adopt` to copy a small allowlist of Codex settings from
`CODEX_HOME` (default `~/.codex`) into
`~/.local/share/gway-agent-ops/codex-home`. No symlinks are installed.
Supported scalar settings are `model`, `model_reasoning_effort`,
`approval_policy`, and `sandbox_mode`; arbitrary hooks, MCP servers,
trust rules and external commands are deliberately excluded.

Authentication is **not** copied by default. Use `agent adopt --include-auth`
to copy an existing owner-only `auth.json` as a separate protected snapshot.
No environment API key is persisted. Identical files are left intact;
different existing files require explicit `--force`. Run
`agent adopt --check` for metadata-only inspection. Never run adoption
as root to provision another user's credentials.

## A2.3: future runtime environment

`gway_agent_ops.runtime.runtime_context()` validates the adopted Codex
configuration directory and prepares an environment for future `agent issue`
execution. It requires private owner-owned directories and configuration,
rejects symlinked authentication, and passes only a short allowlist of
noncredential environment variables while setting `CODEX_HOME` to the
**copied agent configuration**. It performs no subprocess launch, network
access or automatic identity provisioning.

This is an environment boundary, **not an operating-system sandbox**:
invocations retain the permissions of the Linux account launching them.
The CLI/adopter should be invoked under the intended GWAY-001 user; Remote
must not receive credential files or inherit arbitrary command execution.
A2.4 will validate this environment end-to-end before Codex execution lands.

## A2.4: verify the copied agent runtime

Run `agent adopt --verify` or `agent adopt --verify --json` to check the
copied Codex configuration and optional credential file **without running
Codex, reading usage budgets, or calling GitHub**. This verifies private
permissions, parses the copied TOML and checks the JSON structure of existing
authentication. Auth values and detailed parsing failures never appear in
output. A missing credential is reported independently from configuration
readiness: some installations may use another supported authentication
mechanism in a later integration. Exit 0 means the copied configuration is
structurally ready; exit 1 indicates a missing/unsafe/invalid configuration.

Atomic adoption commits remain authoritative even if a previous process left
an incomplete `.agent-adopt-*.tmp` file. Verification ignores such temporary
files and never deletes them automatically.
