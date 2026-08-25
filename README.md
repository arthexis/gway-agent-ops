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
