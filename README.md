# GWAY Agent Ops

Source archive for local agent/approval helpers that should not remain as
ad-hoc state under the GWAY field workspace.

## Canonical managed deployment (issue #306)

On the appliance, use the owner-managed `deploy.sh` with a pinned checkout:

```sh
sh ./deploy.sh --expected-sha "$(git rev-parse HEAD)"
```

This installs an immutable `~/.local/share/gway-agent-ops/releases/<SHA>`
environment and atomically updates `~/.local/bin/agent`. The installer holds
a nonblocking owner lock, independently verifies the installed `agent` CLI,
records successful revisions, maintains `current`/`previous` links, and
restores the previous executable link if installed verification fails.
The installer never adopts credentials, executes Codex, or deletes releases.
Private state remains outside the immutable release directory.

`gway-remote` invokes this canonical script, not a separate Agent Ops
installer. Do not invoke the wrapper as root. **Pruning remains disabled.**

## GWAY-001 agent issue routing

Any open, trusted-author GitHub issue in an accessible repository may be submitted
without a routing label. Labels remain optional task-policy hints (for example,
`agentops:implementation`); they do **not** authorize execution. Operator action,
trusted author, current GitHub issue validation, workspace checks, and single-slot
idempotent admission remain mandatory. The separate `local-agent` discovery opt-in
is unchanged: discovery does not dispatch automatically.

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

`gway_agent_ops.github_issues.resolve(REF)` performs read-only GitHub API
lookup via the installed `gh` CLI against any repository already accessible
to that identity. No repository allowlist is required. Only open GitHub
**issues** authored by `arthexis` pass validation. Pull requests, closed issues, mismatches
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
execution lock, and runs one bounded `codex exec` subprocess. Configure
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

`agent issues --json` uses bounded GitHub search to list open issues
created by `arthexis` across accessible repositories, regardless of labels.
Closed issues, pull requests and other authors are excluded.
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

### Immutable release cleanup

The release owner can inspect prior releases without changing services or persistent data:

```sh
./deploy.sh --prune
./deploy.sh --prune --json
./deploy.sh --prune --apply
```

Dry-run is the default; `--apply` is an explicit destructive operation. Only owner-managed
immutable release directories are candidates; `current`, the available `previous` rollback
pointer, and installed CLI references are preserved. Unknown names, unsafe symlinks,
lock contention and excessive candidate counts block cleanup. Apply uses the same
deployment lock and rechecks each candidate before removal. No database, credentials,
shared dependencies or system services are removed, and deployment never prunes automatically.

## Node-owned MCP provider discovery

The immutable release now installs `bin/agent-mcp`, exposing the read-only MCP
stdio provider. After deploying an approved release, the node's administrative
Ansible convergence publishes a root-owned `/etc/gway-mcp-providers/agentops.json`
manifest with the release SHA and executable path. The presence of that manifest
in the installation directory is sufficient for automatic discovery: no manual
approval, tool catalog maintenance, or Watchtower configuration is required.

For diagnostics, run `python3 -m gway_agent_ops.mcp_manifest --release
~/.local/share/gway-agent-ops/releases/<SHA>` against the verified release.
A repository checkout alone does not register a provider.

## Repository-agnostic AgentOps issue handling (#75; pending acceptance)

AgentOps can preview and request issues from **any GitHub repository accessible to
its existing authenticated `gh` identity**, without enrolling each repository.
The old `GWAY_AGENT_ALLOWED_REPOS` setting is retained as a compatibility-only
configuration key and **ignored for issue admission**. This does not grant
new GitHub permissions or make a repository a deployment target. The
`gway-remote deploy-targets` registry stays independent and restricted.

An issue is trusted only when it is a real open GitHub **issue** (not a PR)
with canonical identity, created by `arthexis`, and is explicitly routed with
the exact `gway-001` label for execution. Preview does no execution.
`submit` requires an authorized operator call with `gway:execute`, fresh
revalidation, a frozen UTC identity, and the existing one-at-a-time durable
admission. `READ-ONLY DEFAULT` remains active. Neither repository access nor
an issue body grants permissions to write or deploy; task-level authorization,
existing Codex sandbox and approval settings still apply.

Execution/audit require a pre-existing checkout under the private
`GWAY_AGENT_WORKSPACE_ROOT/OWNER/REPO`, never an arbitrary path from an
issue body. There is deliberately **no implicit clone** or credential
installation: a missing or unsafe checkout fails closed and must be provisioned
separately through approved workspace setup. Preview works without checkout.

