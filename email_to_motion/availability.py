"""
availability.py — /availability slash command handler.

Pipeline:
  1. Parse the date range from the slash command argument.
  2. Fetch the Outlook ICS calendar from OUTLOOK_ICS_URL.
  3. Expand recurring events with recurring_ical_events.
  4. Compute free slots within working hours (9am–4:30pm Mon–Fri, America/Toronto).
  5. Pass the busy/free summary to Claude for a human-readable email reply.
  6. Post the reply back to the Slack channel.
"""

import re
import requests
import pytz
from datetime import date, datetime, time, timedelta
from icalendar import Calendar
import recurring_ical_events
from dateutil import parser as dateutil_parser
from dateutil.relativedelta import relativedelta
from . import config

TORONTO_TZ    = pytz.timezone("America/Toronto")
WORK_START    = time(9, 0)
WORK_END      = time(16, 30)
TRAVEL_BUFFER = timedelta(minutes=15)

# Keywords that indicate a meeting is virtual and needs no travel buffer.
_VIRTUAL_KEYWORDS = (
    "zoom.us", "teams.microsoft.com", "microsoft teams",
    "meet.google.com", "google meet", "webex.com", "cisco webex",
    "gotomeeting", "bluejeans", "whereby.com", "around.co",
)


# ── Date range parsing ────────────────────────────────────────────────────────

def parse_date_range(text: str) -> tuple[date, date]:
    """
    Parse a human date range string into (start_date, end_date).

    Handles formats such as:
      "Mar 1-15"            → same-month compact range
      "March 1 to March 15" → explicit range with 'to'
      "Mar 1 - Mar 15"      → explicit range with spaced hyphen
    Dates without a year are assumed to be the next future occurrence.
    """
    text = text.strip()
    # Normalise separators
    text = re.sub(r'\s*[–—]\s*', ' - ', text)          # en/em dash → " - "
    text = re.sub(r'\s+to\s+', ' - ', text, flags=re.IGNORECASE)

    today = date.today()

    # Case 1: "Mar 1-15" — month + compact day1-day2 (no spaces around hyphen)
    m = re.match(r'^([A-Za-z]+)\s+(\d{1,2})-(\d{1,2})$', text)
    if m:
        start = _parse_month_day(m.group(1), int(m.group(2)), today)
        end   = start.replace(day=int(m.group(3)))
        if end < start:
            end = end + relativedelta(months=1)
        return start, end

    # Case 2: split on " - " (covers "Mar 1 - Mar 15", "March 1 - 15", etc.)
    parts = text.split(' - ', maxsplit=1)
    if len(parts) == 2:
        start = _parse_flexible_date(parts[0].strip(), today)
        end   = _parse_flexible_date(parts[1].strip(), today, reference=start)
        return start, end

    raise ValueError(
        f"Could not parse date range from {text!r}. "
        "Try a format like 'Mar 1-15' or 'March 1 to March 15'."
    )


def _parse_month_day(month_str: str, day: int, today: date) -> date:
    try:
        d = dateutil_parser.parse(f"{month_str} {day}").date().replace(year=today.year)
    except ValueError:
        raise ValueError(f"Unrecognised month: {month_str!r}")
    if d < today:
        d = d.replace(year=today.year + 1)
    return d


def _parse_flexible_date(text: str, today: date, reference: date | None = None) -> date:
    """Parse a partial date string; a bare day number inherits the month from reference."""
    if re.match(r'^\d{1,2}$', text):
        if reference is None:
            raise ValueError("A day-only date needs a reference month.")
        d = reference.replace(day=int(text))
        if d < reference:
            d = d + relativedelta(months=1)
        return d
    try:
        parsed = dateutil_parser.parse(text, default=datetime(today.year, 1, 1)).date()
    except ValueError:
        raise ValueError(f"Cannot parse date: {text!r}")
    if parsed < today:
        parsed = parsed.replace(year=today.year + 1)
    return parsed


# ── Virtual meeting detection ─────────────────────────────────────────────────

