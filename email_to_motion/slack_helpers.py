"""
slack_helpers.py — Shared Slack utilities used by both the tasks and calendar pipelines.

Covers: channel lookup, message fetching, processed-emoji marking,
attachment downloading, and email content extraction.
"""

import requests
from slack_sdk.errors import SlackApiError
from . import config


def get_channel_id(name: str) -> str:
    """Return the Slack channel ID for the given channel name."""
    cursor = None
    while True:
        kwargs = dict(types="public_channel,private_channel", limit=200)
        if cursor:
            kwargs["cursor"] = cursor
        result = config.slack.conversations_list(**kwargs)
        for ch in result["channels"]:
            if ch["name"] == name:
                return ch["id"]
        cursor = result.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    raise ValueError(
        f"Channel '#{name}' not found. "
        "Create it in Slack and invite your bot with /invite @BotName, then try again."
    )


def get_unprocessed_messages(channel_id: str) -> list:
    """Return messages in the channel that have not yet been marked with ✅."""
    result = config.slack.conversations_history(channel=channel_id, limit=50)
    out = []
    for msg in result.get("messages", []):
        if msg.get("subtype"):
            continue
        if msg.get("bot_id") == config.OWN_BOT_ID:   # skip our own confirmations
            continue
        reactions = msg.get("reactions", [])
        if not any(r["name"] == config.PROCESSED_EMOJI for r in reactions):
            out.append(msg)
    return out


def mark_processed(channel_id: str, ts: str):
    """Add a ✅ reaction to the message so it is not processed again."""
    try:
        config.slack.reactions_add(
            channel=channel_id, timestamp=ts, name=config.PROCESSED_EMOJI
        )
    except SlackApiError as e:
        if e.response["error"] != "already_reacted":
            raise


def _fetch_attachment_text(file: dict) -> str | None:
    """Download a text-based Slack file and return its content (capped at 5 000 chars)."""
    if not file.get("mimetype", "").startswith("text/"):
        return None
    url = file.get("url_private")
    if not url:
        return None
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
            timeout=10,
        )
        r.raise_for_status()
        return r.text[:5000]
    except Exception:
        return None


def extract_email_text(msg: dict) -> str:
    """
    Pull the full email text out of a Slack message.

    Slack's email integration stores the email as a file of type 'email' rather
    than in the message's text field.  Any non-email file attachments are appended
    as labelled sections so Claude can see they exist.

    Returns the combined text, or an empty string if nothing useful is found.
    """
    text = msg.get("text", "").strip()
    attachment_sections = []

    if not text:
        for f in msg.get("files", []):
            if f.get("filetype") == "email":
                subject = f.get("subject", "")
                sender  = (f.get("from") or [{}])[0].get("original", "")
                body    = f.get("plain_text", "")
                text    = "\n".join(p for p in [sender, subject, body] if p).strip()
            else:
                name    = f.get("name", "unknown")
                content = _fetch_attachment_text(f)
                if content:
                    attachment_sections.append(f"--- Attachment: {name} ---\n{content}")
                else:
                    attachment_sections.append(
                        f"--- Attachment: {name} "
                        f"({f.get('pretty_type', f.get('mimetype', 'unknown type'))}) ---\n"
                        f"[Binary file — content not available]"
                    )

    if attachment_sections:
        text += "\n\n" + "\n\n".join(attachment_sections)

    return text
