"""
shortcuts.py — Message shortcut handlers.

Provides "Create Task" and "Create Calendar Event" message shortcuts.

Flow:
  1. User right-clicks a message and selects a shortcut.
  2. A modal opens showing a preview of the selected message and an optional
     "Additional context" field (useful for adding deadlines, clarifications,
     or any detail that isn't in the selected message itself).
  3. On submit, the message text + surrounding conversation context (where
     accessible) + any user-typed context are combined and sent to Claude.
  4. The result is created in Motion / sent as a calendar invite, and an
     ephemeral confirmation is posted back to the user.

Conversation context (up to 5 messages before and after) is fetched
automatically for public/private channels and threads.  It is not available
for DMs between human users (Slack API restriction), which is exactly where
the "Additional context" field is most useful.
"""

import json
import smtplib
import requests
from . import config
from .slack_helpers import extract_email_text
from .tasks import analyze_with_claude, create_motion_task
from .events import analyze_email_for_event, create_ics, send_calendar_invite

# How many messages to fetch on each side of the selected message.
_CONTEXT_COUNT = 5


# ── Context fetching ──────────────────────────────────────────────────────────

def _is_human(m: dict) -> bool:
    """True for human-authored messages (excludes system subtypes and our own bot)."""
    return not m.get("subtype") and m.get("bot_id") != config.OWN_BOT_ID


def _fetch_context_messages(
    channel_id: str, msg: dict
) -> tuple[list[dict], list[dict], bool]:
    """
    Return (before_msgs, after_msgs, context_available) in chronological order.

    Thread messages  — uses conversations_replies for full thread history.
    Channel messages — uses conversations_history; fetches 50 after then
                       reverses so we get the messages immediately after ts.
    DMs              — returns ([], [], False); Slack blocks bot history access
                       for DMs the bot is not a participant in.
    """
    ts = msg["ts"]

    # ── Thread ────────────────────────────────────────────────────────────────
    thread_ts = msg.get("thread_ts")
    if thread_ts:
        try:
            resp     = config.slack.conversations_replies(
                channel=channel_id, ts=thread_ts, limit=100,
            )
            all_msgs = [m for m in resp.get("messages", []) if _is_human(m)]
            before   = [m for m in all_msgs if float(m["ts"]) < float(ts)]
            after    = [m for m in all_msgs if float(m["ts"]) > float(ts)]
            return before[-_CONTEXT_COUNT:], after[:_CONTEXT_COUNT], True
        except Exception:
            pass    # fall through to channel-history strategy

    # ── DMs — not accessible ──────────────────────────────────────────────────
    if channel_id.startswith("D"):
        return [], [], False

    # ── Channel-level messages ────────────────────────────────────────────────
    try:
        resp   = config.slack.conversations_history(
            channel=channel_id, latest=ts, limit=_CONTEXT_COUNT, inclusive=False,
        )
        before = list(reversed([m for m in resp.get("messages", []) if _is_human(m)]))
    except Exception:
        before = []

    try:
        resp  = config.slack.conversations_history(
            channel=channel_id, oldest=ts, limit=50, inclusive=False,
        )
        after = list(reversed([m for m in resp.get("messages", []) if _is_human(m)]))
        after = after[:_CONTEXT_COUNT]
    except Exception:
        after = []

    return before, after, True


def _build_context_text(
    main_msg: dict,
    before: list[dict],
    after: list[dict],
    extra_context: str = "",
) -> str:
    """
    Combine before/main/after messages and any user-provided extra context
    into a single text block for Claude.
    """
    parts = []

    if before:
        parts.append(
            "=== CONVERSATION CONTEXT (messages before the selected message — "
            "may or may not be related) ==="
        )
        for m in before:
            user = m.get("user", "unknown")
            text = m.get("text", "").strip()
            if text:
                parts.append(f"[{user}]: {text}")
        parts.append("")

    parts.append(
        "=== SELECTED MESSAGE (this is the primary content — "
        "use the context above and below only if clearly related) ==="
    )
    parts.append(extract_email_text(main_msg))

    if after:
        parts.append("")
        parts.append(
            "=== CONVERSATION CONTEXT (messages after the selected message — "
            "may or may not be related) ==="
        )
        for m in after:
            user = m.get("user", "unknown")
            text = m.get("text", "").strip()
            if text:
                parts.append(f"[{user}]: {text}")

    if extra_context:
        parts.append("")
        parts.append(
            "=== ADDITIONAL CONTEXT PROVIDED BY USER "
            "(treat this as high-confidence information) ==="
        )
        parts.append(extra_context)

    return "\n".join(parts)


