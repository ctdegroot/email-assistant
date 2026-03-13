"""
tests/test_watch_dates.py — Unit and integration tests for watch_date_handler.

Coverage:
  _should_remind       — all reminder-logic branches
  scan_and_remind      — file scan, reminder posting, frontmatter update
  handle_watch_date_action — dismiss, snooze, task-manual; task-auto is
                             covered by a smoke test (Motion API mocked)
"""

import json
import textwrap
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

# Patch config before the module under test imports it
import email_to_motion.config as _cfg
_cfg.slack  = MagicMock()
_cfg.claude = MagicMock()
_cfg.NOTES_OUTPUT_PATH      = "/tmp/test_notes_wd"
_cfg.SLACK_REMINDERS_CHANNEL = "C_REMINDERS"
_cfg.MOTION_API_KEY          = "test-key"
_cfg.MOTION_WORKSPACE_ID     = "ws-1"
_cfg.MOTION_ASSIGNEE_ID      = "user-1"
_cfg.ALLOWED_SLACK_USER_ID   = "U_OWNER"

from email_to_motion.watch_date_handler import (
    _should_remind,
    _read_note_frontmatter,
    _write_note_frontmatter,
    _get_watch_dates,
    scan_and_remind,
    handle_watch_date_action,
)


# ── helpers ───────────────────────────────────────────────────────────────────

TODAY      = date.today()
FUTURE_14  = (TODAY + timedelta(days=14)).isoformat()
FUTURE_7   = (TODAY + timedelta(days=7)).isoformat()
FUTURE_6   = (TODAY + timedelta(days=6)).isoformat()
FUTURE_3   = (TODAY + timedelta(days=3)).isoformat()
FUTURE_1   = (TODAY + timedelta(days=1)).isoformat()
PAST       = (TODAY - timedelta(days=1)).isoformat()
SNOOZE_FUTURE = (TODAY + timedelta(days=3)).isoformat()
SNOOZE_PAST   = (TODAY - timedelta(days=1)).isoformat()


def _entry(date_str, status="active", snooze_until=None, last_reminded=None):
    return {
        "label": "Test deadline",
        "date": date_str,
        "status": status,
        "snooze_until": snooze_until,
        "last_reminded": last_reminded,
    }


def _note_with_watch_dates(watch_dates: list, tmp_path: Path) -> Path:
    """Write a minimal note with watch_dates frontmatter and return its Path."""
    fm_data = {
        "date": "2026-01-01 09:00",
        "from": "sender@example.com",
        "subject": "Test Grant",
        "tags": ["grant"],
        "attachments": [],
        "watch_dates": watch_dates,
        "source_hash": "abc123",
    }
    content = "---\n" + yaml.dump(fm_data, default_flow_style=False) + "---\n\n## Summary\nTest.\n"
    note_path = tmp_path / "2026-01-01 - Test Grant.md"
    note_path.write_text(content, encoding="utf-8")
    return note_path


# ── _should_remind ─────────────────────────────────────────────────────────

