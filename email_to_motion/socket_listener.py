"""
socket_listener.py — Slack Socket Mode listener.

Maintains a persistent WebSocket connection to Slack so the app can receive
slash command invocations in real time, without exposing a public HTTP endpoint.

connect() is non-blocking: the WebSocket runs in SDK-managed background threads,
so this integrates cleanly with the existing synchronous polling loop.

Currently registered commands:
  /availability <date range>   — check calendar availability
"""

import threading
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from . import config
from .availability import handle_availability_command


def _dispatch(client: SocketModeClient, req: SocketModeRequest):
    """Route incoming Socket Mode requests to the appropriate handler."""
    if req.type != "slash_commands":
        return

    # Acknowledge immediately — Slack requires a response within 3 seconds
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    payload    = req.payload
    command    = payload.get("command", "")
    text       = payload.get("text", "").strip()
    channel_id = payload.get("channel_id", "")

    if command == "/availability":
        if not text:
            config.slack.chat_postMessage(
                channel=channel_id,
                text=(
                    "Usage: `/availability Mar 1-15` "
                    "or `/availability March 1 to March 15`"
                ),
            )
            return
        # Spawn a thread so the heavy work (ICS fetch + Claude) doesn't block
        # the Socket Mode listener, which handles all WebSocket traffic
        threading.Thread(
            target=handle_availability_command,
            args=(text, channel_id),
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
    print("🔌  Socket Mode connected — /availability command is active.")
    return client
