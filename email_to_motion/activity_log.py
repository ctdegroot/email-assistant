"""
activity_log.py — Lightweight JSONL activity log for EmailToMotion.

Records every item successfully processed by the tool so that usage patterns
can be analysed later (dashboards, digests, trend reports, etc.).

Format:
  One JSON object per line (JSONL / JSON Lines), UTF-8, appended atomically.
  Each record has at minimum:
    ts:      ISO-8601 UTC timestamp of when the record was written
    type:    event category (see below)
    outcome: "success" | "error"
  Plus type-specific metadata fields.

Event types and their metadata fields:
  task
    name (str), priority (str), duration_min (int), due_date (str|None),
    source ("email" | "shortcut" | "reminder")

  calendar_event
    title (str), start (str), all_day (bool), source ("email" | "shortcut")

  note
    subject (str), mode ("email" | "url" | "file"), tags (list[str]),
    write_status ("saved" | "updated" | "unchanged")

  ref_letter
    candidate (str), letter_type (str|None), had_pdf (bool)

  reminder_sent
    label (str), date (str), days_until (int), note_name (str)

  reminder_dismissed
    label (str), note_name (str)

  reminder_snoozed
    label (str), snooze_days (int), note_name (str)

  reminder_task_created
    label (str), note_name (str), task_count (int)

Default log path: ~/.email_to_motion/activity.jsonl
Override with ACTIVITY_LOG_PATH in your .env file.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)

# Module-level lock — record() is called from the socket listener thread,
# the scheduler thread, and the startup sweep concurrently; serialise writes
# so lines are never interleaved.
_lock = threading.Lock()


def _log_path() -> Path:
    return Path(config.ACTIVITY_LOG_PATH).expanduser().resolve()


def record(event_type: str, outcome: str = "success", **metadata: Any) -> None:
    """Append one activity record to the JSONL log.

    Args:
        event_type: Category of the event — one of the types listed in the
                    module docstring ("task", "note", "calendar_event", etc.).
        outcome:    "success" (default) or "error".
        **metadata: Type-specific key/value pairs.  Non-serialisable values
                    are coerced to strings via ``default=str``.

    Failures are logged as a warning and silently swallowed so a log-write
    error never interrupts the main processing pipeline.
    """
    entry: dict[str, Any] = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "type":    event_type,
        "outcome": outcome,
        **metadata,
    }
    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with _lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception as exc:
        log.warning("activity_log: failed to write %r record: %s", event_type, exc)
