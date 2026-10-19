# Isolated user-systemd verification

This optional probe validates that an unprivileged systemd user manager can launch,
observe and stop a transient dummy service. It never invokes Codex, GitHub, or
Agent Ops task execution. It is **not** end-to-end approval or execution testing.

Run on a Linux host with a working user systemd session:

```sh
python3 scripts/systemd_integration_probe.py --json
```

The result is JSON with `result=passed` or `result=failed`, and exit status
0 or 1. The probe attempts cleanup of its transient unit on failure.

After a reviewed Agent Ops installation, `ansible/verify.yml` can invoke
the probe on the target account. It requires an inventory group
`gway_agent_ops`, a checkout (default `/opt/gway-agent-ops`), and an
existing systemd user bus. The playbook does not deploy Agent Ops and is
not wired to a production deployment workflow.

This verification is independent of Gway Remote's task execution and
does not exercise the new SQLite ledger. Execution-gate and restart
tests belong to subsequent M2 increments.
