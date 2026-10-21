# GWAY Agent Ops

Source archive for local agent/approval helpers that should not remain as
ad-hoc state under the GWAY field workspace.

## GWAY-001 agent issue routing

**Before submitting a field-agent task**, add the exact GitHub issue label
`gway-001`. This marker is required for tasks targeting the agent on GWAY-001,
including submissions via GWAY Remote **Assigned Issues → Send to Agent**.
The label alone never grants authorization: trusted author, open issue,
allowlisted repository, explicit operator dispatch, and execution admission
remain mandatory.

Check the label when creating an agent issue. Unlabeled issues must not be
assumed eligible even when their repository is allowlisted. Server-side
validation and actionable Remote feedback are tracked by
[issue #34](https://github.com/arthexis/gway-agent-ops/issues/34); until
those checks are verified, manually confirm the label before sending.

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

## A3.2 — Timestamp-addressed invocation admission

The future `agent issue REF` command accepts `--at` with an ISO-8601
timestamp containing an explicit timezone. Android should freeze the value
when the operator presses Send and reuse it only for delivery retries.
A deliberate second Send uses a fresh timestamp. No `--resend` or UUID.

`gway_agent_ops.dispatch.admission(issue, at)` provides the building block:
a nonblocking cross-process `flock` single-execution lock and an fsynced,
append-only journal keyed by the pair `(issue, UTC timestamp)`. Duplicates
are rejected even after process restart; another issue with the same
timestamp is a distinct identity. The lock is held for the full context,
so future execution code must keep that context open through Codex
completion. It does not queue work. Timestamps are normalized to UTC, and
values more than five minutes in the future are rejected.

**A3.1 trust checking is not yet integrated.** The CLI remains a deliberate
`not_implemented` stub: no user-provided reference can write dispatch state
or launch Codex. Once trusted issue resolution is wired, the validated
canonical issue key will be supplied to the admission component.

## A3.1 — trusted issue lookup

`gway_agent_ops.github_issues.resolve(REF, allowed_repositories=(...))`
performs read-only GitHub API lookup via the installed `gh` CLI. The caller
must supply an explicit repository allowlist; none is inferred or implicitly
trusted. Only open GitHub **issues**, authored by `arthexis`, in a matching
allowed repository pass validation. Pull requests, closed issues, mismatches
and unavailable GitHub authentication fail closed. Only a canonical issue
object is returned; the untrusted title/body must be treated as task data,
never shell arguments.

A3.1 deliberately does **not** invoke Codex, create a dispatch record, or
route an untrusted issue through `agent issue`. Integration follows once
trusted issue resolution and execution admission are connected. Future
agent execution must use the copied agent-owned authentication environment
rather than passing Android/Remote credentials.

## A3.3 — One-shot Codex execution

`agent issue OWNER/REPO#NUMBER [--at UTC_TIMESTAMP] [--json]` now
resolves and validates the GitHub issue, acquires the A3.2 single-agent
execution lock, and runs one bounded `codex exec` subprocess. Configure an
explicit comma-separated `GWAY_AGENT_ALLOWED_REPOS` and
`GWAY_AGENT_WORKSPACE_ROOT`; existing checkouts must live at
`WORKSPACE_ROOT/OWNER/REPO`. No arbitrary working directory may be supplied
by a remotely submitted issue. Use `agent adopt --include-auth` to prepare
an agent-owned Codex login before starting. Environment variables containing
operator tokens are not passed into Codex.

The subprocess receives issue text on stdin and is given up to 30 minutes by
default. The lock is retained throughout its lifetime; concurrent invocations
return `busy` without queuing. Each fresh operator submission uses a new
UTC timestamp; delivery retries reuse the frozen value. Dispatch identity
is durable across restarts. stdout/stderr from Codex are **never returned to
Remote** because they can contain credentials or unexpected data. Output
contains the exit code and issue body fingerprint; a zero exit code is **not
a claim that the issue was successfully resolved**.

**Important:** GitHub result comments and crash recovery belong to A3.4;
there is no automatic report yet. This A3.3 implementation runs
synchronously from the invoking process; safe detached Remote invocation is
part of the later transport integration. This is not a daemon or scheduler.

## A3.4 — GitHub comments and recovery

Execution now persists a minimal report in a protected, fsynced local
outbox before sending a GitHub issue comment. The report contains only the
canonical issue, frozen dispatch timestamp, process exit code and an
idempotency marker; it never includes Codex stdout, stderr, prompts or
credential contents. GitHub comments report that Codex finished, failed,
or was interrupted, **not** whether the issue was solved.

A failed GitHub request retains the report for later delivery. The next
`agent issue` attempts delivery via the authenticated GitHub CLI. Repeating
a report checks for an existing marker in issue comments before posting,
and keeps a durable local delivered acknowledgment. Interrupted accepted
invocations are reconstructed from the dispatch journal under the
exclusive lock on the next invocation, without rerunning Codex.

This is intentionally best-effort crash recovery: a host that never
executes another Agent Ops command cannot send a pending comment by
itself, because there is no persistent daemon or scheduler. A3.5 will
expose and test read-only reporting/issue projections.

## A3.5 — Issue list projection

`agent issues --json` lists open GitHub issues from the explicit
`GWAY_AGENT_ALLOWED_REPOS` allowlist, filtering for issues created by
`arthexis`. Closed issues, pull requests and other authors are excluded.
GitHub listing failures return an error rather than a misleading partial
success.

The response uses the versioned CLI envelope and an `issues` array with
title, URL, repository, issue number, cumulative comment count, latest send
timestamp, and projection state: `open` (no accepted send), `sent` (no
newer comment than the last accepted send), or `attended` (comment count
greater than that send's stored baseline). A new accepted dispatch resets
the baseline. Older dispatch records missing a comment baseline remain
`sent` rather than guessing that they have been attended.

This projection reflects **comments**, not verified issue completion.
The command is read-only and does not launch Codex or deliver pending
reports. Field validation on GWAY-001 and remote Android integration remain
separate follow-up work.

## S4 — Supported Codex sound notification adoption

Use `agent adopt --with-sound` to add an opt-in, copied root-level
`notify = ["agent", "notify"]` setting to the **agent-owned** Codex
configuration, while preserving the existing allowlisted preferences.
Re-running the same adoption is idempotent; changes require `--force`.

Codex passes one JSON argument to `agent notify`. The adapter handles
only the documented external `agent-turn-complete` notification and
calls `/usr/local/bin/sound --event Stop --source codex`; it does not
forward prompts, output or arbitrary JSON fields. Unsupported types are
ignored, and sound hardware failure is nonfatal. This is independent of
the TUI notification settings. Other lifecycle event names previously
considered (SessionStart, PermissionRequest, etc.) are not assumed to be
supported by Codex's external notify API. Verify the installed Codex
version and event behavior during S5 field validation.

## S5 — GWAY-001 sound field verification

Before checking audio hardware, update `gway-lcd-sound` and `gway-agent-ops`
on GWAY-001 using their supported installation/deployment procedures.
Do not modify the operator's `~/.codex` manually.

1. **Sound mappings:** run `sound --resolve --event Stop --source codex`
   and confirm sound `ok`. Run `sound --resolve --event disconnected
   --source wifi` and confirm `warning`.
2. **Motif sequence:** with sound unmuted, run `sound --event SessionStart
   --source codex`, then `sound --event Stop --source codex` within
   45 seconds. The first should include Codex motif, the second should
   play only its event sound. Switch to `sound --event disconnected
   --source wifi`; then return to Codex to confirm motif reannouncement.
   Repeat Codex after over 45 seconds to confirm expiry.
3. **Adoption:** run `agent adopt --with-sound --verify` only as
   independent commands (`agent adopt --with-sound`, then
   `agent adopt --verify`). Confirm the agent-owned `config.toml`
   contains exactly one `notify = ["agent", "notify"]` and the
   operator config remains unchanged.
4. **Notify adapter:** invoke `agent notify '{"type":"agent-turn-complete"}'`
   and verify the expected sound event. Run an actual Codex turn under
   the agent-owned `CODEX_HOME` and verify Codex invokes notification
   after completion. Observe that unknown types do not play.
5. **Resilience:** verify muted GPIO, missing sound binary, interrupted
   playback, rapid concurrent emitters and existing direct `sound ok`
   remain safe. Collect diagnostic results *without* copying prompts,
   auth files, or user content.

Automated tests cannot prove physical GPIO/audio or a specific installed
Codex version's support for `notify`. Record those observations in
the dedicated S5 field issue before marking this stage complete.