The MCP/CLI issue list performs bounded GitHub-wide search for
`gway-001`-labeled issues from `arthexis`; the independent discovery loop
uses `local-agent`. The search is at most 3 pages of 100 issues, and rejects
incomplete results instead of representing a partial list as complete. A
search result never authorizes execution; `submit` re-fetches the issue.

Acceptance target: `agentops__preview` for
`arthexis/ocpp-reports#211` succeeds with no allowlist. This code change
does **not** run that issue, deploy Reports, change authentication or reach
production. PR Ready/merge and AgentOps runtime deployment need separate
approval.


## Deferred one-shot scheduling (issue #80, Draft / opt-in)

This feature is **not enabled automatically** by installation or issue discovery.
Scheduling persists a one-shot job in the owner-private AgentOps SQLite ledger
(`GWAY_AGENT_LEDGER_PATH` or `~/.local/state/gway-agent-ops/tasks.sqlite3`).
It does not invoke Codex when a schedule is created.

```sh
agent schedule arthexis/ocpp-csms#355 --run-at 2026-11-20T16:00:00-06:00 --json
agent schedule-list --json
agent schedule-show SCHEDULE_ID --json
agent schedule-cancel SCHEDULE_ID --json
# Explicit opt-in scheduler pass only (not run by discovery):
agent schedule-tick --json
```

The dedicated MCP tools are `schedule` and `schedule_cancel` (execute scope),
and `scheduled_list` and `scheduled_get` (read scope).
The caller must enforce `gway:execute` for write tools. AgentOps keeps the
existing trusted issue and approval gates (routing labels are optional); a schedule does not grant any
production deploy or privileged host authorization. Only owner-managed
`gway-001` is an initial eligible controller.

Only timezone-qualified ISO-8601 instants within 90 days are accepted.
`--run-at` specifies **not-before** scheduling, distinct from the frozen
dispatch identity `--at`. Actual start may be later if unavailable.
At due time, a scheduler tick records a durable `claimed` state **before**
any external call, revalidates live issue trust, and submits through the
existing one-shot nonblocking dispatcher. An interrupted claim, revoked
trust, busy slot or uncertain launch is **not automatically retried**; inspect
receipts before scheduling a new invocation. Cancellation only applies
before claim and cannot stop Codex after dispatch.

**Deployment gate:** Do not enable `schedule-tick` automatically from the
read-only `discover-worker`. A separately approved, AgentOps-owned user timer
or service must explicitly invoke it after field validation; the canonical installer stages the user unit files but does **not** reload,
enable or start them. No autonomous time-based execution occurs until the
operator deliberately activates the timer.


### Optional owner user timer (installed as files, disabled by default)

The repository contains systemd user units in
`packaging/systemd/gway-agent-ops-scheduler.service` and
`packaging/systemd/gway-agent-ops-scheduler.timer`. They run the dedicated
`agent schedule-tick` every minute, separately from read-only discovery.
The canonical installer stages these unit files idempotently in the same
installation user's `~/.config/systemd/user/`, but **never** reloads systemd,
enables or starts the timer. After separate approval and verification on the
owner node, an operator may reload the user manager and explicitly enable
the timer. Do not perform
this activation on Watchtower or satellites. This Draft changes no running
services.

For transport retries, preserve the exact frozen `--at` identity supplied
at the first schedule request. A deliberate new scheduling action uses a
new timestamp. The scheduler does not resubmit ambiguous/claimed work.


### Scheduling authorization and recovery (issue #80)

Scheduling is denied unless the existing private `TaskLedger` contains the
canonical issue in state `approved` with a durable operator `approve` event.
The event ID is bound to the schedule. Both due-time claim and immediate
pre-dispatch checks require that *same* approval generation; a veto,
withdrawal, or approval after revocation cannot revive an old schedule.
The normal GitHub open/trusted-creator and `gway-001` label gates still
apply. Scheduling does **not** authorize an external production deployment.

Use the canonical `GWAY_AGENT_LEDGER_PATH` when configured. Schedule list/get
open the ledger read-only and do not create files, run DDL, or change its
mode; schedule creation and scheduler ticks own schema migrations.
The scheduler can defer a definitive `busy` response for two minutes, at most
six times. It never retries unknown/ambiguous launches or claims left
unresolved by a crash. Operator reconciliation remains necessary.

