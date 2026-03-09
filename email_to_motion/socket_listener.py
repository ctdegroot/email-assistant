"""
socket_listener.py — Slack Socket Mode listener.

Maintains a persistent WebSocket connection to Slack so the app can receive
slash command invocations, message shortcuts, and modal submissions in real
time, without exposing a public HTTP endpoint.

connect() is non-blocking: the WebSocket runs in SDK-managed background threads,
so this integrates cleanly with the existing synchronous polling loop.

Currently registered:
  /availability <date range> [minutes]  — check calendar availability
  "Create Task" message shortcut        — open modal → create Motion task
  "Create Calendar Event" shortcut      — open modal → send calendar invite
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

        if command == "/availability":
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
    print(
        "🔌  Socket Mode connected — "
        "/availability, Create Task, and Create Calendar Event are active."
    )
    return client
