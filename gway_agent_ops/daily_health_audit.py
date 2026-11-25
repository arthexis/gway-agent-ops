"""Bounded, read-only local measurements for the daily health task.

Only numeric aggregates leave this module; process arguments, logs, network
addresses and configuration are deliberately excluded. This is not yet a
complete audit or a scheduler entry point.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


def _cpu(path):
    fields = path.read_text(encoding="ascii").splitlines()[0].split()
    if fields[0] != "cpu" or len(fields) < 5:
        raise ValueError("cpu_unavailable")
    values = [int(value) for value in fields[1:9]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _memory(path):
    wanted = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    values = {}
    for line in path.read_text(encoding="ascii").splitlines():
        key, sep, value = line.partition(":")
        if sep and key in wanted:
            fields = value.split()
            if len(fields) != 2 or fields[1] != "kB":
                raise ValueError("memory_unavailable")
            values[key] = int(fields[0]) * 1024
    if set(values) != wanted or values["MemTotal"] <= 0:
        raise ValueError("memory_unavailable")
    return values


def collect(*, proc=Path("/proc"), disk=Path("/"), thermal=Path("/sys/class/thermal"),
            sample_seconds=1.0, sleep=time.sleep, statvfs=os.statvfs):
    """Return bounded measurements and explicit errors, never raw host text."""
    if not 0 < sample_seconds <= 5:
        raise ValueError("invalid_sample_duration")
    metrics, errors = {}, []
    try:
        before = _cpu(proc / "stat")
        sleep(sample_seconds)
        after = _cpu(proc / "stat")
        total, idle = after[0] - before[0], after[1] - before[1]
        if total <= 0 or not 0 <= idle <= total:
            raise ValueError("cpu_unavailable")
        metrics["cpu_busy_percent"] = round(100 * (total - idle) / total, 1)
    except (OSError, UnicodeError, ValueError, IndexError):
        errors.append("cpu_unavailable")
    try:
        mem = _memory(proc / "meminfo")
        metrics["memory_available_percent"] = round(100 * mem["MemAvailable"] / mem["MemTotal"], 1)
        metrics["swap_total_bytes"] = mem["SwapTotal"]
        metrics["swap_used_bytes"] = mem["SwapTotal"] - mem["SwapFree"]
    except (OSError, UnicodeError, ValueError, IndexError):
        errors.append("memory_unavailable")
    try:
        fs = statvfs(disk)
        if fs.f_blocks <= 0 or fs.f_files <= 0:
            raise ValueError("disk_unavailable")
        metrics["disk_used_percent"] = round(100 * (fs.f_blocks - fs.f_bavail) / fs.f_blocks, 1)
        metrics["inodes_used_percent"] = round(100 * (fs.f_files - fs.f_favail) / fs.f_files, 1)
    except (OSError, ValueError, ZeroDivisionError):
        errors.append("disk_unavailable")
    try:
        temperatures = []
        for path in sorted(thermal.glob("thermal_zone*/temp"))[:32]:
            value = int(path.read_text(encoding="ascii").strip()) / 1000
            if -40 <= value <= 150:
                temperatures.append(value)
        if not temperatures:
            raise ValueError("temperature_unavailable")
        metrics["temperature_c"] = max(temperatures)
    except (OSError, UnicodeError, ValueError):
        errors.append("temperature_unavailable")
    return {"metrics": metrics, "errors": errors, "findings": classify(metrics)}


def classify(metrics):
    """Classify sampled values; CPU is a sample, not sustained-load proof."""
    findings = []
    for metric, warning, critical, lower in (
        ("memory_available_percent", 25, 10, True),
        ("disk_used_percent", 80, 95, False),
        ("inodes_used_percent", 80, 95, False),
        ("temperature_c", 70, 80, False),
    ):
        if metric not in metrics:
            continue
        value = metrics[metric]
        severity = ("critical" if (value < critical if lower else value >= critical)
                    else "warning" if (value < warning if lower else value >= warning)
                    else None)
        if severity:
            findings.append({"component": metric, "severity": severity, "value": value})
    return findings


def collect_extended(*, collector=collect, operational=None):
    """Bounded aggregate diagnostics; never publish raw process args or logs."""
    report = collector(sample_seconds=5)
    samples = [report["metrics"].get("cpu_busy_percent")]
    for _ in range(5):
        sample = collector(sample_seconds=5)
        samples.append(sample["metrics"].get("cpu_busy_percent"))
    if all(v is not None for v in samples):
        sustained = min(samples)
        report["metrics"]["cpu_sustained_min_percent"] = sustained
        if sustained >= 40:
            report["findings"].append({"component":"cpu_sustained_min_percent", "severity":"critical" if sustained >= 85 else "warning", "value":sustained})
    else:
        report["errors"].append("cpu_sustained_unavailable")
    metrics = report["metrics"]
    errors = report["errors"]
    for key, argv in (
        ("failed_system_services", ["systemctl","--failed","--no-legend","--plain","--no-pager"]),
        ("failed_user_services", ["systemctl","--user","--failed","--no-legend","--plain","--no-pager"]),
    ):
        try:
            r = subprocess.run(argv,capture_output=True,text=True,timeout=5,check=True)
            metrics[key] = len([line for line in r.stdout.splitlines() if line.strip()])
        except (OSError, subprocess.SubprocessError):
            errors.append(key+"_unavailable")
    try:
        r = subprocess.run(["ps","-eo","stat="],capture_output=True,text=True,timeout=5,check=True)
        metrics["zombie_processes"] = sum(line.strip().startswith("Z") for line in r.stdout.splitlines())
    except (OSError,subprocess.SubprocessError):
        errors.append("processes_unavailable")
    for key in ("failed_system_services","failed_user_services","zombie_processes"):
        if metrics.get(key,0):
            report["findings"].append({"component":key,"severity":"warning","value":metrics[key]})
    extra, unavailable = (operational or operational_metrics)()
    metrics.update(extra)
    errors.extend(unavailable)
    for key in ("throttle_flags", "agentops_release_unverified"):
        if metrics.get(key,0):
            report["findings"].append({"component":key,"severity":"warning","value":metrics[key]})
    return report


def operational_metrics(*, runner=subprocess.run, home=None):
    """Fixed read-only probes; only counts, sizes and booleans are returned."""
    home = Path.home() if home is None else Path(home)
    metrics, errors = {}, []
    probes = (
        ('wireguard_interface_count', ['ip','-o','link','show','type','wireguard'], lambda s: len(s.splitlines())),
        ('dns_resolved', ['getent','ahosts','github.com'], lambda s: bool(s.strip())),
        ('journal_bytes', ['journalctl','--disk-usage'], lambda s: _journal_bytes(s)),
        ('throttle_flags', ['vcgencmd','get_throttled'], lambda s: int(s.strip().split('=')[1],16)),
    )
    for key, argv, parse in probes:
        try:
            r = runner(argv,capture_output=True,text=True,timeout=5,check=True)
            metrics[key] = parse(r.stdout)
        except (OSError,subprocess.SubprocessError,ValueError,IndexError):
            errors.append(key+'_unavailable')
    for key, path in (('agentops_ledger_bytes',home/'.local/state/gway-agent-ops/tasks.sqlite3'),):
        try:
            if path.is_file() and not path.is_symlink():
                metrics[key] = path.stat().st_size
        except OSError:
            errors.append(key+'_unavailable')
    try:
        metrics['maintenance_lock_state'] = maintenance_state(home/'.local/state/maintenance.lock')
    except (OSError, ValueError):
        errors.append('maintenance_lock_unavailable')
    try:
        metrics["agentops_release_unverified"] = not verified_installation(home)
    except (OSError, ValueError, RuntimeError, TypeError):
        errors.append("agentops_release_unavailable")
    return metrics, errors


def _journal_bytes(value):
    import re
    match = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?)B?\s+in the file system',value)
    if not match:
        raise ValueError('journal_usage_unavailable')
    return int(float(match[1])*1024**(' KMGT'.index(match[2] or ' ')))


def maintenance_state(path):
    """Observe an existing lock without creating, truncating or replacing it."""
    import fcntl
    import stat
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return 'absent'
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError('unsafe_maintenance_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'held'
        return 'unlocked'
    finally:
        os.close(fd)


def verified_installation(home):
    """Compare actual activation pointers and inode against installer history."""
    from .release_deploy import _read_history, _release_for, _directory
    root = home/'.local/share/gway-agent-ops'
    releases = root/'releases'
    if maintenance_state(root/'.deploy.lock') == 'held':
        raise ValueError('deployment_in_progress')
    current = _release_for(root/'current', releases)
    entry = _release_for(home/'.local/bin/agent', releases)
    if current is None or entry != current:
        return False
    info = _directory(current)
    history = _read_history(root)
    result = any(r['sha']==current.name and r['identity']==[info.st_dev,info.st_ino] for r in history)
    if maintenance_state(root/'.deploy.lock') == 'held' or _release_for(root/'current',releases)!=current:
        raise ValueError('deployment_in_progress')
    return result
