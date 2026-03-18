"""
conflict_checker.py — Proactive calendar conflict detection and resolution.

Called every weekday at 8am by scheduler.py:
  1. Fetch the ICS feed for the current work week (Mon–Fri).
  2. Find pairs of overlapping timed events.
  3. Post an interactive Slack alert for each conflict with
     [Resolve Conflict] and [Ignore] buttons.

Resolution flow (triggered by button click → socket_listener.py):
  [Resolve Conflict]
    → open_resolve_modal() opens a modal asking which event to reschedule
  [modal submit]
    → handle_resolve_submit() generates a ready-to-send email draft with
      availability and posts it to Slack for copying

Email draft logic:
  - Offer remaining slots this week (today→Friday).
  - If today is Thursday or Friday, also offer Monday–Tuesday of next week
    as a fallback, presented in the email after this-week times.
  - The slot occupied by the meeting being moved is treated as free
    (since rescheduling it opens it up).
"""

import json
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytz
import requests
from icalendar import Calendar
import recurring_ical_events

from . import config
from .utils import call_with_retries, get_with_retries
from .availability import (
    TORONTO_TZ, WORK_START, WORK_END,
    fetch_busy_blocks, _build_summary, _to_toronto,
)


# ── Event extraction ──────────────────────────────────────────────────────────

def _event_info(event) -> dict | None:
    """
    Extract key fields from an icalendar VEVENT component.

    Returns None for:
      - All-day events (vDate instead of datetime) — they're markers, not appointments
      - Events marked TRANSP=TRANSPARENT (show as free)
    """
    dtstart = event.get("DTSTART")
    dtend   = event.get("DTEND")
    if not dtstart or not dtend:
        return None

    # All-day events have a vDate (date), not a datetime
    if not isinstance(dtstart.dt, datetime):
        return None

    # Events marked as "free" — no actual scheduling conflict
    if str(event.get("TRANSP", "")).upper() == "TRANSPARENT":
        return None

    start = _to_toronto(dtstart)
    end   = _to_toronto(dtend)
    title = str(event.get("SUMMARY", "Untitled Event"))

    # Extract attendees, excluding the calendar owner.
    # CALENDAR_OWNER_EMAIL is the Outlook/university address; SMTP_USER is Gmail.
    # Both are checked so self never appears in the list regardless of which address
    # Outlook uses to identify the organiser.
    owner_emails = {e.lower() for e in [config.CALENDAR_OWNER_EMAIL, config.SMTP_USER] if e}

    attendees = []
    raw = event.get("ATTENDEE")
    if raw is not None:
        if not isinstance(raw, list):
            raw = [raw]
        for att in raw:
            email = str(att).replace("mailto:", "").strip().lower()
            name  = str(att.params.get("CN", "")).strip() if hasattr(att, "params") else ""
            if email and email not in owner_emails:
                attendees.append({"name": name, "email": email})

    return {
        "title":     title,
        "start":     start.isoformat(),
        "end":       end.isoformat(),
        "attendees": attendees[:8],   # cap to keep JSON payloads small
        "uid":       str(event.get("UID", "")),
    }


# ── Seen-conflict deduplication ───────────────────────────────────────────────
# Conflicts are recorded when first posted so the same pair isn't re-alerted
# each morning. The record is tied to the current Mon–Fri week; it resets
# automatically at the start of each new week.

def _seen_path() -> Path:
    """Path to the JSON file that tracks already-reported conflicts."""
    data_dir = Path.home() / ".email_to_motion"
    data_dir.mkdir(exist_ok=True)
    return data_dir / "seen_conflicts.json"


def _week_key() -> str:
    """ISO date string for the Monday of the current week — used as the expiry key."""
    today = date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


def _load_seen() -> set[str]:
    """Load the set of already-reported conflict IDs for the current week."""
    path = _seen_path()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        if data.get("week") == _week_key():
            return set(data.get("ids", []))
    except Exception:
        pass
    return set()   # expired or corrupt — start fresh


def _save_seen(seen: set[str]):
    """Persist the seen-conflict set for the current week."""
    try:
        _seen_path().write_text(json.dumps({"week": _week_key(), "ids": list(seen)}))
    except Exception:
        pass   # non-fatal


