"""Non-mutating prerequisites observed in the calling execution context."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from .nonblocking import _launch_prerequisites
from .verify import verify


def check():
    checks = {}
    try:
        _launch_prerequisites()
        checks['launch_prerequisites'] = 'ok'
    except (ValueError, OSError) as exc:
        checks['launch_prerequisites'] = str(exc) if str(exc).startswith('agent_') else 'unavailable'
    adoption = verify()
    checks['codex_configuration'] = 'ok' if adoption['ready'] else 'unavailable'
    checks['codex_authentication'] = 'present' if adoption['authentication_available'] and adoption['authentication_format_valid'] else 'unavailable'
    checks['codex_executable'] = 'available' if shutil.which('codex') else 'unavailable'
    try:
        result = subprocess.run(['gh', 'auth', 'status'], capture_output=True, timeout=10, check=False)
        checks['github_authentication'] = 'available' if result.returncode == 0 else 'unavailable'
    except (OSError, subprocess.SubprocessError):
        checks['github_authentication'] = 'unavailable'
    path = Path(os.environ.get('GWAY_AGENT_LEDGER_PATH', str(Path.home() / '.local/state/gway-agent-ops/tasks.sqlite3')))
    checks['registry'] = 'unavailable'
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError('invalid ledger')
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            row = db.execute('SELECT refreshed_at, healthy FROM repository_registry WHERE id=1').fetchone()
        if row:
            refreshed = datetime.fromisoformat(row[0])
            checks['registry'] = ('fresh' if row[1] and refreshed.tzinfo is not None and
                                  refreshed >= datetime.now(timezone.utc) - timedelta(hours=1) else 'stale')
    except (OSError, ValueError, TypeError, sqlite3.Error):
        pass
    ok = {'ok', 'present', 'available', 'fresh'}
    return {'ready': all(value in ok for value in checks.values()), 'checks': checks,
            'context': 'calling process; run in the intended worker identity and environment',
            'limitations': 'No issue, workspace checkout, transient unit, Codex execution, or GitHub repository access was tested.'}