def _is_virtual(event) -> bool:
    """
    Return True if the event appears to be a virtual meeting (no travel needed).

    Only the LOCATION field is checked — many physical meetings also embed a
    dial-in link in the description body, so checking description would cause
    false positives. An empty/missing location is treated as virtual (no
    physical place to travel to).
    """
    location = str(event.get("LOCATION", "") or "").lower().strip()

    # No location → assume virtual / no travel
    if not location:
        return True

    # Location is a bare URL → virtual
    if location.startswith(("http://", "https://")):
        return True

    # Location contains a known conferencing keyword → virtual
    return any(kw in location for kw in _VIRTUAL_KEYWORDS)


# ── ICS fetching and event extraction ────────────────────────────────────────

def fetch_busy_blocks(
    start_date: date, end_date: date
) -> dict[date, list[tuple[datetime, datetime]]]:
    """
    Download the ICS feed and return a dict mapping each working day in the
    range to a list of (start, end) busy blocks in Toronto time.
    Recurring events are fully expanded by the recurring_ical_events library.
    """
    r = requests.get(config.OUTLOOK_ICS_URL, timeout=15)
    r.raise_for_status()
    cal = Calendar.from_ical(r.content)

    window_start = datetime.combine(start_date, time(0, 0))
    window_end   = datetime.combine(end_date,   time(23, 59, 59))
    events       = recurring_ical_events.of(cal).between(window_start, window_end)

    # Initialise a slot for every working day in the range
    blocks: dict[date, list[tuple[datetime, datetime]]] = {}
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:   # Mon–Fri
            blocks[current] = []
        current += timedelta(days=1)

    for event in events:
        dtstart = event.get("DTSTART")
        dtend   = event.get("DTEND")
        if not dtstart or not dtend:
            continue
        ev_start = _to_toronto(dtstart)
        ev_end   = _to_toronto(dtend)

        # Add travel buffer around physical meetings
        if not _is_virtual(event):
            ev_start = ev_start - TRAVEL_BUFFER
            ev_end   = ev_end   + TRAVEL_BUFFER

        # An event may span multiple working days — add a block to each
        d = ev_start.date()
        while d <= ev_end.date() and d <= end_date:
            if d in blocks:
                day_ws = TORONTO_TZ.localize(datetime.combine(d, WORK_START))
                day_we = TORONTO_TZ.localize(datetime.combine(d, WORK_END))
                busy_start = max(ev_start, day_ws)
                busy_end   = min(ev_end,   day_we)
                if busy_start < busy_end:
                    blocks[d].append((busy_start, busy_end))
            d += timedelta(days=1)

    return blocks


def _to_toronto(val) -> datetime:
    """Coerce an icalendar DTSTART/DTEND value to a Toronto-localised datetime."""
    dt = val.dt
    if isinstance(dt, datetime):
        return dt.astimezone(TORONTO_TZ) if dt.tzinfo else TORONTO_TZ.localize(dt)
    # All-day vDate — treat as midnight start / midnight end
    return TORONTO_TZ.localize(datetime.combine(dt, time(0, 0)))


# ── Free slot computation ─────────────────────────────────────────────────────

def compute_free_slots(
    d: date,
    busy_blocks: list[tuple[datetime, datetime]],
    min_duration: timedelta = timedelta(minutes=30),
) -> list[tuple[datetime, datetime]]:
    """
    Return the free windows within working hours for day d that are at least
    min_duration long. Slot start times are rounded up to the nearest even
    hour or half hour; slots that become too short after rounding are dropped.
    """
    work_start = TORONTO_TZ.localize(datetime.combine(d, WORK_START))
    work_end   = TORONTO_TZ.localize(datetime.combine(d, WORK_END))

    # Clip blocks to working hours and merge overlaps
    merged: list[list[datetime]] = []
    for start, end in sorted(busy_blocks):
        start = max(start, work_start)
        end   = min(end,   work_end)
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])

    # Invert to find free windows, filtering by min_duration
    free: list[tuple[datetime, datetime]] = []
    cursor = work_start
    for busy_start, busy_end in merged:
        if cursor < busy_start:
            slot_start = _round_up_to_half_hour(cursor)
            if slot_start + min_duration <= busy_start:
                free.append((slot_start, busy_start))
        cursor = max(cursor, busy_end)
    if cursor < work_end:
        slot_start = _round_up_to_half_hour(cursor)
        if slot_start + min_duration <= work_end:
            free.append((slot_start, work_end))

    return free