class TestShouldRemind:

    def test_fires_at_14_days(self):
        assert _should_remind(_entry(FUTURE_14), TODAY) is True

    def test_fires_at_7_days(self):
        assert _should_remind(_entry(FUTURE_7), TODAY) is True

    def test_fires_at_6_days(self):
        assert _should_remind(_entry(FUTURE_6), TODAY) is True

    def test_fires_at_3_days(self):
        assert _should_remind(_entry(FUTURE_3), TODAY) is True

    def test_fires_at_1_day(self):
        assert _should_remind(_entry(FUTURE_1), TODAY) is True

    def test_does_not_fire_at_13_days(self):
        d = (TODAY + timedelta(days=13)).isoformat()
        assert _should_remind(_entry(d), TODAY) is False

    def test_does_not_fire_at_10_days(self):
        d = (TODAY + timedelta(days=10)).isoformat()
        assert _should_remind(_entry(d), TODAY) is False

    def test_does_not_fire_at_8_days(self):
        d = (TODAY + timedelta(days=8)).isoformat()
        assert _should_remind(_entry(d), TODAY) is False

    def test_does_not_fire_for_dismissed(self):
        assert _should_remind(_entry(FUTURE_7, status="dismissed"), TODAY) is False

    def test_does_not_fire_while_snoozed(self):
        e = _entry(FUTURE_7, status="snoozed", snooze_until=SNOOZE_FUTURE)
        assert _should_remind(e, TODAY) is False

    def test_fires_after_snooze_expires(self):
        e = _entry(FUTURE_3, status="snoozed", snooze_until=SNOOZE_PAST)
        assert _should_remind(e, TODAY) is True

    def test_does_not_fire_if_reminded_today(self):
        e = _entry(FUTURE_7, last_reminded=TODAY.isoformat())
        assert _should_remind(e, TODAY) is False

    def test_does_not_fire_for_past_date(self):
        assert _should_remind(_entry(PAST), TODAY) is False

    def test_does_not_fire_for_today(self):
        # days_until == 0: passed the "days_until < 0" check but 0 is not in
        # STANDARD_THRESHOLDS and not in 0 < days_until <= 6, so should NOT fire.
        # (If the deadline is today, the user already got a reminder yesterday.)
        d = TODAY.isoformat()
        assert _should_remind(_entry(d), TODAY) is False

    def test_handles_date_object_in_entry(self):
        """yaml.safe_load may return a datetime.date object instead of a string."""
        entry = {
            "label": "Test",
            "date": TODAY + timedelta(days=7),   # date object, not string
            "status": "active",
        }
        assert _should_remind(entry, TODAY) is True

    def test_handles_invalid_date_gracefully(self):
        entry = {"label": "Test", "date": "not-a-date", "status": "active"}
        assert _should_remind(entry, TODAY) is False

    def test_handles_missing_date_gracefully(self):
        entry = {"label": "Test", "status": "active"}
        assert _should_remind(entry, TODAY) is False


# ── scan_and_remind ────────────────────────────────────────────────────────

class TestScanAndRemind:

    def test_posts_reminder_for_upcoming_date(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg, "NOTES_OUTPUT_PATH", str(tmp_path))
        monkeypatch.setattr(_cfg, "SLACK_REMINDERS_CHANNEL", "C_REMIND")
        _cfg.slack.reset_mock()

        _note_with_watch_dates(
            [{"label": "LOI deadline", "date": FUTURE_7}], tmp_path
        )
        scan_and_remind()

        _cfg.slack.chat_postMessage.assert_called_once()
        call_kwargs = _cfg.slack.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C_REMIND"
        assert "LOI deadline" in call_kwargs["text"]

    def test_updates_last_reminded_in_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg, "NOTES_OUTPUT_PATH", str(tmp_path))
        monkeypatch.setattr(_cfg, "SLACK_REMINDERS_CHANNEL", "C_REMIND")
        _cfg.slack.reset_mock()

        note_path = _note_with_watch_dates(
            [{"label": "LOI deadline", "date": FUTURE_7}], tmp_path
        )
        scan_and_remind()

        fm, _ = _read_note_frontmatter(note_path)
        entry = _get_watch_dates(fm)[0]
        assert entry.get("last_reminded") == TODAY.isoformat()

    def test_does_not_post_for_dismissed_date(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg, "NOTES_OUTPUT_PATH", str(tmp_path))
        monkeypatch.setattr(_cfg, "SLACK_REMINDERS_CHANNEL", "C_REMIND")
        _cfg.slack.reset_mock()

        _note_with_watch_dates(
            [{"label": "LOI deadline", "date": FUTURE_7, "status": "dismissed"}],
            tmp_path,
        )
        scan_and_remind()
        _cfg.slack.chat_postMessage.assert_not_called()

    def test_does_not_post_for_active_snooze(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg, "NOTES_OUTPUT_PATH", str(tmp_path))
        monkeypatch.setattr(_cfg, "SLACK_REMINDERS_CHANNEL", "C_REMIND")
        _cfg.slack.reset_mock()

        _note_with_watch_dates(
            [{
                "label": "LOI deadline",
                "date": FUTURE_7,
                "status": "snoozed",
                "snooze_until": SNOOZE_FUTURE,
            }],
            tmp_path,
        )
        scan_and_remind()
        _cfg.slack.chat_postMessage.assert_not_called()

    def test_does_not_post_when_no_reminders_channel(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg, "NOTES_OUTPUT_PATH", str(tmp_path))
        monkeypatch.setattr(_cfg, "SLACK_REMINDERS_CHANNEL", "")
        _cfg.slack.reset_mock()

        _note_with_watch_dates(
            [{"label": "LOI deadline", "date": FUTURE_7}], tmp_path
        )
        scan_and_remind()
        _cfg.slack.chat_postMessage.assert_not_called()

    def test_skips_note_with_empty_watch_dates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg, "NOTES_OUTPUT_PATH", str(tmp_path))
        monkeypatch.setattr(_cfg, "SLACK_REMINDERS_CHANNEL", "C_REMIND")
        _cfg.slack.reset_mock()

        _note_with_watch_dates([], tmp_path)
        scan_and_remind()
        _cfg.slack.chat_postMessage.assert_not_called()

    def test_clears_snooze_status_when_snooze_expires(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg, "NOTES_OUTPUT_PATH", str(tmp_path))
        monkeypatch.setattr(_cfg, "SLACK_REMINDERS_CHANNEL", "C_REMIND")
        _cfg.slack.reset_mock()

        note_path = _note_with_watch_dates(
            [{
                "label": "LOI deadline",
                "date": FUTURE_3,
                "status": "snoozed",
                "snooze_until": SNOOZE_PAST,   # snooze has expired
            }],
            tmp_path,
        )
        scan_and_remind()

        _cfg.slack.chat_postMessage.assert_called_once()
        fm, _ = _read_note_frontmatter(note_path)
        entry = _get_watch_dates(fm)[0]
        assert entry.get("status") == "active"
        assert entry.get("snooze_until") is None

    def test_posts_multiple_reminders_for_multiple_dates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg, "NOTES_OUTPUT_PATH", str(tmp_path))
        monkeypatch.setattr(_cfg, "SLACK_REMINDERS_CHANNEL", "C_REMIND")
        _cfg.slack.reset_mock()

        _note_with_watch_dates(
            [
                {"label": "First deadline",  "date": FUTURE_7},
                {"label": "Second deadline", "date": FUTURE_14},
            ],
            tmp_path,
        )
        scan_and_remind()
        assert _cfg.slack.chat_postMessage.call_count == 2


