# AgentOps development and deployment

- For appliance installation, follow the canonical README deployment contract.
- Verify a clean checkout of the exact approved SHA and idle execution admission.
- Invoke `sh ./deploy.sh --expected-sha "$APPROVED_SHA"` as the installation owner.
- Never add `--apply`, `--dry-run`, or `--json` to installation: these are prune-only. Installation emits JSON by default. Never prune as part of deployment.
- The wrapper performs Ansible provider publication; verify installed CLI and manifest SHA and preserve existing schedules.
- Prefer cloud development when no node-specific dependency exists; use node execution for hardware, isolated target validation and authorized deployment. Do not delegate to AgentOps unless required and authorized.
