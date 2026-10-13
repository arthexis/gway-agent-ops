# Contained systemd integration verification

The optional probe uses a **real systemd user manager** but launches only a
short-lived Python sleeper in a uniquely named transient unit. It does not
submit a GitHub issue, invoke Codex, or read agent credentials.

Run as the target unprivileged user on Linux after deployment:

```bash
python3 scripts/systemd_integration_probe.py --json
```

Exit 0 with `result=passed` means a transient unit launched, became active,
was stopped, and was confirmed inactive. Failures exit 1 and include a short
reason. The probe attempts to stop its unit in a `finally` block.

## Remote deployment contract

Gway Remote should deploy this repository through its existing approved
deployment mechanism, then run the probe **on the target host as the agent
user**, collecting its JSON and exit status. No interactive/manual test is
needed when Remote has a working user-systemd session. Do not execute as root,
open a new remote shell interface, or grant arbitrary command execution.

This is a first integration slice. The probe does **not yet** exercise
`agent submit`/`agent release` or a restart/race against the real systemd
unit; those require an isolated injection seam so the test cannot start Codex.
Until those tests exist, this is not full end-to-end admission verification.
