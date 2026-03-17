"""
watch_date_handler.py — Date-based watch reminders for Obsidian notes.

When a note is created from a time-sensitive source (e.g. a grant announcement,
a conference CFP, an invitation with a decision deadline), Claude extracts key
dates into a ``watch_dates`` YAML frontmatter list.

This module:
  1. Provides a daily scan job (``scan_and_remind``) that checks all notes for
     upcoming watch dates and posts reminders to #auto-reminders.
  2. Posts rich Slack Block Kit messages with action buttons: Dismiss,
     Snooze (1 / 2 / 3 / 7 days), Create Task (Auto), Create Task (Manual).
  3. Handles the resulting block_actions payloads (``handle_watch_date_action``).
  4. Persists status changes back into the note's YAML frontmatter.

Reminder cadence:
  - 14 days before: first reminder.
  - 7 days before:  second reminder.
  - 6 days and under: daily escalation reminders until dismissed or date passes.
  Snooze overrides the next scheduled reminder until the snooze period expires.

Frontmatter schema (per watch_date entry, as written by the scan job):
  label:         short description (e.g. "LOI deadline")
  date:          ISO date string, e.g. "2026-05-15"
  status:        active | snoozed | dismissed   (default: active if absent)
  snooze_until:  ISO date string, or null
  last_reminded: ISO date string of most recent reminder, or null
"""

import json
import re
import threading
from datetime import date, timedelta
from pathlib import Path

import yaml

from . import activity_log
from . import config
from .tasks import analyze_with_claude, create_motion_task


# ── Reminder thresholds ───────────────────────────────────────────────────────

# Standard checkpoints: fire when days_until equals one of these values.
_STANDARD_THRESHOLDS: frozenset[int] = frozenset({14, 7})

# Daily escalation: fire every day when this many days (or fewer) remain.
_DAILY_ESCALATION_DAYS: int = 6   # covers days 6, 5, 4, 3, 2, 1

# Snooze options presented in the reminder message.
_SNOOZE_OPTIONS: list[tuple[str, int]] = [
    ("1 day",  1),
    ("2 days", 2),
    ("3 days", 3),
    ("7 days", 7),
]


# ── YAML frontmatter helpers ──────────────────────────────────────────────────

_FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n?', re.DOTALL)


def _read_note_frontmatter(path: Path) -> tuple[dict, str]:
    """
    Read a note file and return (frontmatter_dict, full_text).
    Returns ({}, full_text) if no valid YAML frontmatter is found.
    """
    text = path.read_text(encoding='utf-8')
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    return fm, text


