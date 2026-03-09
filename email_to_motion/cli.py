"""
cli.py — Command-line entry point.

Parses arguments, initialises clients, and runs one or both pipelines
either once or on a repeating interval.
"""

import argparse
import sys
import time
from . import config
from .tasks import get_workspaces, process_channel
from .events import process_calendar_channel
from . import socket_listener


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Monitor Slack channels for forwarded emails and create "
            "Motion tasks or Outlook calendar events automatically."
        )
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="Run continuously, checking both channels on a fixed interval.",
    )
    parser.add_argument(
        "--interval", type=int, default=30, metavar="MINUTES",
        help="Polling interval in minutes when using --loop (default: 30).",
    )
    parser.add_argument(
        "--tasks-only", action="store_true",
        help="Process only the #email-to-motion tasks channel.",
    )
    parser.add_argument(
        "--calendar-only", action="store_true",
        help="Process only the #email-to-calendar channel.",
    )
    parser.add_argument(
        "--workspaces", action="store_true",
        help="List your Motion workspace IDs and exit (useful during initial setup).",
    )
    args = parser.parse_args()

    if args.workspaces:
        config.validate()
        config.init_clients()
        print("Fetching Motion workspaces…")
        for ws in get_workspaces():
            print(f"  {ws['id']}  —  {ws['name']}")
        sys.exit(0)

    config.validate()
    config.init_clients()

    # Start Socket Mode listener for slash commands (e.g. /availability).
    # Requires SLACK_APP_TOKEN (xapp-…) — silently skipped if not configured.
    if config.SLACK_APP_TOKEN:
        socket_listener.start(config.SLACK_APP_TOKEN)
    else:
        print("ℹ️   SLACK_APP_TOKEN not set — slash commands disabled.")

    run_tasks    = not args.calendar_only
    run_calendar = not args.tasks_only

    def run_once():
        if run_tasks:
            print(f"Checking #{config.SLACK_MOTION_CHANNEL_NAME}…")
            process_channel()
        if run_calendar:
            print(f"Checking #{config.SLACK_CALENDAR_CHANNEL}…")
            process_calendar_channel()

    if args.loop:
        print(f"🔄  Running every {args.interval} minutes. Ctrl-C to stop.\n")
        try:
            while True:
                print(f"[{time.strftime('%H:%M:%S')}]")
                run_once()
                time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        run_once()
