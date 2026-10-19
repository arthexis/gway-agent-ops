"""Unit tests for isolated systemd probe failure handling."""
import subprocess
from unittest.mock import patch

import pytest
from scripts.systemd_integration_probe import verify

def test_refuses_root():
    with patch("scripts.systemd_integration_probe.os.geteuid", return_value=0):
        with pytest.raises(RuntimeError, match="unprivileged"):
            verify()

def test_fails_without_user_manager():
    with patch("scripts.systemd_integration_probe.os.geteuid", return_value=1000):
        with patch("scripts.systemd_integration_probe.run", return_value=subprocess.CompletedProcess([], 1, "", "no bus")):
            with pytest.raises(RuntimeError, match="user_manager_unavailable"):
                verify()
