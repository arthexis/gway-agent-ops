from types import SimpleNamespace

from gway_agent_ops.daily_health_audit import classify, collect


def test_thresholds_and_cpu_not_claimed_sustained():
    result = classify({"memory_available_percent": 9.9, "disk_used_percent": 80,
                       "inodes_used_percent": 95, "temperature_c": 80,
                       "cpu_busy_percent": 99})
    assert [(item["component"], item["severity"]) for item in result] == [
        ("memory_available_percent", "critical"), ("disk_used_percent", "warning"),
        ("inodes_used_percent", "critical"), ("temperature_c", "critical")]
    assert classify({"memory_available_percent": 25, "disk_used_percent": 79.9}) == []


def test_collect_only_numeric_aggregates(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu 10 0 10 80 0 0 0 0\n")
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\nSwapTotal: 100 kB\nSwapFree: 80 kB\n")
    thermal = tmp_path / "thermal" / "thermal_zone0"
    thermal.mkdir(parents=True)
    (thermal / "temp").write_text("60000\n")
    def sample(_):
        (proc / "stat").write_text("cpu 20 0 20 160 0 0 0 0\n")
    fs = SimpleNamespace(f_blocks=100, f_bavail=50, f_files=100, f_favail=80)
    result = collect(proc=proc, thermal=thermal.parent, sleep=sample, statvfs=lambda _: fs)
    assert result["metrics"]["cpu_busy_percent"] == 20
    assert result["metrics"]["memory_available_percent"] == 50
    assert result["metrics"]["swap_used_bytes"] == 20480
    assert result["metrics"]["temperature_c"] == 60
    assert result["findings"] == result["errors"] == []


def test_missing_sources_are_errors_not_healthy(tmp_path):
    def unavailable(_):
        raise OSError()
    result = collect(proc=tmp_path, thermal=tmp_path, sleep=lambda _: None, statvfs=unavailable)
    assert result["metrics"] == {}
    assert result["errors"] == ["cpu_unavailable", "memory_unavailable", "disk_unavailable", "temperature_unavailable"]
