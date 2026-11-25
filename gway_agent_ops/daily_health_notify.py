"""Opt-in health notifications with durable at-most-once local claims."""
import hashlib
import json
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from . import activity_feed, lcd_notification
from .scheduler import ledger_path
from .task_ledger import TaskLedger


def notify(day, report, *, path=None, runner=subprocess.run):
    date.fromisoformat(day)
    critical = sum(f['severity']=='critical' for f in report['findings'])
    identity = hashlib.sha256(('gway-001-daily-health:'+day).encode()).hexdigest()
    channels = []
    if activity_feed.enabled():
        channels.append(('feed', [['/usr/local/bin/gway-remote','feed','Daily node health audit','--summary',f"Findings: {len(report['findings'])}; critical: {critical}; unavailable: {len(report['errors'])}",'--id',identity,'--time',datetime.now(timezone.utc).isoformat(),'--type','agentops.daily-health','--category','agentops','--severity','error' if critical else 'warning' if report['findings'] or report['errors'] else 'info','--node','gway-001']]))
    if critical and lcd_notification.enabled():
        notice = dict(schema='gway-notification/v1',system='fleet',origin_node='gway-001',target_node='gway-001',subject={'type':'service','name':'daily-health'},state='degraded',severity='error',reason_code='CRITICAL_HEALTH',episode_id=identity[:32],expires_in=120)
        channels.append(('critical', [[str(Path.home()/'.local/bin/lcd'),'notice',json.dumps(notice)], [str(Path.home()/'.local/bin/sound'),'warning']]))
    for channel, commands in channels:
        with TaskLedger(ledger_path(path)) as ledger:
            ledger.db.execute('CREATE TABLE IF NOT EXISTS daily_health_notifications (day TEXT, channel TEXT, PRIMARY KEY(day,channel))')
            claimed = ledger.db.execute('INSERT OR IGNORE INTO daily_health_notifications VALUES (?,?)',(day,channel)).rowcount
        if claimed:
            for argv in commands:
                try:
                    runner(argv,timeout=8,check=False,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                except (OSError,subprocess.SubprocessError):
                    pass
