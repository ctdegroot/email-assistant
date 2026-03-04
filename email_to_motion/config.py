"""
config.py — Environment variables, shared constants, and client initialisation.

All other modules import this module and access clients via config.slack / config.claude
rather than holding their own references, so init_clients() only needs to be called once.
"""

import os
import sys
import anthropic
from dotenv import load_dotenv
from slack_sdk import WebClient

load_dotenv()

# ── Environment variables ─────────────────────────────────────────────────────

SLACK_BOT_TOKEN           = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_MOTION_CHANNEL_NAME = os.environ.get("SLACK_MOTION_CHANNEL", "email-to-motion")
SLACK_CALENDAR_CHANNEL    = os.environ.get("SLACK_CALENDAR_CHANNEL", "email-to-calendar")
MOTION_API_KEY            = os.environ.get("MOTION_API_KEY", "")
MOTION_WORKSPACE_ID       = os.environ.get("MOTION_WORKSPACE_ID", "")
ANTHROPIC_API_KEY         = os.environ.get("ANTHROPIC_API_KEY", "")
SMTP_USER                 = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD             = os.environ.get("SMTP_PASSWORD", "")
CALENDAR_EMAIL            = os.environ.get("CALENDAR_EMAIL", "")

PROCESSED_EMOJI = "white_check_mark"   # ✅ added to handled messages

# ── Shared clients (set by init_clients()) ────────────────────────────────────

slack:      WebClient | None          = None
claude:     anthropic.Anthropic | None = None
OWN_BOT_ID: str | None                = None


def validate():
    missing = [v for v in [
        "SLACK_BOT_TOKEN", "MOTION_API_KEY", "MOTION_WORKSPACE_ID", "ANTHROPIC_API_KEY",
        "SMTP_USER", "SMTP_PASSWORD", "CALENDAR_EMAIL",
    ] if not os.environ.get(v)]
    if missing:
        print("❌  Missing required environment variables:")
        for v in missing:
            print(f"    {v}")
        print("\nSee README.md — copy .env.example to .env and fill in the values.")
        sys.exit(1)


def init_clients():
    global slack, claude, OWN_BOT_ID
    slack      = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    claude     = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    OWN_BOT_ID = slack.auth_test()["bot_id"]