**Federated OAuth admission:** the MCP write-tool annotations are only
classification metadata, **not an authorization check by themselves**.
GWay Remote must demonstrate denial of `schedule`/`schedule_cancel`
with `gway:read` and dispatch only with `gway:execute`, at its provider
authorization boundary, before rollout. The systemd timer remains opt-in;
do not enable it until this check and owner runtime environment are verified.

## Local execution history

Use `agent history` to list accepted local Agent Ops executions, newest first.
`agent history --limit 3` limits the results; `agent history --limit 3 --json`
returns a versioned JSON envelope. This reads the local dispatch and report
journals without contacting GitHub or starting work. The recorded process state
and report delivery do not establish that the issue was resolved. This is
separate from `agent tasks`, cloud executions and other nodes.


## Recurring issue schedules (issue #93; operator opt-in)

This extends the existing one-shot scheduler without changing its
`schedule`/`schedule-tick` contract. A recurring definition is **paused by
default** and can do no work until the installation operator explicitly
activates it. Existing installations do not acquire active recurring work on
upgrade. This feature is code-only until a separately accepted deployment.

```sh
# Fixed issue, hourly while open (bounded by --max-runs and no-progress limit):
agent schedule-recurring arthexis/ocpp-csms#346 --every 1h --until closed --max-runs 20 --json

# Repository-scoped discovery of future matching issues:
agent schedule-recurring arthexis/ocpp-csms --title-prefix '[auto-fix]' --every 6h --until closed --json
# Conservative, anchored regex without groups/alternation:
agent schedule-recurring arthexis/ocpp-csms --title-regex '^\[auto-fix\]\s+' --every 1h --until closed --json

agent recurring-list --json
agent recurring-show DEFINITION_ID --json
agent recurring-resume DEFINITION_ID --json  # explicit opt-in only
agent recurring-pause DEFINITION_ID --json
agent recurring-cancel DEFINITION_ID --json
```

MCP equivalents are `recurring_create`, `recurring_list`,
`recurring_get`, `recurring_pause`, `recurring_resume` and
`recurring_cancel`. The gateway must enforce `gway:execute` for creation,
pause, resume and cancel, and may serve list/get with `gway:read`.
The provider's `readOnlyHint` is classification metadata, not an OAuth
authorization boundary. No bearer token or request scope is obtained from
the issue body.

**Authorization and lifecycle:** A recurring definition never creates
per-issue operator approvals. Every matching issue still needs its own
existing `approved` task-ledger state, current operator approval event,
fresh trusted GitHub identity and an available, non-exhausted current Codex
budget before an invocation is claimed. New matches from a selector are
not implicitly authorized. A fixed definition completes when its issue is
closed; a title-selector definition keeps discovering until explicitly
cancelled. Closed selector matches are completed and do not auto-restart
on reopening. A completed fixed issue can only be rearmed by an explicit
operator `recurring-resume` after reopening.

Each active tick enumerates bounded, fully fetched issue pages (maximum 3),
at most 50 candidate issues per definition, 8 definitions, and 2 requests
per tick. GitHub outages, ambiguous pagination, missing/expired budgets or
revoked approval do not authorize dispatch. Every successful request has
a unique frozen UTC identity, a durable claim and an event trail in the
private existing SQLite ledger; no second dispatch is attempted while the
prior identity is unresolved. A definitive `busy` response is deferred
using that **same** frozen identity. A claim lost to a crash, an uncertain
launch or mismatched identity remains blocked for operator reconciliation:
it is never automatically retried with a new timestamp.

Terminal result processing requires delivered findings before another
successful Codex invocation may be scheduled. Per-issue max runs
(default 100) and consecutive unchanged issue-title/body limit
(default 3) pause the issue to avoid unbounded no-progress loops.
A Codex exit code 0 does **not** prove host changes, successful deployment
or issue resolution. No issue is auto-closed.

**Deployment safety:** The activated `gway-agent-ops-scheduler.timer`
continues using `agent schedule-tick`. It can evaluate recurring policies
only after the approved code is deployed and definitions are explicitly
resumed. Do not activate new recurring definitions on a production node
without separate operator authorization. Restart/crash-safety tests and
local CLI/MCP tests do not certify a live GitHub + Codex recurring run.
