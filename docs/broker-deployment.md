# Provider deployment broker

Deployment development and unit tests are cloud-first. Use AgentOps development
only if an identified local hardware or exclusive environment dependency cannot
be exercised via remote execution. Deployment and live acceptance alone do not
justify delegating portable development to AgentOps.

For an already authorized node, stage a clean detached full-SHA checkout under
the installation user's `~/.local/share/gway-agent-ops/checkouts/<sha>`.
Use only a reviewed, CI-approved revision. The root publication playbook
`ansible/publish-mcp.yml` accepts `agentops_deployment_checkout` and
`agentops_deployment_revision` together. This registers the fixed
`broker-deploy.sh` from that checkout; it does not grant OAuth execute access.
The existing installed provider revision is independent of the deployment target.
Subsequent publication without a new pin preserves the previous declaration.
Refresh the pin explicitly through root-controlled publication; never fetch
mutable main in the runner. Invalid or dirty checkouts fail closed.
Publication verification restores the previous manifest on failure. Existing
application rollback remains the responsibility of the provider installer.
Do not remove a checkout referenced by the manifest or an active deployment.
