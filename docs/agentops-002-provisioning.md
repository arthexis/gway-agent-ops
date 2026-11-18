# gway-002: opt-in AgentOps provider preparation

This is a **code-only preparation path**, not approval to change the production
node. The operator must first verify gway-002 locally, including SSH forced
command, existing WireGuard enrollment, attestation, installation identity,
Codex authentication and the exact AgentOps release. Watchtower currently
reports the node as enrolled and reachable, but a real node_exec request was
rejected; preview alone is not operational evidence.

## Current ownership and separation

- `gway-002` stays in the **production** Ansible group of
  `arthexis/gway-remote`. Its `remote_managed_repositories: []` remains
  unchanged, denying generic deployments.
- AgentOps publishes its **own** immutable provider manifest and the
  separate secret-free runtime policy. Do not introduce a gateway-local
  AgentOps process, a second broker or a scheduler.
- Authentication and Codex configuration are adopted locally by `arthe`
  from **gway-002** alone. Never copy credentials from 001 or Watchtower.
- The playbook does not install releases, create a workspace, start Codex,
  enable timers, restart services, edit SSH or WireGuard, or grant deploy rights.

## Operator-only preparation

Run on gway-002 **as arthe**, from a locally reviewed pinned AgentOps checkout,
after ensuring the installation already exists. Supply the installed immutable
AgentOps release SHA and the separately verified absolute local Codex binary.

```sh
ansible-playbook -i localhost, -c local ansible/provision-agentops-002.yml \
  -e agentops_002_revision=<installed-40-hex-sha> \
  -e agentops_002_codex_executable=<verified-absolute-path-to-codex>
```

The default performs preflight only. It fails closed if the local hostname,
private workspace, local adopted Codex credentials, release executables
or existing policy files are missing or unsafe. It also reads the existing
root-controlled GWay Remote executor identity without changing that policy.
The apply path **refuses** publication unless the existing node executor is
`arthe`; an existing `gway-exec` identity requires an independently reviewed
handoff/cutover, not implicit migration. The playbook does not display
authentication contents.

After separately authorizing the production-node policy publication, repeat
with `-e '{"agentops_002_apply": true, "agentops_002_approval": "I_ACCEPT_GWAY_002_AGENTOPS_PROVIDER"}'`.
The JSON extra-vars form is required to preserve the boolean type.
The only writes are two root-owned files under
`/etc/gway-mcp-providers/`: `agentops.json` and `agentops.runtime.json`.
**Security boundary:** publishing `agentops.json` immediately registers an
enabled provider for local GWay Remote discovery. The next authenticated
provider discovery or gateway catalog refresh can expose its tools without
a separate enable step. Thus applying this playbook **is an activation of
provider registration** and needs explicit authorization; it is not proof
that SSH federation, Codex or an actual job is operationally accepted.
The additional `agentops.runtime.json` is secret-free metadata excluded from
MCP manifest discovery; it does not grant independent permissions.

## Independent field acceptance (not performed by this change)

1. Confirm gway-002's pin/attestation and SSH forced-command receiver are
   functioning through existing approved paths; repair the rejection without
   granting broad shell/deployment rights.
2. Confirm release SHA, owner-local Codex/GitHub login, private workspace and
   node-owned admission ledger. Adopt on 002 only if separately authorized.
3. Refresh Watchtower federation and verify unique `gway_002_agentops__*`
   tools bound to **002**. Existing unprefixed `agentops__*` must remain 001
   and `gway_004_agentops__*` must remain 004. No ambiguous source binding.
4. Test read-only status/preview, denied write under read-only OAuth, and
   `gway:execute`-gated submission. Only after that authorize a single benign
   issue with a frozen UTC identity; observe the 002 ledger and report.
5. Keep scheduler timers off and generic deployment allowlists empty.

CI and syntax checks validate a **playbook contract only**. They cannot prove
a live WireGuard handshake, SSH transport, local identity, credentials, provider
discovery or successful Codex execution.