def _conflict_id(event_a: dict, event_b: dict) -> str:
    """
    Stable identifier for a conflict pair, independent of event ordering.
    Falls back to title+start if the UID is blank.
    """
    def _key(e: dict) -> str:
        return e["uid"] or f"{e['title']}@{e['start']}"
    keys = sorted([_key(event_a), _key(event_b)])
    return f"{keys[0]}|{keys[1]}"


# ── Conflict detection ────────────────────────────────────────────────────────

def _current_week_range() -> tuple[date, date]:
    """Return (Monday, Friday) of the current calendar week."""
    today  = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=4)


def find_conflicts(start_date: date, end_date: date) -> list[tuple[dict, dict]]:
    """
    Download the ICS feed and return all (event_a, event_b) pairs whose
    time ranges overlap within the given date window.
    """
    r = get_with_retries(config.OUTLOOK_ICS_URL, timeout=30)
    r.raise_for_status()
    cal = Calendar.from_ical(r.content)

    window_start = datetime.combine(start_date, time(0, 0))
    window_end   = datetime.combine(end_date,   time(23, 59, 59))
    raw_events   = recurring_ical_events.of(cal).between(window_start, window_end)

    events = [info for ev in raw_events if (info := _event_info(ev)) is not None]

    conflicts = []
    for i, a in enumerate(events):
        for b in events[i + 1:]:
            start_a = datetime.fromisoformat(a["start"])
            end_a   = datetime.fromisoformat(a["end"])
            start_b = datetime.fromisoformat(b["start"])
            end_b   = datetime.fromisoformat(b["end"])
            # True overlap: A starts before B ends AND A ends after B starts
            if start_a < end_b and end_a > start_b:
                conflicts.append((a, b))

    return conflicts


# ── Morning check entry point ─────────────────────────────────────────────────

def debug_events(channel_id: str):
    """
    Fetch this week's events and post a full dump of titles, times, and raw
    attendee data to Slack. Used to verify ICS feed content after changing
    sharing settings. Triggered by `/conflict-check debug`.
    """
    monday, friday = _current_week_range()
    try:
        r = get_with_retries(config.OUTLOOK_ICS_URL, timeout=30)
        r.raise_for_status()
        cal = Calendar.from_ical(r.content)
        window_start = datetime.combine(monday, time(0, 0))
        window_end   = datetime.combine(friday, time(23, 59, 59))
        raw_events   = recurring_ical_events.of(cal).between(window_start, window_end)
    except Exception as e:
        config.slack.chat_postMessage(channel=channel_id, text=f"⚠️ Could not fetch ICS: {e}")
        return

    lines = [f"*ICS event dump — {monday} to {friday}*\n"]
    for ev in raw_events:
        dtstart = ev.get("DTSTART")
        if not dtstart or not isinstance(dtstart.dt, datetime):
            continue
        title = str(ev.get("SUMMARY", "Untitled"))
        start = _to_toronto(dtstart)

        raw_att = ev.get("ATTENDEE")
        if raw_att is None:
            att_str = "_no ATTENDEE field_"
        else:
            if not isinstance(raw_att, list):
                raw_att = [raw_att]
            att_str = ", ".join(
                f"{str(a).replace('mailto:', '')} (CN={a.params.get('CN', '?')})"
                if hasattr(a, "params") else str(a)
                for a in raw_att
            )

        lines.append(
            f"• *{title}* — {start.strftime('%a %-d %b, %-I:%M %p')}\n"
            f"  Attendees: {att_str}"
        )

    config.slack.chat_postMessage(
        channel=channel_id,
        text="\n".join(lines) or "No timed events found this week.",
    )


def run_morning_check(channel_id: str, verbose: bool = False, force: bool = False):
    """
    Fetch the ICS feed for the current work week, find conflicts, and post
    an interactive Slack alert for each *new* (not yet seen) conflict.

    Args:
        verbose: If True, post a "no conflicts" confirmation when the week is clean.
                 Used by the manual /conflict-check command.
        force:   If True, ignore the seen-conflict list and report everything.
                 Used by the manual /conflict-check command for full re-checks.
    """
    monday, friday = _current_week_range()

    try:
        conflicts = find_conflicts(monday, friday)
    except Exception as e:
        config.slack.chat_postMessage(
            channel=channel_id,
            text=f"⚠️ Conflict check failed: {e}",
        )
        return

    seen = set() if force else _load_seen()
    new_conflicts = [(a, b) for a, b in conflicts if _conflict_id(a, b) not in seen]

    if not new_conflicts:
        if verbose:
            config.slack.chat_postMessage(
                channel=channel_id,
                text="✅ No conflicts found for this week.",
            )
        return

    for event_a, event_b in new_conflicts:
        _post_conflict_alert(channel_id, event_a, event_b)
        seen.add(_conflict_id(event_a, event_b))

    _save_seen(seen)


