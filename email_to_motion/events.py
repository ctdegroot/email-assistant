"""
events.py — Calendar event extraction pipeline.

Responsibilities:
  1. Send email text to Claude and get back structured event JSON.
  2. Build a .ics file from the extracted data.
  3. Email the .ics to the configured calendar address via SMTP.
  4. Post a confirmation back to Slack.
  5. Orchestrate the above for every unprocessed message in the calendar channel.

Note: this module is named 'events' rather than 'calendar' to avoid shadowing
Python's standard-library 'calendar' module.
"""

import json
import logging
import smtplib
import uuid
import pytz
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from icalendar import Calendar, Event as CalEvent
from . import config
from .slack_helpers import get_channel_id, get_unprocessed_messages, mark_processed, extract_email_text
from .utils import call_with_retries, parse_claude_json
from . import activity_log

log = logging.getLogger(__name__)

TORONTO_TZ = pytz.timezone("America/Toronto")

# ── Channel ID cache ──────────────────────────────────────────────────────────
_channel_id: str = ""


def _get_channel_id() -> str:
    global _channel_id
    if not _channel_id:
        _channel_id = get_channel_id(config.SLACK_CALENDAR_CHANNEL)
    return _channel_id

# ── Claude prompts ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a calendar event extraction assistant. Given an email (which may be "
    "forwarded and include headers), extract calendar events and return ONLY a JSON array "
    "of event objects — no markdown, no explanation. "
    "If information is ambiguous or missing, apply sensible defaults. "
    "Always return an array, even when there is only one event. "
    "When the forwarding note specifies which items should go to the calendar, treat that "
    "as an authoritative whitelist — only those items should appear in the output."
)

PROMPT_TEMPLATE = """\
Today's date is {today}. Use this to resolve any ambiguous or year-less dates.

Analyze this email and extract all calendar event details.

EMAIL:
{email_text}

Return a JSON array of zero or more event objects. Each object has exactly these keys \
(no extras, no markdown fences around the array):
[
  {{
    "title": "<concise event title>",
    "start": "<YYYY-MM-DDTHH:MM:SS or YYYY-MM-DD for all-day>",
    "end":   "<YYYY-MM-DDTHH:MM:SS or YYYY-MM-DD for all-day>",
    "all_day": <true or false>,
    "location": "<location string or null>",
    "description": "<see description rules below>"
  }}
]

IMPORTANT — Forwarding note (read this first):
  If the email begins with a note from the person who forwarded it (before any "From:",
  "Fwd:", or "------" separator), that note is an authoritative whitelist of what belongs
  in the calendar.

  Step 1: Read the forwarding note and identify exactly which items it names for the calendar
          (e.g. "place hold in calendar for the public lecture and exam" → whitelist =
          {{public lecture, exam}}).
  Step 2: Create calendar entries ONLY for those whitelisted items. Everything else —
          including anything the note routes to Motion, any deadline-driven task, any item
          the note doesn't mention at all — must be excluded.
  Step 3: If the forwarding note does not explicitly mention the calendar at all, fall back
          to your own judgement about which items are timed events vs. tasks.

  Example: Note says "Place hold in calendar for the public lecture and exam. Create a
  motion event for reviewing the thesis." → Output contains only the public lecture and
  exam events. The thesis review is excluded even if the email gives it a specific date
  and time, because the note routes it to Motion.

NEVER create calendar entries for:
  - Deadline-driven tasks (e.g. "review document by April 13" — this has a deadline, not
    a start time people block for; it belongs in Motion).
  - Items the forwarding note says should go to Motion or are described as tasks.
  - Anything not on the forwarding note's calendar whitelist (when such a whitelist exists).

Event-splitting rules:
  - Create one entry per distinct event. If the email describes two events at different
    times (e.g. a public lecture and a thesis examination), return two entries.
  - Do NOT split a single event into multiple entries; only split when there are genuinely
    separate events with different times, titles, or purposes.

Extraction rules:
  - title: short and descriptive, e.g. "Faculty Meeting" or "PhD Defence — Jane Smith"
  - start/end: use the timezone America/Toronto. If no end time is given, default to
    start + 30 minutes. If only a date is given with no time, set all_day to true and
    use YYYY-MM-DD format.
  - location: include room number, building, address, or video link if mentioned; null if absent.

Description rules:
  - Plain text only (no markdown — this goes into a calendar event).
  - Include everything the attendee needs to know: purpose of the event, agenda, background
    context, relevant people, any preparation required, and links.
  - End with "Source: <sender name/email> — <original subject line>".
"""


# ── Claude analysis ───────────────────────────────────────────────────────────

def analyze_email_for_events(email_text: str) -> list[dict]:
    """Return a list of event dicts extracted from the email (always a list)."""
    response = call_with_retries(
        config.claude.messages.create,
        model="claude-sonnet-4-5-20250929",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": PROMPT_TEMPLATE.format(
                today=date.today().isoformat(),
                email_text=email_text,
            ),
        }],
    )
    result = parse_claude_json(response.content[0].text)
    # Normalise: always return a list even if Claude returns a bare dict
    if isinstance(result, dict):
        result = [result]
    return result


# ── ICS generation ────────────────────────────────────────────────────────────