# ── Response helper ───────────────────────────────────────────────────────────

def _respond(channel_id: str, user_id: str, text: str):
    """Ephemeral in channel, falling back to a DM if the bot is not a member."""
    try:
        config.slack.chat_postEphemeral(channel=channel_id, user=user_id, text=text)
    except Exception:
        config.slack.chat_postMessage(channel=user_id, text=text)


# ── Modal helpers ─────────────────────────────────────────────────────────────

def _modal_blocks(preview: str, is_dm: bool) -> list[dict]:
    """Build the shared block layout used by both shortcut modals."""
    # Truncate and quote the preview for display
    truncated = preview[:300] + ("…" if len(preview) > 300 else "")
    quoted    = "\n".join(f"> {line}" for line in truncated.splitlines() if line.strip())
    if not quoted:
        quoted = "> _(no text)_"

    hint = (
        "Conversation context is *not available in DMs* — use this field to supply "
        "any missing details (e.g. 'deadline is March 21' or 'meeting next Tuesday 2pm')."
        if is_dm else
        "Optional — add any details not captured in the message above "
        "(e.g. 'deadline is March 21' or 'this is for the AI conference')."
    )

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Selected message:*\n{quoted}"},
        },
        {"type": "divider"},
        {
            "type": "input",
            "optional": True,
            "block_id": "context_block",
            "label": {"type": "plain_text", "text": "Additional context"},
            "hint": {"type": "plain_text", "text": hint},
            "element": {
                "type": "plain_text_input",
                "action_id": "context_input",
                "multiline": True,
                "placeholder": {
                    "type": "plain_text",
                    "text": "E.g. 'deadline is March 21' or 'meeting next Tuesday at 2pm'",
                },
            },
        },
    ]


def open_create_task_modal(payload: dict):
    """Open the Create Task modal. Must be called within 3s of the trigger."""
    channel_id = payload["channel"]["id"]
    msg        = payload["message"]
    preview    = extract_email_text(msg)
    is_dm      = channel_id.startswith("D")

    metadata = json.dumps({
        "channel_id": channel_id,
        "ts":         msg["ts"],
        "thread_ts":  msg.get("thread_ts"),
        "msg_text":   preview[:2000],   # stored for re-use at submit time
    })

    config.slack.views_open(
        trigger_id=payload["trigger_id"],
        view={
            "type":             "modal",
            "callback_id":      "create_task_submit",
            "title":            {"type": "plain_text", "text": "Create Task"},
            "submit":           {"type": "plain_text", "text": "Create"},
            "close":            {"type": "plain_text", "text": "Cancel"},
            "private_metadata": metadata,
            "blocks":           _modal_blocks(preview, is_dm),
        },
    )


def open_create_event_modal(payload: dict):
    """Open the Create Calendar Event modal. Must be called within 3s of the trigger."""
    channel_id = payload["channel"]["id"]
    msg        = payload["message"]
    preview    = extract_email_text(msg)
    is_dm      = channel_id.startswith("D")

    metadata = json.dumps({
        "channel_id": channel_id,
        "ts":         msg["ts"],
        "thread_ts":  msg.get("thread_ts"),
        "msg_text":   preview[:2000],
    })

    config.slack.views_open(
        trigger_id=payload["trigger_id"],
        view={
            "type":             "modal",
            "callback_id":      "create_calendar_event_submit",
            "title":            {"type": "plain_text", "text": "Create Calendar Event"},
            "submit":           {"type": "plain_text", "text": "Create"},
            "close":            {"type": "plain_text", "text": "Cancel"},
            "private_metadata": metadata,
            "blocks":           _modal_blocks(preview, is_dm),
        },
    )


# ── Submit handlers (called after the user clicks Create in the modal) ────────

