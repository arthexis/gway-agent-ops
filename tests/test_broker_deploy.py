from pathlib import Path
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]

def test_pinned_runner_rejects_wrong_revision_and_dirty_checkout(tmp_path):
    sha = "a" * 40
    checkout = tmp_path / sha
    checkout.mkdir()
    shutil.copy2(ROOT / "broker-deploy.sh", checkout)
    fake = tmp_path / "bin"
    fake.mkdir()
    git = fake / "git"
    git.write_text('#!/bin/sh\ncase "$*" in *rev-parse*) echo "$FAKE_SHA";; *status*) printf "%s" "$FAKE_DIRTY";; esac\n')
    git.chmod(0o755)
    marker = checkout / "deploy.sh"
    marker.write_text("#!/bin/sh\nexit 99\n")
    marker.chmod(0o755)
    env = dict(os.environ, PATH=str(fake)+":/usr/bin:/bin", FAKE_SHA="b"*40, FAKE_DIRTY="")
    assert subprocess.run([str(checkout/"broker-deploy.sh")], env=env).returncode == 2
    env.update(FAKE_SHA=sha, FAKE_DIRTY=" M deploy.sh")
    assert subprocess.run([str(checkout/"broker-deploy.sh")], env=env).returncode == 2

def test_publication_preserves_pin_and_rolls_back():
    text = (ROOT / "ansible/publish-mcp.yml").read_text()
    assert "broker_declaration" in text
    assert "previous_manifest" in text
    assert "rescue:" in text
    assert "Restore previous manifest atomically" in text
    assert ".get('deployment', {}) == broker_declaration" in text

def test_runner_uses_fixed_deploy_command(tmp_path):
    sha = "a" * 40
    checkout = tmp_path / sha
    checkout.mkdir()
    shutil.copy2(ROOT / "broker-deploy.sh", checkout)
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "calls"
    for name, body in {
        "git": 'case "$*" in *rev-parse*) echo "$FAKE_SHA";; *status*) :;; esac',
        "id": "echo 1000",
        "ansible-playbook": 'printf "stage %s\n" "$*" >> "$LOG"',
        "sudo": 'printf "publish %s\n" "$*" >> "$LOG"',
    }.items():
        path = fake / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    deploy = checkout / "deploy.sh"
    deploy.write_text('#!/bin/sh\nprintf "deploy %s %s\n" "$DBUS_SESSION_BUS_ADDRESS" "$*" >> "$LOG"\n')
    deploy.chmod(0o644)
    env = dict(os.environ, PATH=str(fake)+":/usr/bin:/bin", FAKE_SHA=sha, LOG=str(log))
    subprocess.run([str(checkout/"broker-deploy.sh")], env=env, check=True)
    calls = log.read_text()
    assert "deploy unix:path=/run/user/1000/bus" in calls
    assert "--expected-sha "+sha in calls