def _fmt_event_line(info: dict) -> str:
    """One-line mrkdwn summary of an event for the Slack alert."""
    start = datetime.fromisoformat(info["start"])
    end   = datetime.fromisoformat(info["end"])
    return (
        f"*{info['title']}*  —  "
        f"{start.strftime('%A, %B %-d')}  "
        f"{start.strftime('%-I:%M %p')}–{end.strftime('%-I:%M %p')}"
    )


def _post_conflict_alert(channel_id: str, event_a: dict, event_b: dict):
    """
    Post an interactive Slack message about a specific conflict pair.
    The [Resolve Conflict] button value carries both events' data as JSON
    so the modal opener can reconstruct the context without a database.
    """
    # Build conflict payload; trim attendees if too long for Slack's 2000-char limit
    def _make_payload():
        return json.dumps({
            "channel_id": channel_id,
            "event_a":    event_a,
            "event_b":    event_b,
        })

    payload_str = _make_payload()
    while len(payload_str) > 1900 and (event_a["attendees"] or event_b["attendees"]):
        if event_a["attendees"]:
            event_a["attendees"].pop()
        elif event_b["attendees"]:
            event_b["attendees"].pop()
        payload_str = _make_payload()

    config.slack.chat_postMessage(
        channel=channel_id,
        text="⚠️ Calendar conflict detected",   # notification fallback
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "⚠️ *Calendar Conflict Detected*\n\n"
                        f"• {_fmt_event_line(event_a)}\n"
                        f"• {_fmt_event_line(event_b)}"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type":      "button",
                        "text":      {"type": "plain_text", "text": "Resolve Conflict"},
                        "style":     "primary",
                        "action_id": "resolve_conflict",
                        "value":     payload_str,
                    },
                    {
                        "type":      "button",
                        "text":      {"type": "plain_text", "text": "Ignore"},
                        "action_id": "ignore_conflict",
                        "value":     "ignore",
                    },
                ],
            },
        ],
    )


# ── Interactive button handlers ───────────────────────────────────────────────

def handle_ignore(payload: dict):
    """
    Update the original conflict alert to show it was dismissed,
    replacing the action buttons with a simple acknowledgement.
    """
    channel_id = payload.get("channel", {}).get("id", "")
    message_ts = payload.get("message", {}).get("ts", "")
    try:
        config.slack.chat_update(
            channel=channel_id,
            ts=message_ts,
            text="~Calendar conflict~ — _Ignored_ ✓",
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "~Calendar conflict~ — _Ignored_ ✓"},
                }
            ],
        )
    except Exception:
        pass  # best-effort; don't crash if update fails


def open_resolve_modal(payload: dict, action: dict):
    """
    Open a modal asking which of the two conflicting events to reschedule.
    MUST be called directly in _dispatch — trigger_id expires in 3 seconds.
    """
    trigger_id   = payload.get("trigger_id", "")
    channel_id   = payload.get("channel", {}).get("id", "")
    message_ts   = payload.get("message", {}).get("ts", "")
    conflict     = json.loads(action.get("value", "{}"))
    event_a      = conflict["event_a"]
    event_b      = conflict["event_b"]

    metadata = json.dumps({
        "channel_id": channel_id,
        "message_ts": message_ts,
        "event_a":    event_a,
        "event_b":    event_b,
    })

    def _label(info: dict) -> str:
        start = datetime.fromisoformat(info["start"])
        end   = datetime.fromisoformat(info["end"])
        return (
            f"{info['title']}  "
            f"({start.strftime('%a %-d %b, %-I:%M %p')}–{end.strftime('%-I:%M %p')})"
        )

    config.slack.views_open(
        trigger_id=trigger_id,
        view={
            "type":             "modal",
            "callback_id":      "resolve_conflict_submit",
            "private_metadata": metadata,
            "title":            {"type": "plain_text", "text": "Resolve Conflict"},
            "submit":           {"type": "plain_text", "text": "Generate Email Draft"},
            "close":            {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Which meeting would you like to reschedule?",
                    },
                },
                {
                    "type":     "input",
                    "block_id": "move_choice_block",
                    "label":    {"type": "plain_text", "text": "Meeting to reschedule"},
                    "element": {
                        "type":      "radio_buttons",
                        "action_id": "move_choice",
                        "options": [
                            {
                                "text":  {"type": "plain_text", "text": _label(event_a)},
                                "value": "event_a",
                            },
                            {
                                "text":  {"type": "plain_text", "text": _label(event_b)},
                                "value": "event_b",
                            },
                        ],
                    },
                },
            ],
        },
    )