def _extract_submission(payload: dict) -> tuple[str, str, str, str, str]:
    """
    Unpack a view_submission payload.
    Returns (channel_id, user_id, msg_text, thread_ts, extra_context).
    """
    user_id  = payload["user"]["id"]
    view     = payload["view"]
    meta     = json.loads(view["private_metadata"])

    channel_id  = meta["channel_id"]
    ts          = meta["ts"]
    thread_ts   = meta.get("thread_ts")
    msg_text    = meta.get("msg_text", "")
    extra_context = (
        view["state"]["values"]
        .get("context_block", {})
        .get("context_input", {})
        .get("value") or ""
    ).strip()

    return channel_id, user_id, ts, thread_ts, msg_text, extra_context


def handle_create_task_submit(payload: dict):
    """Process the Create Task modal submission."""
    channel_id, user_id, ts, thread_ts, msg_text, extra_context = \
        _extract_submission(payload)

    # Try to enrich with surrounding conversation context
    synthetic_msg = {"ts": ts, "text": msg_text}
    if thread_ts:
        synthetic_msg["thread_ts"] = thread_ts
    before, after, _ = _fetch_context_messages(channel_id, synthetic_msg)

    text = _build_context_text(synthetic_msg, before, after, extra_context)
    if not text.strip():
        _respond(channel_id, user_id, "⚠️ Could not extract any text from that message.")
        return

    try:
        tasks = analyze_with_claude(text)
    except json.JSONDecodeError as e:
        _respond(channel_id, user_id, f"⚠️ Claude returned invalid JSON: {e}")
        return
    except Exception as e:
        _respond(channel_id, user_id, f"⚠️ Claude error: {e}")
        return

    if not tasks:
        _respond(channel_id, user_id, "ℹ️ No actionable tasks found in that message.")
        return

    created, errors = [], []
    for task in tasks:
        try:
            create_motion_task(task)
            due = task.get("dueDate") or "flexible"
            created.append(
                f"*{task['name']}* — "
                f"{task['priority']} · {task['duration']} min · due {due}"
            )
        except requests.HTTPError as e:
            errors.append(f"{task.get('name', '?')}: {e.response.status_code} {e.response.text}")
        except Exception as e:
            errors.append(f"{task.get('name', '?')}: {e}")

    lines = []
    if created:
        n = len(created)
        lines.append(f"✅ *{n} Motion task{'s' if n > 1 else ''} created*")
        lines.extend(f"• {t}" for t in created)
    if errors:
        lines.append(f"⚠️ {len(errors)} failed:")
        lines.extend(f"• {e}" for e in errors)

    _respond(channel_id, user_id, "\n".join(lines))


def handle_create_event_submit(payload: dict):
    """Process the Create Calendar Event modal submission."""
    channel_id, user_id, ts, thread_ts, msg_text, extra_context = \
        _extract_submission(payload)

    synthetic_msg = {"ts": ts, "text": msg_text}
    if thread_ts:
        synthetic_msg["thread_ts"] = thread_ts
    before, after, _ = _fetch_context_messages(channel_id, synthetic_msg)

    text = _build_context_text(synthetic_msg, before, after, extra_context)
    if not text.strip():
        _respond(channel_id, user_id, "⚠️ Could not extract any text from that message.")
        return

    try:
        event = analyze_email_for_event(text)
    except json.JSONDecodeError as e:
        _respond(channel_id, user_id, f"⚠️ Claude returned invalid JSON: {e}")
        return
    except Exception as e:
        _respond(channel_id, user_id, f"⚠️ Claude error: {e}")
        return

    try:
        ics = create_ics(event)
        send_calendar_invite(event, ics)
    except smtplib.SMTPException as e:
        _respond(channel_id, user_id, f"⚠️ Could not send calendar invite: {e}")
        return
    except Exception as e:
        _respond(channel_id, user_id, f"⚠️ Error creating calendar event: {e}")
        return

    label = "all-day" if event.get("all_day") else f"{event['start']} → {event['end']}"
    lines = ["📅 *Calendar invite sent*", f"• *{event['title']}*", f"• {label}"]
    if event.get("location"):
        lines.append(f"• {event['location']}")
    _respond(channel_id, user_id, "\n".join(lines))
