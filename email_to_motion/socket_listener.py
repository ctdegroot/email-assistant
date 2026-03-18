"""
socket_listener.py — Slack Socket Mode listener.

Maintains a persistent WebSocket connection to Slack so the app can receive
slash command invocations, message shortcuts, modal submissions, and channel
message events in real time, without exposing a public HTTP endpoint.

connect() is non-blocking: the WebSocket runs in SDK-managed background threads,
so this integrates cleanly with the existing synchronous polling loop.

Currently registered:
  /availability <date range> [minutes]  — check calendar availability
  /conflict-check [force | debug]       — check for calendar conflicts
  /get-template                         — upload blank ref-letter YAML template
  "Create Task" message shortcut        — open modal → create Motion task
  "Create Calendar Event" shortcut      — open modal → send calendar invite
  #notes-inbox messages                 — generate Obsidian .md note from forwarded email
  #ref-letters messages (with YAML)     — generate reference letter PDF + .tex zip
  #sign-pdf messages (with PDF)         — sign and date fillable PDF forms
  wd_dismiss / wd_snooze / wd_task_*   — watch-date reminder button actions
"""

import re
import threading
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from . import config
from .availability import (
    handle_availability_command,
    open_availability_match_modal,
    handle_availability_match_submit,
    _parse_availability_args,
)
from . import shortcuts
from . import conflict_checker
from . import slack_notes_handler
from . import watch_date_handler
from . import ref_letter_handler
from . import pdf_sign_handler
from .tasks import process_channel
from .events import process_calendar_channel
from .slack_helpers import get_channel_id

# Channel IDs resolved once at startup for event-driven dispatch
_tasks_channel_id:    str = ""
_calendar_channel_id: str = ""


def _is_authorized(user_id: str) -> bool:
    """Return True if the user is allowed to use this app's commands and shortcuts."""
    allowed = config.ALLOWED_SLACK_USER_ID
    return not allowed or user_id == allowed


def _deny(channel_id: str, user_id: str):
    """Post an ephemeral 'not authorized' message, falling back to a DM."""
    msg = "⛔ Sorry, this command is only available to the app owner."
    try:
        config.slack.chat_postEphemeral(channel=channel_id, user=user_id, text=msg)
    except Exception:
        config.slack.chat_postMessage(channel=user_id, text=msg)


