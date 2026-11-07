#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$(id -u)" -eq 0 ]; then
  echo 'Run deploy.sh as the intended non-root Agent Ops owner.' >&2
  exit 1
fi
# Invoke owner source explicitly; never import an older installed Agent Ops copy.
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -m gway_agent_ops.release_deploy --source "$ROOT" "$@"
revision=$(git -C "$ROOT" rev-parse --verify HEAD)
# Root-controlled atomic publication; never use mutable symlink paths in the manifest.
sudo -n ansible-playbook -i localhost, -c local "$ROOT/ansible/publish-mcp.yml" -e "agentops_revision=$revision"
