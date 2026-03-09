"""
scheduler.py — Background job scheduler for recurring tasks.

Runs a weekday morning conflict check at 8:00 AM local time using the
`schedule` library in a daemon thread so it doesn't block the main loop.

NOTE: Jobs fire at local system time. Ensure the Mac's timezone is set
to America/Toronto (System Settings → General → Date & Time → Time Zone).
"""

import threading
import time as _time

import schedule

from . import config
from .conflict_checker import run_morning_check


def _conflict_check_job():
    channel_id = config.SLACK_CONFLICT_CHANNEL or config.SLACK_MOTION_CHANNEL_NAME
    run_morning_check(channel_id)


def start():
    """Register all scheduled jobs and launch the background runner thread."""
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        getattr(schedule.every(), day).at("08:00").do(_conflict_check_job)

    def _run():
        while True:
            schedule.run_pending()
            _time.sleep(30)   # check every 30 s — fine-grained enough for an 8am job

    threading.Thread(target=_run, daemon=True, name="scheduler").start()
    print("🗓️  Scheduler started — conflict check runs weekdays at 08:00.")