def _dispatch(client: SocketModeClient, req: SocketModeRequest):
    """Route incoming Socket Mode requests to the appropriate handler."""
    # Always acknowledge immediately — Slack requires a response within 3 seconds
    # for ALL request types, not just the ones we handle.
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    # ── Slash commands ────────────────────────────────────────────────────────
    if req.type == "slash_commands":
        payload    = req.payload
        command    = payload.get("command", "")
        text       = payload.get("text", "").strip()
        channel_id = payload.get("channel_id", "")
        user_id    = payload.get("user_id", "")

        if not _is_authorized(user_id):
            _deny(channel_id, user_id)
            return

        if command == "/conflict-check":
            if "debug" in text.lower():
                config.slack.chat_postMessage(
                    channel=channel_id,
                    text="_Fetching this week's events from ICS feed…_",
                )
                threading.Thread(
                    target=conflict_checker.debug_events,
                    args=(channel_id,),
                    daemon=True,
                ).start()
            else:
                force = "force" in text.lower()
                config.slack.chat_postMessage(
                    channel=channel_id,
                    text="_Checking for conflicts this week…_",
                )
                threading.Thread(
                    target=conflict_checker.run_morning_check,
                    args=(channel_id,),
                    kwargs={"verbose": True, "force": force},
                    daemon=True,
                ).start()

        elif command == "/get-template":
            # Send the ref-letter YAML template to the channel where the command was run.
            threading.Thread(
                target=ref_letter_handler.send_template,
                args=(channel_id,),
                daemon=True,
            ).start()

        elif command == "/availability":
            if not text:
                config.slack.chat_postMessage(
                    channel=channel_id,
                    text=(
                        "Usage: `/availability Mar 1-15` "
                        "or `/availability March 1 to March 15`\n"
                        "Optionally append a meeting duration in minutes: "
                        "`/availability Mar 1-15 60`\n"
                        "To cross-check against someone else's availability, "
                        "append `match`: `/availability Mar 1-15 60 match`"
                    ),
                )
                return

            # Detect "match" keyword anywhere in the text (case-insensitive)
            match_mode = bool(re.search(r'\bmatch\b', text, re.IGNORECASE))
            if match_mode:
                clean_text = re.sub(r'\s*\bmatch\b\s*', ' ', text, flags=re.IGNORECASE).strip()
                date_text, duration_minutes = _parse_availability_args(clean_text)
                # Must open modal in this thread — trigger_id expires in 3 s
                trigger_id = payload.get("trigger_id", "")
                open_availability_match_modal(trigger_id, channel_id, user_id, date_text, duration_minutes)
            else:
                threading.Thread(
                    target=handle_availability_command,
                    args=(text, channel_id),
                    daemon=True,
                ).start()

    # ── Interactive payloads (shortcuts and modal submissions) ────────────────
    elif req.type == "interactive":
        payload      = req.payload
        payload_type = payload.get("type")
        user_id      = payload.get("user", {}).get("id", "")
        channel_id   = (payload.get("channel") or {}).get("id", user_id)  # fallback to DM

        if not _is_authorized(user_id):
            _deny(channel_id, user_id)
            return

        # Button clicks in messages (conflict alerts, watch-date reminders, etc.)
        # trigger_id is present, so modals can be opened directly here.
        if payload_type == "block_actions":
            for action in payload.get("actions", []):
                action_id = action.get("action_id", "")
                if action_id == "resolve_conflict":
                    conflict_checker.open_resolve_modal(payload, action)
                elif action_id == "ignore_conflict":
                    conflict_checker.handle_ignore(payload)
                elif action_id in ("wd_dismiss", "wd_snooze", "wd_task_auto", "wd_task_manual"):
                    # Heavy work (note file I/O, Motion API) runs in a thread.
                    # wd_task_auto spawns its own inner thread for Claude + Motion;
                    # the others are fast but still off the dispatch thread for safety.
                    threading.Thread(
                        target=watch_date_handler.handle_watch_date_action,
                        args=(payload, action),
                        daemon=True,
                    ).start()

        # Message shortcuts — open a modal immediately.
        # IMPORTANT: views.open must be called within 3 seconds of the trigger,
        # so we call the modal opener directly here (not in a spawned thread).
        if payload_type == "message_action":
            callback_id = payload.get("callback_id", "")
            if callback_id == "create_task":
                shortcuts.open_create_task_modal(payload)
            elif callback_id == "create_calendar_event":
                shortcuts.open_create_event_modal(payload)

        # Modal submissions — the heavy work (Claude + APIs) runs in a thread.
        elif payload_type == "view_submission":
            callback_id = payload.get("view", {}).get("callback_id", "")
            if callback_id == "create_task_submit":
                threading.Thread(
                    target=shortcuts.handle_create_task_submit,
                    args=(payload,),
                    daemon=True,
                ).start()
            elif callback_id == "create_calendar_event_submit":
                threading.Thread(
                    target=shortcuts.handle_create_event_submit,
                    args=(payload,),
                    daemon=True,
                ).start()
            elif callback_id == "availability_match_submit":
                threading.Thread(
                    target=handle_availability_match_submit,
                    args=(payload,),
                    daemon=True,
                ).start()
            elif callback_id == "resolve_conflict_submit":
                threading.Thread(
                    target=conflict_checker.handle_resolve_submit,
                    args=(payload,),
                    daemon=True,
                ).start()

    # ── Channel message events ────────────────────────────────────────────────
    # Triggered when a message is posted to any channel the bot is in that has
    # message.channels / message.groups event subscriptions enabled.
    # Dispatches to the appropriate pipeline based on which channel the message
    # arrived in: tasks queue, calendar inbox, or notes inbox.
    elif req.type == "events_api":
        event      = req.payload.get("event", {})
        event_type = event.get("type")
        channel    = event.get("channel")

        if event_type == "message":
            if _tasks_channel_id and channel == _tasks_channel_id:
                threading.Thread(
                    target=process_channel,
                    daemon=True,
                ).start()
            elif _calendar_channel_id and channel == _calendar_channel_id:
                threading.Thread(
                    target=process_calendar_channel,
                    daemon=True,
                ).start()
            elif slack_notes_handler._channel_id and channel == slack_notes_handler._channel_id:
                threading.Thread(
                    target=slack_notes_handler.process_message,
                    args=(event,),
                    daemon=True,
                ).start()
            elif ref_letter_handler._channel_id and channel == ref_letter_handler._channel_id:
                threading.Thread(
                    target=ref_letter_handler.process_message,
                    args=(event,),
                    daemon=True,
                ).start()
            elif pdf_sign_handler._channel_id and channel == pdf_sign_handler._channel_id:
                threading.Thread(
                    target=pdf_sign_handler.process_message,
                    args=(event,),
                    daemon=True,
                ).start()


def start(app_token: str) -> SocketModeClient:
    """
    Create, register handlers, and connect the SocketModeClient.

    connect() is non-blocking — the WebSocket runs in SDK background threads.
    Returns the client so the caller can keep a reference to it.
    """
    client = SocketModeClient(
        app_token=app_token,
        web_client=config.slack,
    )
    client.socket_mode_request_listeners.append(_dispatch)
    client.connect()

    global _tasks_channel_id, _calendar_channel_id
    for name, attr in [
        (config.SLACK_MOTION_CHANNEL_NAME, "_tasks_channel_id"),
        (config.SLACK_CALENDAR_CHANNEL,    "_calendar_channel_id"),
    ]:
        try:
            globals()[attr] = get_channel_id(name)
        except Exception as e:
            print(f"⚠️  Could not resolve channel '{name}': {e}")

    slack_notes_handler.init(config.SLACK_NOTES_CHANNEL)
    ref_letter_handler.init(config.SLACK_REFLETTER_CHANNEL)
    pdf_sign_handler.init(config.SLACK_SIGN_CHANNEL)

    notes_status = (
        f"notes-inbox #{config.SLACK_NOTES_CHANNEL} ({slack_notes_handler._channel_id})"
        if slack_notes_handler._channel_id
        else "notes-inbox disabled"
    )
    refletter_status = (
        f"ref-letters #{config.SLACK_REFLETTER_CHANNEL} ({ref_letter_handler._channel_id})"
        if ref_letter_handler._channel_id
        else "ref-letters disabled"
    )
    sign_status = (
        f"sign-pdf #{config.SLACK_SIGN_CHANNEL} ({pdf_sign_handler._channel_id})"
        if pdf_sign_handler._channel_id
        else "sign-pdf disabled"
    )
    print(
        f"🔌  Socket Mode connected — "
        f"/availability, /conflict-check, Create Task, Create Calendar Event, "
        f"{notes_status}, {refletter_status}, {sign_status} active."
    )
    return client
