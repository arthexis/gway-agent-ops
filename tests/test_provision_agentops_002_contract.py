"""Hermetic contract checks for the gway-002 opt-in provider playbook.

These tests never execute Ansible against a host, and never read credentials.
"""
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "ansible/provision-agentops-002.yml"


def test_002_policy_requires_local_owner_pinned_sha_and_explicit_apply():
    source = PLAYBOOK.read_text(encoding="utf-8")
    assert "hosts: localhost" in source
    assert "connection: local" in source
    assert "lookup('pipe', 'hostname -s') == 'gway-002'" in source
    assert "lookup('pipe', 'id -un') == agentops_002_owner" in source
    assert "agentops_002_revision is match('^[0-9a-f]{40}$')" in source
    assert "agentops_002_apply: false" in source
    assert "agentops_002_apply | type_debug == 'bool'" in source
    assert "I_ACCEPT_GWAY_002_AGENTOPS_PROVIDER" in source
    assert "when: agentops_002_apply | bool" in source


def test_002_provider_writes_only_two_root_owned_files_not_services_or_credentials():
    source = PLAYBOOK.read_text(encoding="utf-8")
    assert source.count("ansible.builtin.copy:") == 2
    assert "agentops_002_manifest: /etc/gway-mcp-providers/agentops.json" in source
    assert "agentops_002_runtime_policy: /etc/gway-mcp-providers/agentops.runtime.json" in source
    assert source.count("owner: root") == 2
    assert source.count("mode: '0644'") == 2
    assert "agentops_002_prerequisites" in source
    assert "follow: false" in source
    assert "remote_managed_repositories" not in source
    assert "systemd" not in source.lower()
    assert not re.search(r"ansible\\.builtin\\.(?:shell|service|systemd|git|apt|user):", source)


def test_002_ansible_syntax_when_ansible_available():
    executable = shutil.which("ansible-playbook")
    if executable is None:
        pytest.skip("ansible-playbook not installed; field/CI syntax gate remains")
    result = subprocess.run(
        [executable, "--syntax-check", "-i", "localhost,", str(PLAYBOOK)],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr

def test_002_provider_rejects_identity_mismatch_before_manifest_write():
    source = PLAYBOOK.read_text(encoding="utf-8")
    identity_read = source.index("Read the installed node executor identity")
    deny = source.index("Reject provider publication under a different node executor identity")
    publish = source.index("Publish only the pinned node-owned AgentOps policies")
    assert identity_read < deny < publish
    assert "from gway_remote.node_exec_identity import selected_user" in source
    assert "agentops_002_executor_identity.stdout == agentops_002_owner" in source
    assert "when: agentops_002_apply | bool" in source
    assert "changed_when: false" in source
    documentation = (ROOT / "docs/agentops-002-provisioning.md").read_text(encoding="utf-8")
    assert "activation of" in documentation
    assert "next authenticated" in documentation