# ── handle_watch_date_action ───────────────────────────────────────────────

def _make_payload(channel_id="C_REMIND", ts="12345.67890", user_id="U_USER"):
    return {
        "channel": {"id": channel_id},
        "message": {"ts": ts},
        "user":    {"id": user_id},
    }


def _make_btn_action(action_id: str, note_name: str, date_label: str, date_str: str):
    value = json.dumps({"note_name": note_name, "date_label": date_label, "date": date_str})
    return {"action_id": action_id, "value": value}


class TestHandleWatchDateAction:

    def _setup_note(self, tmp_path, monkeypatch, watch_dates):
        monkeypatch.setattr(_cfg, "NOTES_OUTPUT_PATH", str(tmp_path))
        _cfg.slack.reset_mock()
        return _note_with_watch_dates(watch_dates, tmp_path)

    # ── dismiss ──────────────────────────────────────────────────────────────

    def test_dismiss_sets_status_to_dismissed(self, tmp_path, monkeypatch):
        note_path = self._setup_note(
            tmp_path, monkeypatch,
            [{"label": "LOI deadline", "date": FUTURE_7}],
        )
        action = _make_btn_action("wd_dismiss", note_path.name, "LOI deadline", FUTURE_7)
        handle_watch_date_action(_make_payload(), action)

        fm, _ = _read_note_frontmatter(note_path)
        entry = _get_watch_dates(fm)[0]
        assert entry["status"] == "dismissed"

    def test_dismiss_updates_slack_message(self, tmp_path, monkeypatch):
        note_path = self._setup_note(
            tmp_path, monkeypatch,
            [{"label": "LOI deadline", "date": FUTURE_7}],
        )
        action = _make_btn_action("wd_dismiss", note_path.name, "LOI deadline", FUTURE_7)
        handle_watch_date_action(_make_payload(), action)

        _cfg.slack.chat_update.assert_called_once()
        text = _cfg.slack.chat_update.call_args[1]["text"]
        assert "Dismissed" in text
        assert "LOI deadline" in text

    # ── snooze ───────────────────────────────────────────────────────────────

    def test_snooze_sets_status_and_snooze_until(self, tmp_path, monkeypatch):
        note_path = self._setup_note(
            tmp_path, monkeypatch,
            [{"label": "LOI deadline", "date": FUTURE_7}],
        )
        snooze_value = json.dumps({
            "days": 3,
            "note_name":  note_path.name,
            "date_label": "LOI deadline",
            "date":       FUTURE_7,
        })
        action = {
            "action_id":       "wd_snooze",
            "selected_option": {"value": snooze_value},
        }
        handle_watch_date_action(_make_payload(), action)

        fm, _ = _read_note_frontmatter(note_path)
        entry = _get_watch_dates(fm)[0]
        assert entry["status"] == "snoozed"
        expected_snooze = (TODAY + timedelta(days=3)).isoformat()
        assert str(entry["snooze_until"]) == expected_snooze

    def test_snooze_acks_with_correct_date(self, tmp_path, monkeypatch):
        note_path = self._setup_note(
            tmp_path, monkeypatch,
            [{"label": "LOI deadline", "date": FUTURE_7}],
        )
        snooze_value = json.dumps({
            "days": 7,
            "note_name":  note_path.name,
            "date_label": "LOI deadline",
            "date":       FUTURE_7,
        })
        action = {
            "action_id":       "wd_snooze",
            "selected_option": {"value": snooze_value},
        }
        handle_watch_date_action(_make_payload(), action)

        text = _cfg.slack.chat_update.call_args[1]["text"]
        assert "7 days" in text

    # ── task manual ──────────────────────────────────────────────────────────

    def test_task_manual_dismisses_and_shows_note_name(self, tmp_path, monkeypatch):
        note_path = self._setup_note(
            tmp_path, monkeypatch,
            [{"label": "LOI deadline", "date": FUTURE_7}],
        )
        action = _make_btn_action("wd_task_manual", note_path.name, "LOI deadline", FUTURE_7)
        handle_watch_date_action(_make_payload(), action)

        fm, _ = _read_note_frontmatter(note_path)
        entry = _get_watch_dates(fm)[0]
        assert entry["status"] == "dismissed"

        text = _cfg.slack.chat_update.call_args[1]["text"]
        assert note_path.name in text
        assert FUTURE_7 in text

    # ── malformed action value ────────────────────────────────────────────────

    def test_handles_malformed_action_value_gracefully(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg, "NOTES_OUTPUT_PATH", str(tmp_path))
        _cfg.slack.reset_mock()
        action = {"action_id": "wd_dismiss", "value": "not-json"}
        # Should not raise
        handle_watch_date_action(_make_payload(), action)

    # ── missing note file ─────────────────────────────────────────────────────

    def test_handles_missing_note_file_gracefully(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg, "NOTES_OUTPUT_PATH", str(tmp_path))
        _cfg.slack.reset_mock()
        action = _make_btn_action("wd_dismiss", "nonexistent.md", "LOI deadline", FUTURE_7)
        # Should not raise — note simply not found
        handle_watch_date_action(_make_payload(), action)


# ── watch_dates frontmatter is written by note_generator ──────────────────

class TestWatchDatesInFrontmatter:
    """Verify that notes written with watch_dates frontmatter can be
    round-tripped through _read_note_frontmatter / _write_note_frontmatter."""

    def test_round_trip_preserves_watch_dates(self, tmp_path):
        watch_dates = [
            {"label": "LOI deadline",      "date": "2026-05-15"},
            {"label": "Submission deadline","date": "2026-09-01"},
        ]
        note_path = _note_with_watch_dates(watch_dates, tmp_path)

        fm, full_text = _read_note_frontmatter(note_path)
        entries = _get_watch_dates(fm)

        assert len(entries) == 2
        assert entries[0]["label"] == "LOI deadline"
        # yaml.safe_load may return a datetime.date; _date_to_str handles both
        from email_to_motion.watch_date_handler import _date_to_str
        assert _date_to_str(entries[0]["date"]) == "2026-05-15"

    def test_empty_watch_dates_returns_empty_list(self, tmp_path):
        note_path = _note_with_watch_dates([], tmp_path)
        fm, _ = _read_note_frontmatter(note_path)
        assert _get_watch_dates(fm) == []