def _write_note_frontmatter(path: Path, fm: dict, full_text: str):
    """
    Serialise fm back to YAML, replace the frontmatter block in full_text,
    and write the result to path.
    """
    fm_yaml = yaml.dump(
        fm,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    new_text = _FM_RE.sub(f'---\n{fm_yaml}---\n', full_text, count=1)
    path.write_text(new_text, encoding='utf-8')


def _get_watch_dates(fm: dict) -> list[dict]:
    """Return the watch_dates list from frontmatter, normalised to a list of dicts."""
    wds = fm.get('watch_dates') or []
    if not isinstance(wds, list):
        return []
    return [e for e in wds if isinstance(e, dict)]


def _date_to_str(value) -> str | None:
    """Coerce a value that may be a datetime.date or string to an ISO string."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    try:
        # Validate that it parses as a date
        date.fromisoformat(str(value))
        return str(value)
    except (ValueError, TypeError):
        return None


# ── Reminder logic ────────────────────────────────────────────────────────────

def _should_remind(entry: dict, today: date) -> bool:
    """
    Return True if a reminder should fire for this watch_date entry today.

    Rules:
    - dismissed:     never remind.
    - snoozed:       only remind once snooze_until <= today.
    - Fire on standard thresholds (14 or 7 days out).
    - Fire every day when within _DAILY_ESCALATION_DAYS of the deadline.
    - Never fire more than once per calendar day (last_reminded tracks this).
    - Never fire for dates that have already passed.
    """
    if entry.get('status') == 'dismissed':
        return False

    raw_date = _date_to_str(entry.get('date'))
    if raw_date is None:
        return False

    try:
        watch_date = date.fromisoformat(raw_date)
    except ValueError:
        return False

    days_until = (watch_date - today).days
    if days_until < 0:
        return False  # date has passed

    # Check if still within a snooze window
    if entry.get('status') == 'snoozed':
        snooze_raw = _date_to_str(entry.get('snooze_until'))
        if snooze_raw:
            try:
                if date.fromisoformat(snooze_raw) > today:
                    return False  # still snoozed
            except ValueError:
                pass
        # Snooze expired — fall through to normal reminder logic

    # Never double-remind on the same calendar day
    last_raw = _date_to_str(entry.get('last_reminded'))
    if last_raw:
        try:
            if date.fromisoformat(last_raw) >= today:
                return False
        except ValueError:
            pass

    # Standard checkpoints
    if days_until in _STANDARD_THRESHOLDS:
        return True

    # Daily escalation zone
    if 0 < days_until <= _DAILY_ESCALATION_DAYS:
        return True

    return False


# ── Block Kit message building ────────────────────────────────────────────────

def _build_btn_value(note_name: str, date_label: str, date_str: str) -> str:
    """
    Build the JSON value stored in action buttons.
    Uses note filename (not full path) to stay within Slack's 2000-char limit
    and to remain portable if the notes directory is moved.
    """
    return json.dumps({
        "note_name":  note_name,
        "date_label": date_label[:40],   # guard against very long labels
        "date":       date_str,
    })


def _post_reminder(
    note_path: Path,
    entry: dict,
    days_until: int,
    reminders_channel_id: str,
):
    """Post a rich Block Kit reminder message to the reminders channel."""
    label    = entry.get('label', 'Deadline')
    date_str = _date_to_str(entry.get('date')) or ''
    btn_val  = _build_btn_value(note_path.name, label, date_str)

    if days_until == 0:
        timing = "⚠️ *today*"
    elif days_until == 1:
        timing = "*tomorrow*"
    else:
        timing = f"in *{days_until} days*"

    header = (
        f"⏰ *{label}* — {timing} ({date_str})\n"
        f"📄 {note_path.stem}"
    )

    # Snooze dropdown — each option embeds the note_name + date context so the
    # handler can locate the note without additional lookups.
    snooze_options = [
        {
            "text":  {"type": "plain_text", "text": f"Snooze {opt_label}"},
            "value": json.dumps({
                "days":       opt_days,
                "note_name":  note_path.name,
                "date_label": label[:40],
                "date":       date_str,
            }),
        }
        for opt_label, opt_days in _SNOOZE_OPTIONS
    ]

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type":      "button",
                    "text":      {"type": "plain_text", "text": "✅ Dismiss"},
                    "action_id": "wd_dismiss",
                    "style":     "danger",
                    "value":     btn_val,
                },
                {
                    "type":        "static_select",
                    "placeholder": {"type": "plain_text", "text": "💤 Snooze…"},
                    "action_id":   "wd_snooze",
                    "options":     snooze_options,
                },
                {
                    "type":      "button",
                    "text":      {"type": "plain_text", "text": "📋 Create Task (Auto)"},
                    "action_id": "wd_task_auto",
                    "value":     btn_val,
                },
                {
                    "type":      "button",
                    "text":      {"type": "plain_text", "text": "🔗 Create Task (Manual)"},
                    "action_id": "wd_task_manual",
                    "value":     btn_val,
                },
            ],
        },
    ]

    config.slack.chat_postMessage(
        channel=reminders_channel_id,
        text=f"⏰ Reminder: {label} — {timing}",   # plain-text fallback
        blocks=blocks,
    )


# ── Daily scan job ────────────────────────────────────────────────────────────

def scan_and_remind():
    """
    Scan all notes for upcoming watch dates and post reminders as needed.
    Called daily by the scheduler.  Safe to call manually for debugging.
    """
    reminders_channel_id = config.SLACK_REMINDERS_CHANNEL
    if not reminders_channel_id:
        print("ℹ️  watch_date_handler: SLACK_REMINDERS_CHANNEL not set — skipping scan.")
        return

    notes_dir = Path(config.NOTES_OUTPUT_PATH).expanduser().resolve()
    if not notes_dir.exists():
        print(f"ℹ️  watch_date_handler: notes dir {notes_dir} does not exist — skipping scan.")
        return

    today         = date.today()
    reminded      = 0
    notes_checked = 0

    for note_path in sorted(notes_dir.glob('*.md')):
        notes_checked += 1
        try:
            fm, full_text = _read_note_frontmatter(note_path)
            watch_dates   = _get_watch_dates(fm)
            if not watch_dates:
                continue

            updated = False
            for entry in watch_dates:
                raw_date = _date_to_str(entry.get('date'))
                if raw_date is None:
                    continue
                try:
                    watch_date = date.fromisoformat(raw_date)
                except ValueError:
                    continue

                days_until = (watch_date - today).days

                if not _should_remind(entry, today):
                    continue

                _post_reminder(note_path, entry, days_until, reminders_channel_id)
                activity_log.record(
                    "reminder_sent",
                    label=entry.get("label", ""),
                    date=raw_date,
                    days_until=days_until,
                    note_name=note_path.name,
                )
                entry['last_reminded'] = today.isoformat()
                entry['snooze_until']  = None
                # Clear snoozed status now that the snooze has expired
                if entry.get('status') == 'snoozed':
                    entry['status'] = 'active'
                updated = True
                reminded += 1

            if updated:
                fm['watch_dates'] = watch_dates
                _write_note_frontmatter(note_path, fm, full_text)

        except Exception as e:
            print(f"⚠️  watch_date_handler: error processing {note_path.name}: {e}")

    print(
        f"🔔 watch_date_handler: checked {notes_checked} note(s), "
        f"sent {reminded} reminder(s)."
    )


# ── Block action handlers ─────────────────────────────────────────────────────

def _find_note(note_name: str) -> Path | None:
    """Locate a note file by filename within NOTES_OUTPUT_PATH."""
    notes_dir = Path(config.NOTES_OUTPUT_PATH).expanduser().resolve()
    path = notes_dir / note_name
    return path if path.exists() else None


def _update_entry(note_path: Path, date_label: str, **updates) -> bool:
    """
    Find the watch_date entry with matching label in note_path and apply **updates.
    Returns True if the entry was found and the file was updated, False otherwise.
    """
    fm, full_text = _read_note_frontmatter(note_path)
    watch_dates   = _get_watch_dates(fm)

    for entry in watch_dates:
        if entry.get('label', '')[:40] == date_label[:40]:
            entry.update(updates)
            fm['watch_dates'] = watch_dates
            _write_note_frontmatter(note_path, fm, full_text)
            return True
    return False


def _ack_action(payload: dict, text: str):
    """Post an ephemeral acknowledgement replacing the original reminder message."""
    channel_id  = (payload.get('channel') or {}).get('id', '')
    message_ts  = (payload.get('message') or {}).get('ts', '')
    user_id     = (payload.get('user') or {}).get('id', '')
    if channel_id and message_ts:
        try:
            config.slack.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=text,
                blocks=[],
            )
        except Exception:
            # Fall back to a new message if update fails
            if channel_id:
                config.slack.chat_postMessage(channel=channel_id, text=text)


def _handle_dismiss(payload: dict, data: dict):
    note_path = _find_note(data['note_name'])
    if note_path:
        _update_entry(note_path, data['date_label'], status='dismissed')
    activity_log.record(
        "reminder_dismissed",
        label=data.get("date_label", ""),
        note_name=data.get("note_name", ""),
    )
    _ack_action(payload, f"✅ Dismissed — *{data['date_label']}* will not be surfaced again.")


def _handle_snooze(payload: dict, data: dict):
    days        = int(data.get('days', 7))
    snooze_date = (date.today() + timedelta(days=days)).isoformat()
    note_path   = _find_note(data['note_name'])
    if note_path:
        _update_entry(
            note_path,
            data['date_label'],
            status='snoozed',
            snooze_until=snooze_date,
        )
    activity_log.record(
        "reminder_snoozed",
        label=data.get("date_label", ""),
        snooze_days=days,
        note_name=data.get("note_name", ""),
    )
    day_word = "day" if days == 1 else "days"
    _ack_action(
        payload,
        f"💤 Snoozed for {days} {day_word} — *{data['date_label']}* "
        f"will resurface on {snooze_date}.",
    )


def _handle_task_auto(payload: dict, data: dict):
    """Read the note, ask Claude to extract tasks, create them in Motion."""
    note_path = _find_note(data['note_name'])
    if not note_path:
        _ack_action(payload, f"⚠️ Could not locate note: {data['note_name']}")
        return

    try:
        note_text  = note_path.read_text(encoding='utf-8')
        # Provide the deadline context so Claude can set a sensible due date
        task_input = (
            f"Note content:\n{note_text}\n\n"
            f"Key deadline to use as the due date: {data['date_label']} — {data['date']}"
        )
        tasks = analyze_with_claude(task_input)
        # Override dueDate with the watched deadline
        for task in tasks:
            task['dueDate'] = data['date']

        created = []
        for task in tasks:
            create_motion_task(task)
            created.append(task['name'])

        # Dismiss the watch_date — it has been acted on
        _update_entry(note_path, data['date_label'], status='dismissed')

        activity_log.record(
            "reminder_task_created",
            label=data.get("date_label", ""),
            note_name=data.get("note_name", ""),
            task_count=len(created),
        )
        task_list = "\n".join(f"  • {n}" for n in created)
        _ack_action(
            payload,
            f"📋 Created {len(created)} Motion task(s) from *{data['note_name']}*:\n{task_list}",
        )

    except Exception as e:
        _ack_action(payload, f"⚠️ Task creation failed: {e}")


def _handle_task_manual(payload: dict, data: dict):
    """Tell the user which note to open; they'll create the task themselves."""
    note_path = _find_note(data['note_name'])
    if note_path:
        _update_entry(note_path, data['date_label'], status='dismissed')
    _ack_action(
        payload,
        f"🔗 Open your note to create the task manually:\n"
        f"  *{data['note_name']}*\n"
        f"  Deadline: {data['date_label']} — {data['date']}",
    )


def handle_watch_date_action(payload: dict, action: dict):
    """
    Route a block_action from the reminders channel to the appropriate handler.
    Called in a background thread from socket_listener._dispatch.
    """
    action_id = action.get('action_id', '')

    # Button actions carry their data in action['value'] (JSON string).
    # The snooze static_select carries data in action['selected_option']['value'].
    if action_id == 'wd_snooze':
        raw = (action.get('selected_option') or {}).get('value', '{}')
    else:
        raw = action.get('value', '{}')

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        print(f"⚠️  watch_date_handler: could not parse action value: {raw!r}")
        return

    if action_id == 'wd_dismiss':
        _handle_dismiss(payload, data)
    elif action_id == 'wd_snooze':
        _handle_snooze(payload, data)
    elif action_id == 'wd_task_auto':
        threading.Thread(
            target=_handle_task_auto,
            args=(payload, data),
            daemon=True,
        ).start()
    elif action_id == 'wd_task_manual':
        _handle_task_manual(payload, data)
