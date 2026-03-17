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
SLACK_APP_TOKEN           = os.environ.get("SLACK_APP_TOKEN", "")   # xapp-… required for Socket Mode
SLACK_MOTION_CHANNEL_NAME = os.environ.get("SLACK_MOTION_CHANNEL", "email-to-motion")
SLACK_CALENDAR_CHANNEL    = os.environ.get("SLACK_CALENDAR_CHANNEL", "email-to-calendar")
MOTION_API_KEY            = os.environ.get("MOTION_API_KEY", "")
MOTION_WORKSPACE_ID       = os.environ.get("MOTION_WORKSPACE_ID", "")
# Motion user ID to assign created tasks to. Find yours at usemotion.com → Settings → Profile.
MOTION_ASSIGNEE_ID        = os.environ.get("MOTION_ASSIGNEE_ID", "")
ANTHROPIC_API_KEY         = os.environ.get("ANTHROPIC_API_KEY", "")
SMTP_USER                 = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD             = os.environ.get("SMTP_PASSWORD", "")
CALENDAR_EMAIL            = os.environ.get("CALENDAR_EMAIL", "")
OUTLOOK_ICS_URL           = os.environ.get("OUTLOOK_ICS_URL", "")   # secret ICS feed URL

# Your Outlook/university email address — used to filter yourself out of attendee lists.
# This is likely different from SMTP_USER (your Gmail). If not set, falls back to SMTP_USER.
CALENDAR_OWNER_EMAIL      = os.environ.get("CALENDAR_OWNER_EMAIL", "")

# The Slack user ID of the only person allowed to use slash commands and shortcuts.
# Find yours at slack.com/account/settings (Profile → ··· → Copy member ID).
ALLOWED_SLACK_USER_ID     = os.environ.get("ALLOWED_SLACK_USER_ID", "")

# Channel to post conflict alerts to. Defaults to SLACK_MOTION_CHANNEL if not set.
SLACK_CONFLICT_CHANNEL    = os.environ.get("SLACK_CONFLICT_CHANNEL", "")

# Notes inbox — channel name (without #) for email-to-note forwarding.
# Leave blank to disable note processing.
SLACK_NOTES_CHANNEL       = os.environ.get("SLACK_NOTES_CHANNEL", "")

# Reminders channel — channel name (without #) for automated watch-date reminders.
# Leave blank to disable proactive reminders.
SLACK_REMINDERS_CHANNEL   = os.environ.get("SLACK_REMINDERS_CHANNEL", "")

# Reference letter generator — channel name (without #) for YAML-driven letter generation.
# Leave blank to disable.
SLACK_REFLETTER_CHANNEL   = os.environ.get("SLACK_REFLETTER_CHANNEL", "")

# Directory containing LaTeX template support files (WesternLetter.cls, Signature.pdf,
# Engineer_Stacked_PurpleGrey.png, etc.).  Defaults to ref_letter_templates/ inside the
# project directory.  Use an absolute path if running from a different working directory.
REF_LETTER_TEMPLATES_DIR  = os.environ.get(
    "REF_LETTER_TEMPLATES_DIR",
    str(__import__("pathlib").Path(__file__).parent.parent / "ref_letter_templates"),
)

# Where to write generated reference letter files (.tex, .pdf, .zip).
REF_LETTERS_OUTPUT_PATH   = os.environ.get("REF_LETTERS_OUTPUT_PATH", "~/email_to_motion_ref_letters")

# Where to write generated .md note files (always written locally for inspection/backup).
# Defaults to ~/email_to_motion_notes/. Use an absolute path for a Linux server.
NOTES_OUTPUT_PATH         = os.environ.get("NOTES_OUTPUT_PATH", "~/email_to_motion_notes")

# ── Obsidian vault delivery (Stage 2) ─────────────────────────────────────────
# Set OBSIDIAN_DELIVERY=git to push notes to the vault via Git after writing locally.
# Leave unset (or set to "local") to write only to NOTES_OUTPUT_PATH.
OBSIDIAN_DELIVERY         = os.environ.get("OBSIDIAN_DELIVERY", "local").lower()

# Absolute (or ~/…) path to the local Git clone of your Obsidian vault on this server.
OBSIDIAN_VAULT_PATH       = os.environ.get("OBSIDIAN_VAULT_PATH", "~/obsidian-vault")

# Subfolder inside the vault where inbox notes should be written.
# The folder is created automatically if it doesn't exist.
# Example: "Notes/Inbox" or "00 Inbox"
OBSIDIAN_NOTES_SUBFOLDER  = os.environ.get("OBSIDIAN_NOTES_SUBFOLDER", "Notes/Inbox")

PROCESSED_EMOJI = "white_check_mark"   # ✅ added to handled messages

# ── Activity log ───────────────────────────────────────────────────────────────
# JSONL file that records every item processed (tasks, notes, events, etc.).
# Used for future dashboards and digest reports.  Defaults to a file in the
# same state directory as known_tags.json and conflict dedup data.
ACTIVITY_LOG_PATH = os.environ.get(
    "ACTIVITY_LOG_PATH",
    "~/.email_to_motion/activity.jsonl",
)

# ── Shared clients (set by init_clients()) ────────────────────────────────────

slack:      WebClient | None          = None
claude:     anthropic.Anthropic | None = None
OWN_BOT_ID: str | None                = None


def validate():
    missing = [v for v in [
        "SLACK_BOT_TOKEN", "MOTION_API_KEY", "MOTION_WORKSPACE_ID", "MOTION_ASSIGNEE_ID",
        "ANTHROPIC_API_KEY", "SMTP_USER", "SMTP_PASSWORD", "CALENDAR_EMAIL",
    ] if not os.environ.get(v)]
    if missing:
        print("❌  Missing required environment variables:")
        for v in missing:
            print(f"    {v}")
        print("\nSee README.md — copy .env.example to .env and fill in the values.")
        sys.exit(1)


def init_clients():
    global slack, claude, OWN_BOT_ID
    slack  = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    try:
        OWN_BOT_ID = slack.auth_test()["bot_id"]
    except Exception as exc:
        # Non-fatal at init time: OWN_BOT_ID is used only to filter the bot's
        # own messages.  A None value disables self-filtering until the Slack
        # token can be verified, rather than crashing the entire process.
        print(f"⚠️  Could not retrieve bot ID (auth_test failed): {exc}")
        OWN_BOT_ID = None
