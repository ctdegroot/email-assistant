"""
Tests for activity_log.py — pure logic, no external I/O.
"""

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import email_to_motion.config as _cfg

# Point the log at a temp path before importing the module
_cfg.ACTIVITY_LOG_PATH = "/tmp/test_activity_log_placeholder.jsonl"

from email_to_motion.activity_log import record  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_records(path: Path) -> list[dict]:
    """Read all JSONL records from *path*."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRecord:

    def test_creates_file_and_writes_record(self, tmp_path):
        log_file = tmp_path / "activity.jsonl"
        with patch.object(_cfg, "ACTIVITY_LOG_PATH", str(log_file)):
            record("task", name="Review paper", priority="HIGH", duration_min=60)

        assert log_file.exists()
        records = _read_records(log_file)
        assert len(records) == 1
        r = records[0]
        assert r["type"] == "task"
        assert r["outcome"] == "success"
        assert r["name"] == "Review paper"
        assert r["priority"] == "HIGH"
        assert r["duration_min"] == 60
        assert "ts" in r

    def test_default_outcome_is_success(self, tmp_path):
        log_file = tmp_path / "activity.jsonl"
        with patch.object(_cfg, "ACTIVITY_LOG_PATH", str(log_file)):
            record("note", subject="Test")
        r = _read_records(log_file)[0]
        assert r["outcome"] == "success"

    def test_custom_outcome(self, tmp_path):
        log_file = tmp_path / "activity.jsonl"
        with patch.object(_cfg, "ACTIVITY_LOG_PATH", str(log_file)):
            record("task", outcome="error", reason="API timeout")
        r = _read_records(log_file)[0]
        assert r["outcome"] == "error"
        assert r["reason"] == "API timeout"

    def test_multiple_records_appended(self, tmp_path):
        log_file = tmp_path / "activity.jsonl"
        with patch.object(_cfg, "ACTIVITY_LOG_PATH", str(log_file)):
            record("task", name="First")
            record("note", subject="Second")
            record("calendar_event", title="Third")

        records = _read_records(log_file)
        assert len(records) == 3
        assert records[0]["name"] == "First"
        assert records[1]["subject"] == "Second"
        assert records[2]["title"] == "Third"

    def test_creates_parent_dirs(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "activity.jsonl"
        with patch.object(_cfg, "ACTIVITY_LOG_PATH", str(nested)):
            record("task", name="Test")
        assert nested.exists()

    def test_ts_is_iso8601_utc(self, tmp_path):
        log_file = tmp_path / "activity.jsonl"
        with patch.object(_cfg, "ACTIVITY_LOG_PATH", str(log_file)):
            record("note", subject="Test")
        ts = _read_records(log_file)[0]["ts"]
        # UTC ISO-8601 ends with +00:00
        assert ts.endswith("+00:00")

    def test_non_serialisable_value_coerced_to_str(self, tmp_path):
        """Non-JSON-serialisable values (e.g. date objects) should not raise."""
        from datetime import date
        log_file = tmp_path / "activity.jsonl"
        with patch.object(_cfg, "ACTIVITY_LOG_PATH", str(log_file)):
            record("reminder_sent", label="Deadline", date=date(2026, 5, 1), days_until=14)
        r = _read_records(log_file)[0]
        assert r["label"] == "Deadline"
        # date object coerced to string
        assert isinstance(r["date"], str)

    def test_all_event_types_accepted(self, tmp_path):
        """Smoke-test that all documented event types write without error."""
        log_file = tmp_path / "activity.jsonl"
        event_types = [
            ("task",                  {"name": "x", "priority": "LOW", "duration_min": 30, "due_date": None, "source": "email"}),
            ("calendar_event",        {"title": "Meeting", "start": "2026-05-01T09:00", "all_day": False, "source": "email"}),
            ("note",                  {"subject": "Grant", "mode": "email", "tags": ["research"], "write_status": "saved"}),
            ("ref_letter",            {"candidate": "Jane Smith", "letter_type": "phd-application", "had_pdf": True}),
            ("reminder_sent",         {"label": "LOI deadline", "date": "2026-05-01", "days_until": 14, "note_name": "note.md"}),
            ("reminder_dismissed",    {"label": "LOI deadline", "note_name": "note.md"}),
            ("reminder_snoozed",      {"label": "LOI deadline", "snooze_days": 7, "note_name": "note.md"}),
            ("reminder_task_created", {"label": "LOI deadline", "note_name": "note.md", "task_count": 2}),
        ]
        with patch.object(_cfg, "ACTIVITY_LOG_PATH", str(log_file)):
            for event_type, meta in event_types:
                record(event_type, **meta)

        records = _read_records(log_file)
        assert len(records) == len(event_types)
        written_types = [r["type"] for r in records]
        assert written_types == [et for et, _ in event_types]

    def test_write_failure_does_not_raise(self, tmp_path):
        """A permission error writing the log must not propagate to the caller."""
        unwritable = "/dev/null/impossible/path/activity.jsonl"
        with patch.object(_cfg, "ACTIVITY_LOG_PATH", unwritable):
            record("task", name="Should not crash")   # must not raise

    def test_thread_safe_concurrent_writes(self, tmp_path):
        """Many concurrent record() calls must all land without interleaving."""
        log_file = tmp_path / "activity.jsonl"
        n_threads = 20

        errors: list[Exception] = []

        def _write(i: int):
            try:
                with patch.object(_cfg, "ACTIVITY_LOG_PATH", str(log_file)):
                    record("task", name=f"Task {i}", thread=i)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Threads raised exceptions: {errors}"
        records = _read_records(log_file)
        assert len(records) == n_threads
        # Every line must be valid JSON (no interleaved writes)
        names = {r["name"] for r in records}
        assert names == {f"Task {i}" for i in range(n_threads)}
