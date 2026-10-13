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