def _round_up_to_half_hour(dt: datetime) -> datetime:
    if dt.minute in (0, 30):
        return dt.replace(second=0, microsecond=0)
    if dt.minute < 30:
        return dt.replace(minute=30, second=0, microsecond=0)
    return (dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


# ── Claude response generation ────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a helpful assistant that formats calendar availability for email replies. "
    "Given a list of free time slots, produce a plain-text availability block ready to "
    "paste into an email. Use a bulleted list (a dash '-' for each day). "
    "Do not include a greeting or sign-off. Do not mention the time zone."
)

PROMPT_TEMPLATE = """\
I am looking to schedule a {duration}-minute meeting. \
My available time slots for the requested period are:

{slots}

Format this as an availability block starting with the line \
"I am available during the following times:" followed by a bulleted list \
(one bullet per day, skip fully booked days). End with exactly this closing sentence: \
"Please let me know what works best for your schedule, and I'll get it on the calendar."
"""


def _build_summary(
    busy_blocks: dict[date, list[tuple[datetime, datetime]]],
    min_duration: timedelta,
) -> str:
    lines = []
    for d in sorted(busy_blocks):
        free = compute_free_slots(d, busy_blocks[d], min_duration=min_duration)
        label = d.strftime("%A, %B %-d")
        if not free:
            lines.append(f"{label}: fully booked")
        else:
            slots = ", ".join(
                f"{s.strftime('%-I:%M %p')}–{e.strftime('%-I:%M %p')}"
                for s, e in free
            )
            lines.append(f"{label}: {slots}")
    return "\n".join(lines)


def _ask_claude(summary: str, duration_minutes: int) -> str:
    response = config.claude.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(
            slots=summary, duration=duration_minutes,
        )}],
    )
    return response.content[0].text.strip()


# ── Entry point called by the socket listener ─────────────────────────────────

def handle_availability_command(text: str, channel_id: str):
    """
    Full pipeline: parse → fetch → compute → Claude → post.

    Accepts an optional duration (in minutes) at the end of the text:
      /availability Mar 10-14        → 30-minute slots (default)
      /availability Mar 10-14 60     → 60-minute slots
    """
    # Strip an optional trailing integer duration, e.g. "Mar 10-14 60"
    duration_minutes = 30
    m = re.search(r'\s+(\d+)\s*$', text)
    if m:
        duration_minutes = int(m.group(1))
        text = text[:m.start()].strip()

    min_duration = timedelta(minutes=duration_minutes)

    try:
        start, end = parse_date_range(text)
    except ValueError as e:
        config.slack.chat_postMessage(channel=channel_id, text=f"⚠️ {e}")
        return

    config.slack.chat_postMessage(
        channel=channel_id,
        text=(
            f"_Checking availability {start.strftime('%b %-d')}–{end.strftime('%b %-d')}"
            f" ({duration_minutes} min)…_"
        ),
    )

    try:
        busy = fetch_busy_blocks(start, end)
    except Exception as e:
        config.slack.chat_postMessage(channel=channel_id, text=f"⚠️ Could not fetch calendar: {e}")
        return

    summary = _build_summary(busy, min_duration=min_duration)

    try:
        reply = _ask_claude(summary, duration_minutes)
    except Exception as e:
        config.slack.chat_postMessage(channel=channel_id, text=f"⚠️ Claude error: {e}")
        return

    config.slack.chat_postMessage(channel=channel_id, text=reply)