def create_ics(event: dict) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Email to Calendar//EN")
    cal.add("version", "2.0")
    cal.add("method", "REQUEST")

    vevent = CalEvent()
    vevent.add("uid", str(uuid.uuid4()))
    vevent.add("summary", event["title"])
    vevent.add("dtstamp", datetime.now(pytz.utc))

    if event.get("all_day"):
        start = date.fromisoformat(event["start"][:10])
        end   = date.fromisoformat(event["end"][:10]) + timedelta(days=1)  # ICS end is exclusive
        vevent.add("dtstart", start)
        vevent.add("dtend",   end)
    else:
        start = datetime.fromisoformat(event["start"])
        end   = datetime.fromisoformat(event["end"])
        if start.tzinfo is None:
            start = TORONTO_TZ.localize(start)
        if end.tzinfo is None:
            end = TORONTO_TZ.localize(end)
        vevent.add("dtstart", start)
        vevent.add("dtend",   end)

    if event.get("location"):
        vevent.add("location", event["location"])
    if event.get("description"):
        vevent.add("description", event["description"])

    cal.add_component(vevent)
    return cal.to_ical()


# ── SMTP delivery ─────────────────────────────────────────────────────────────

def send_calendar_invite(event: dict, ics_bytes: bytes):
    """Send the .ics as an email attachment via Gmail SMTP (port 587 / STARTTLS)."""
    msg = MIMEMultipart("mixed")
    msg["From"]    = config.SMTP_USER
    msg["To"]      = config.CALENDAR_EMAIL
    msg["Subject"] = f"Calendar: {event['title']}"

    lines = [f"Event:    {event['title']}",
             f"Start:    {event['start']}",
             f"End:      {event['end']}"]
    if event.get("location"):
        lines.append(f"Location: {event['location']}")
    msg.attach(MIMEText("\n".join(lines), "plain"))

    # Content-Type: text/calendar; method=REQUEST triggers Outlook's Accept/Decline UI
    cal_part = MIMEText(ics_bytes.decode("utf-8"), "calendar", "utf-8")
    cal_part.set_param("method", "REQUEST")
    cal_part.add_header("Content-Disposition", "attachment", filename="event.ics")
    msg.attach(cal_part)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)


# ── Pipeline ──────────────────────────────────────────────────────────────────

def process_calendar_channel() -> int:
    """Process all unhandled emails in the calendar Slack channel. Returns invites sent."""
    channel_id = _get_channel_id()
    messages   = get_unprocessed_messages(channel_id)

    if not messages:
        log.debug("calendar: no unprocessed messages")
        return 0

    log.info("calendar: found %d unprocessed message(s)", len(messages))
    created = 0

    for msg in messages:
        text = extract_email_text(msg)

        if len(text) < 20:
            log.debug("calendar: skipping short message: %r", text[:40])
            continue

        log.info("calendar: analyzing ts=%s — %s…", msg.get("ts"), text[:80])
        try:
            events = analyze_email_for_events(text)
            log.info("calendar: extracted %d event(s)", len(events))
        except json.JSONDecodeError as e:
            log.error("calendar: Claude returned invalid JSON: %s", e)
            continue
        except Exception as e:
            log.error("calendar: extraction error for ts=%s: %s", msg.get("ts"), e, exc_info=True)
            continue

        if not events:
            log.info("calendar: no events to create — marking processed")
            mark_processed(channel_id, msg["ts"])
            config.slack.chat_postMessage(
                channel=channel_id,
                thread_ts=msg["ts"],
                text="ℹ️ No calendar events found in this message.",
            )
            continue

        # Process each event independently — a failure on one should not
        # prevent the others from being sent or the message being marked done.
        sent_lines = []
        error_lines = []
        for event in events:
            label = "all-day" if event.get("all_day") else f"{event['start']} → {event['end']}"
            log.info("calendar: processing — title=%r when=%s", event.get("title"), label)
            try:
                ics = create_ics(event)
                send_calendar_invite(event, ics)
                activity_log.record(
                    "calendar_event",
                    title=event.get("title"),
                    start=event.get("start"),
                    all_day=event.get("all_day", False),
                    source="email",
                )
                log.info("calendar: invite sent — %r", event.get("title"))
                created += 1
                line = f"• *{event['title']}* — {label}"
                if event.get("location"):
                    line += f"  •  {event['location']}"
                sent_lines.append(line)
            except (smtplib.SMTPException, OSError) as e:
                log.error("calendar: SMTP error for %r: %s", event.get("title"), e)
                error_lines.append(f"• ⚠️ *{event['title']}* — send failed: {e}")
            except Exception as e:
                log.error("calendar: error for %r: %s", event.get("title"), e, exc_info=True)
                error_lines.append(f"• ⚠️ *{event['title']}* — error: {e}")

        # Mark processed regardless of per-event errors to avoid duplicate sends on retry
        mark_processed(channel_id, msg["ts"])

        reply_parts = []
        if sent_lines:
            n = len(sent_lines)
            reply_parts.append(f"📅 *{n} calendar invite{'s' if n > 1 else ''} sent*\n" + "\n".join(sent_lines))
        if error_lines:
            reply_parts.append("*Failed to send:*\n" + "\n".join(error_lines))
        config.slack.chat_postMessage(
            channel=channel_id,
            thread_ts=msg["ts"],
            text="\n\n".join(reply_parts) if reply_parts else "ℹ️ No calendar invites sent.",
        )

    log.info("calendar: done — sent %d invite(s) from %d email(s)", created, len(messages))
    return created