# ── Modal submission handler ──────────────────────────────────────────────────

def handle_resolve_submit(payload: dict):
    """
    Generate a rescheduling email draft and post it to the channel.
    Runs in a background thread (spawned by socket_listener).
    """
    view       = payload["view"]
    metadata   = json.loads(view["private_metadata"])
    channel_id = metadata["channel_id"]
    message_ts = metadata.get("message_ts", "")
    event_a    = metadata["event_a"]
    event_b    = metadata["event_b"]

    choice = (
        view["state"]["values"]
            ["move_choice_block"]
            ["move_choice"]
            ["selected_option"]["value"]
    )
    event_to_move = event_a if choice == "event_a" else event_b
    event_staying = event_b if choice == "event_a" else event_a

    # Update the original alert to show we're resolving it
    if message_ts:
        try:
            config.slack.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"~Calendar conflict~ — _Rescheduling: *{event_to_move['title']}*_ ✓",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"~Calendar conflict~ — "
                                f"_Rescheduling: *{event_to_move['title']}*_ ✓"
                            ),
                        },
                    }
                ],
            )
        except Exception:
            pass

    # Build availability text for the email, treating the moved event's slot as free
    avail_text = _build_rescheduling_availability(event_to_move)

    try:
        draft = _draft_email_with_claude(event_to_move, event_staying, avail_text)
    except Exception as e:
        config.slack.chat_postMessage(
            channel=channel_id,
            text=f"⚠️ Could not generate email draft: {e}",
        )
        return

    config.slack.chat_postMessage(
        channel=channel_id,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✉️ *Rescheduling email draft — _{event_to_move['title']}_*",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```\n{draft}\n```"},
            },
        ],
        text=f"Email draft: rescheduling '{event_to_move['title']}'",
    )


# ── Availability for rescheduling emails ──────────────────────────────────────

def _build_rescheduling_availability(event_to_move: dict) -> str:
    """
    Compute free slots for the rest of this week, treating the event being
    moved as already cancelled (so its slot appears free).

    Same-day logic:
      - Before noon  → suggest afternoon slots at least 3 hours from now
                       (gives people time to respond before the slot arrives).
      - After noon   → skip today entirely; start from the next working day.

    If today is Thursday or Friday, also includes Monday–Tuesday of next week
    as a fallback range, clearly labelled.
    """
    now_toronto = datetime.now(TORONTO_TZ)
    today       = now_toronto.date()
    weekday     = today.weekday()   # 0=Mon … 4=Fri
    friday      = today + timedelta(days=(4 - weekday))

    # Earliest date we're willing to offer slots on
    if now_toronto.hour >= 12:
        # After noon — skip today; find the next weekday
        avail_start = today + timedelta(days=1)
        while avail_start.weekday() >= 5:   # skip Sat/Sun
            avail_start += timedelta(days=1)
    else:
        avail_start = today

    # The event being moved frees up its own time slot
    ev_start = datetime.fromisoformat(event_to_move["start"])
    ev_end   = datetime.fromisoformat(event_to_move["end"])

    def _avail_for_range(start: date, end: date) -> str:
        if start > end:
            return "  (no remaining slots in this range)"
        try:
            busy = fetch_busy_blocks(start, end)
        except Exception:
            return "  (could not fetch calendar)"

        # Remove the event being rescheduled (its slot is now free)
        for d in busy:
            busy[d] = [
                (bs, be) for (bs, be) in busy[d]
                if not (bs >= ev_start and be <= ev_end)
            ]

        # Before-noon case: block out everything before now + 3 hours on today
        if start == today and today in busy:
            cutoff  = now_toronto + timedelta(hours=3)
            day_ws  = TORONTO_TZ.localize(datetime.combine(today, WORK_START))
            if cutoff > day_ws:
                busy[today] = [(day_ws, cutoff)] + busy[today]

        return _build_summary(busy, min_duration=timedelta(minutes=30))

    parts = []
    if avail_start <= friday:
        parts.append(f"This week:\n{_avail_for_range(avail_start, friday)}")

    if weekday >= 3:   # Thursday or Friday → offer early next week too
        next_mon = friday + timedelta(days=3)
        next_tue = next_mon + timedelta(days=1)
        parts.append(
            f"Early next week (if this week doesn't work):\n"
            f"{_avail_for_range(next_mon, next_tue)}"
        )

    return "\n\n".join(parts) if parts else "  (no available slots found)"


