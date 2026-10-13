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

The probe exercises real cross-process controller admission and terminal-evidence\nreopening with temporary state. It does **not yet** exercise the production\n`agent submit`/`agent release` against a real systemd unit, or a real process\ncrash/restart race; those require an isolated execution seam that cannot launch\nCodex. These gaps must not be represented as passing checks.
Until those tests exist, this is not full end-to-end admission verification.

## Ansible verification after an approved deployment

`ansible/verify.yml` is the post-install verification entrypoint. The
approved Gway Remote deployment should call it only after installing a pinned,
reviewed Agent Ops revision. The playbook does not perform installation itself.

Inventory must contain a `gway_agent_ops` host group. The default checkout is
`/opt/gway-agent-ops` and the default account is `arthe`; override both
using inventory variables if necessary. The target account must have a running
systemd user manager and its runtime bus at `/run/user/<uid>/bus`.

```sh
ansible-playbook -i INVENTORY ansible/verify.yml
```

The playbook resolves the target UID, switches to that unprivileged user,
runs the probe with `--json`, and fails the deployment verification on any
nonzero exit or non-passing JSON result. It is read-only with respect to the
installed Agent Ops revision; the only runtime changes are isolated transient
test units and temporary controller state.

**Integration not yet wired:** Gway Remote must explicitly invoke this
playbook in its approved deployment workflow and record the result. Do not
claim automatic deployment until that Remote change is merged and exercised.
