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
import os
import smtplib
import uuid
import pytz
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from icalendar import Calendar, Event as CalEvent
from . import config
from .slack_helpers import get_channel_id, get_unprocessed_messages, mark_processed, extract_email_text

TORONTO_TZ = pytz.timezone("America/Toronto")

# ── Claude prompts ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a calendar event extraction assistant. Given an email (which may be "
    "forwarded and include headers), extract the details of the calendar event being "
    "described and return ONLY a JSON object — no markdown, no explanation. "
    "If information is ambiguous or missing, apply sensible defaults."
)

PROMPT_TEMPLATE = """\
Today's date is {today}. Use this to resolve any ambiguous or year-less dates.

Analyze this email and extract calendar event details.

EMAIL:
{email_text}

Return exactly this JSON (no extra keys, no markdown fences):
{{
  "title": "<concise event title>",
  "start": "<YYYY-MM-DDTHH:MM:SS or YYYY-MM-DD for all-day>",
  "end":   "<YYYY-MM-DDTHH:MM:SS or YYYY-MM-DD for all-day>",
  "all_day": <true or false>,
  "location": "<location string or null>",
  "description": "<see description rules below>"
}}

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

def analyze_email_for_event(email_text: str) -> dict:
    response = config.claude.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": PROMPT_TEMPLATE.format(
                today=date.today().isoformat(),
                email_text=email_text,
            ),
        }],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
    return json.loads(raw)


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
    msg["From"]    = os.environ["SMTP_USER"]
    msg["To"]      = os.environ["CALENDAR_EMAIL"]
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

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)


# ── Pipeline ──────────────────────────────────────────────────────────────────

def process_calendar_channel() -> int:
    """Process all unhandled emails in the calendar Slack channel. Returns invites sent."""
    channel_id = get_channel_id(config.SLACK_CALENDAR_CHANNEL)
    messages   = get_unprocessed_messages(channel_id)

    if not messages:
        print("  No unprocessed messages.")
        return 0

    print(f"  Found {len(messages)} unprocessed message(s).")
    created = 0

    for msg in messages:
        text = extract_email_text(msg)

        if len(text) < 20:
            print(f"  Skipping short message: {text[:40]!r}")
            continue

        print(f"\n  ▶ Analyzing: {text[:80]}…")
        try:
            event = analyze_email_for_event(text)
            label = "all-day" if event.get("all_day") else f"{event['start']} → {event['end']}"
            print(f"    Title:    {event['title']}")
            print(f"    When:     {label}")
            if event.get("location"):
                print(f"    Location: {event['location']}")

            ics = create_ics(event)
            send_calendar_invite(event, ics)
            mark_processed(channel_id, msg["ts"])

            config.slack.chat_postMessage(
                channel=channel_id,
                thread_ts=msg["ts"],
                text=(
                    f"📅 *Calendar invite sent*\n"
                    f"• *{event['title']}*\n"
                    f"• {label}"
                    + (f"  •  {event['location']}" if event.get("location") else "")
                ),
            )
            print("    ✅ Calendar invite sent.")
            created += 1

        except json.JSONDecodeError as e:
            print(f"    ✗ Claude returned invalid JSON: {e}")
        except smtplib.SMTPException as e:
            print(f"    ✗ SMTP error: {e}")
        except Exception as e:
            print(f"    ✗ Unexpected error: {e}")

    print(f"\n  Done. Sent {created} calendar invite(s) from {len(messages)} email(s).")
    return created
