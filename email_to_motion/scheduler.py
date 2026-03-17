"""
scheduler.py — Background job scheduler for recurring tasks.

Jobs (all fire at local system time):
  - Weekdays 08:00  Conflict check — scans ICS feed for calendar conflicts.
  - Daily    09:00  Watch-date scan — posts Slack reminders for upcoming note
                    deadlines (grants, CFPs, decision dates, etc.).

NOTE: Jobs fire at local system time. Ensure the Mac's timezone is set
to America/Toronto (System Settings → General → Date & Time → Time Zone).
"""

import logging
import threading
import time as _time

import schedule

from . import config
from .conflict_checker import run_morning_check
from .watch_date_handler import scan_and_remind

log = logging.getLogger(__name__)


def _conflict_check_job():
    channel_id = config.SLACK_CONFLICT_CHANNEL or config.SLACK_MOTION_CHANNEL_NAME
    run_morning_check(channel_id)


def _watch_date_job():
    scan_and_remind()


def start():
    """Register all scheduled jobs and launch the background runner thread."""
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        getattr(schedule.every(), day).at("08:00").do(_conflict_check_job)

    # Watch-date reminders run every day (including weekends) since deadlines
    # don't stop for weekends.
    schedule.every().day.at("09:00").do(_watch_date_job)

    def _run():
        while True:
            try:
                schedule.run_pending()
            except Exception:
                # Log but do not crash the scheduler thread; the next tick will retry.
                log.exception("scheduler: unhandled exception in scheduled job")
            _time.sleep(30)   # check every 30 s — fine-grained enough for minute-level jobs

    threading.Thread(target=_run, daemon=True, name="scheduler").start()
    log.info("scheduler: started — conflict check weekdays 08:00, watch-date scan daily 09:00")