# ── Email draft generation ────────────────────────────────────────────────────

_EMAIL_SYSTEM = (
    "You are a professional email assistant. "
    "Write a polite, concise email informing meeting participants that a scheduling "
    "conflict has arisen and the meeting needs to be rescheduled. "
    "Include the provided availability times as alternatives. "
    "If early-next-week times are provided, present them as a secondary fallback only. "
    "Use the exact greeting provided — do not change it. "
    "Do not include a closing apology or any second expression of regret after the "
    "availability list. A single upfront acknowledgement is enough. "
    "Sign the email 'Cheers, Chris'. "
    "Do not include a subject line in the body — output the subject on a separate first "
    "line formatted exactly as 'Subject: ...' followed by a blank line, then the body."
)

_EMAIL_TEMPLATE = """\
I need to reschedule the following meeting due to a calendar conflict:

MEETING TO RESCHEDULE:
  Title:        {title}
  Date/Time:    {when}
  Participants: {participants}

CONFLICT WITH:
  Title:        {conflict_title}
  Date/Time:    {conflict_when}

MY AVAILABILITY to offer as alternatives:
{availability}

Open the email with exactly this greeting: "{greeting}"
Write the rescheduling email now.
"""


def _fmt_when(info: dict) -> str:
    start = datetime.fromisoformat(info["start"])
    end   = datetime.fromisoformat(info["end"])
    return (
        f"{start.strftime('%A, %B %-d')} "
        f"{start.strftime('%-I:%M %p')}–{end.strftime('%-I:%M %p')}"
    )


def _fmt_participants(info: dict) -> str:
    if not info["attendees"]:
        return "Unknown (no attendees listed in the calendar event)"
    return ", ".join(
        f"{a['name']} <{a['email']}>" if a["name"] else a["email"]
        for a in info["attendees"]
    )


def _make_greeting(info: dict) -> str:
    """
    Build an appropriate opening greeting based on the number of attendees.
    1 person  → "Hi [First name]"  (or "Hi" if name unknown)
    2 people  → "Hi [First] and [Second]"
    3+ people → "Hi all"
    """
    attendees = info["attendees"]
    if not attendees:
        return "Hi"

    first_names = [a["name"].split()[0] for a in attendees if a.get("name")]

    if len(attendees) == 1:
        return f"Hi {first_names[0]}" if first_names else "Hi"
    if len(attendees) == 2:
        if len(first_names) == 2:
            return f"Hi {first_names[0]} and {first_names[1]}"
        return "Hi"
    return "Hi all"


def _draft_email_with_claude(
    event_to_move: dict,
    event_staying: dict,
    availability: str,
) -> str:
    response = call_with_retries(
        config.claude.messages.create,
        model="claude-sonnet-4-5-20250929",
        max_tokens=700,
        system=_EMAIL_SYSTEM,
        messages=[{"role": "user", "content": _EMAIL_TEMPLATE.format(
            title=event_to_move["title"],
            when=_fmt_when(event_to_move),
            participants=_fmt_participants(event_to_move),
            conflict_title=event_staying["title"],
            conflict_when=_fmt_when(event_staying),
            availability=availability,
            greeting=_make_greeting(event_to_move),
        )}],
    )
    return response.content[0].text.strip()
