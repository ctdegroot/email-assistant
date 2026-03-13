"""
Integration tests for conflict_checker.py.

Covers the full flow from ICS fetch through Slack alerts and interactive
button handling, with all external I/O replaced by mocks:

  - requests.get       → stubbed ICS responses
  - config.slack       → MagicMock (captures Slack API calls)
  - config.claude      → stubbed Claude responses (for email draft)
  - _seen_path()       → redirected to tmp_path
  - fetch_busy_blocks  → stubbed (for availability in resolve flow)
"""

import json
import textwrap
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

import email_to_motion.conflict_checker as cc
from email_to_motion import config


# ── Test data ─────────────────────────────────────────────────────────────────

_CHANNEL = "C_CONFLICT_TEST"

_EVENT_A = {
    "title":     "Morning Standup",
    "start":     "2026-03-16T09:00:00+00:00",
    "end":       "2026-03-16T10:00:00+00:00",
    "attendees": [{"name": "Alice Smith", "email": "alice@example.com"}],
    "uid":       "standup-uid-001",
}

_EVENT_B = {
    "title":     "Budget Review",
    "start":     "2026-03-16T09:30:00+00:00",
    "end":       "2026-03-16T10:30:00+00:00",
    "attendees": [{"name": "Bob Jones", "email": "bob@example.com"}],
    "uid":       "budget-uid-002",
}

# Valid ICS with two overlapping UTC events on 2026-03-16
_ICS_TWO_CONFLICTS = textwrap.dedent("""\
    BEGIN:VCALENDAR
    VERSION:2.0
    PRODID:-//Test//Test//EN
    BEGIN:VEVENT
    DTSTART:20260316T090000Z
    DTEND:20260316T100000Z
    SUMMARY:Morning Standup
    UID:standup-uid-001@test
    ATTENDEE;CN=Alice Smith:mailto:alice@example.com
    END:VEVENT
    BEGIN:VEVENT
    DTSTART:20260316T093000Z
    DTEND:20260316T103000Z
    SUMMARY:Budget Review
    UID:budget-uid-002@test
    ATTENDEE;CN=Bob Jones:mailto:bob@example.com
    END:VEVENT
    END:VCALENDAR
""").encode()

# Valid ICS with two non-overlapping events
_ICS_NO_CONFLICTS = textwrap.dedent("""\
    BEGIN:VCALENDAR
    VERSION:2.0
    PRODID:-//Test//Test//EN
    BEGIN:VEVENT
    DTSTART:20260316T090000Z
    DTEND:20260316T100000Z
    SUMMARY:Morning Standup
    UID:standup-uid-001@test
    END:VEVENT
    BEGIN:VEVENT
    DTSTART:20260316T110000Z
    DTEND:20260316T120000Z
    SUMMARY:Lunch Planning
    UID:lunch-uid-002@test
    END:VEVENT
    END:VCALENDAR
""").encode()

# ICS with one all-day event (should be ignored)
_ICS_ALLDAY = textwrap.dedent("""\
    BEGIN:VCALENDAR
    VERSION:2.0
    PRODID:-//Test//Test//EN
    BEGIN:VEVENT
    DTSTART;VALUE=DATE:20260316
    DTEND;VALUE=DATE:20260317
    SUMMARY:All-day Conference
    UID:allday-uid-001@test
    END:VEVENT
    END:VCALENDAR
""").encode()

# ICS with a transparent (free/show-as-free) event
_ICS_TRANSPARENT = textwrap.dedent("""\
    BEGIN:VCALENDAR
    VERSION:2.0
    PRODID:-//Test//Test//EN
    BEGIN:VEVENT
    DTSTART:20260316T090000Z
    DTEND:20260316T100000Z
    SUMMARY:Reminder
    UID:reminder-uid-001@test
    TRANSP:TRANSPARENT
    END:VEVENT
    BEGIN:VEVENT
    DTSTART:20260316T093000Z
    DTEND:20260316T103000Z
    SUMMARY:Budget Review
    UID:budget-uid-002@test
    END:VEVENT
    END:VCALENDAR
""").encode()

_DRAFT_EMAIL = "Subject: Rescheduling Morning Standup\n\nDear Alice,\n\nI need to reschedule.\n\nCheers, Chris"

_TEST_WEEK = (date(2026, 3, 16), date(2026, 3, 20))  # Mon–Fri


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def conflict_env(tmp_path, monkeypatch):
    """Patch all external dependencies for conflict checker tests."""
    # Slack stub
    slack_client = MagicMock()
    monkeypatch.setattr(config, "slack", slack_client)

    # Claude stub — returns a plausible email draft
    claude_resp = MagicMock()
    claude_resp.content = [MagicMock(text=_DRAFT_EMAIL)]
    claude_client = MagicMock()
    claude_client.messages.create.return_value = claude_resp
    monkeypatch.setattr(config, "claude", claude_client)

    # Config stubs
    monkeypatch.setattr(config, "OUTLOOK_ICS_URL",     "https://fake-ics.example.com/calendar.ics")
    monkeypatch.setattr(config, "CALENDAR_OWNER_EMAIL", "chris@example.com")
    monkeypatch.setattr(config, "SMTP_USER",            "chris@gmail.com")

    # Redirect seen_conflicts.json to tmp_path so tests don't touch ~/.email_to_motion
    monkeypatch.setattr(cc, "_seen_path", lambda: tmp_path / "seen_conflicts.json")

    return {
        "tmp_path":    tmp_path,
        "slack":       slack_client,
        "claude":      claude_client,
        "claude_resp": claude_resp,
    }


def _mock_ics_response(ics_bytes: bytes) -> MagicMock:
    """Build a fake requests.Response for an ICS feed."""
    resp = MagicMock()
    resp.content = ics_bytes
    resp.raise_for_status = lambda: None
    return resp


def _resolve_payload(event_a: dict, event_b: dict, choice: str = "event_a") -> dict:
    """Build a fake Slack modal-submission payload for handle_resolve_submit."""
    metadata = json.dumps({
        "channel_id": _CHANNEL,
        "message_ts": "9999999999.000001",
        "event_a":    event_a,
        "event_b":    event_b,
    })
    return {
        "view": {
            "private_metadata": metadata,
            "state": {
                "values": {
                    "move_choice_block": {
                        "move_choice": {
                            "selected_option": {"value": choice}
                        }
                    }
                }
            },
        }
    }


# ── find_conflicts ────────────────────────────────────────────────────────────

class TestFindConflicts:

    def test_overlapping_events_detected(self, conflict_env):
        with patch("email_to_motion.conflict_checker.requests.get",
                   return_value=_mock_ics_response(_ICS_TWO_CONFLICTS)):
            conflicts = cc.find_conflicts(date(2026, 3, 16), date(2026, 3, 16))
        assert len(conflicts) == 1
        titles = {conflicts[0][0]["title"], conflicts[0][1]["title"]}
        assert titles == {"Morning Standup", "Budget Review"}

    def test_non_overlapping_events_not_reported(self, conflict_env):
        with patch("email_to_motion.conflict_checker.requests.get",
                   return_value=_mock_ics_response(_ICS_NO_CONFLICTS)):
            conflicts = cc.find_conflicts(date(2026, 3, 16), date(2026, 3, 16))
        assert conflicts == []

    def test_all_day_events_are_skipped(self, conflict_env):
        with patch("email_to_motion.conflict_checker.requests.get",
                   return_value=_mock_ics_response(_ICS_ALLDAY)):
            conflicts = cc.find_conflicts(date(2026, 3, 16), date(2026, 3, 16))
        assert conflicts == []

    def test_transparent_events_are_skipped(self, conflict_env):
        """TRANSP:TRANSPARENT events (show-as-free) must not trigger conflicts."""
        with patch("email_to_motion.conflict_checker.requests.get",
                   return_value=_mock_ics_response(_ICS_TRANSPARENT)):
            conflicts = cc.find_conflicts(date(2026, 3, 16), date(2026, 3, 16))
        assert conflicts == []

    def test_attendees_extracted(self, conflict_env):
        with patch("email_to_motion.conflict_checker.requests.get",
                   return_value=_mock_ics_response(_ICS_TWO_CONFLICTS)):
            conflicts = cc.find_conflicts(date(2026, 3, 16), date(2026, 3, 16))
        all_attendee_emails = {
            att["email"]
            for pair in conflicts
            for event in pair
            for att in event["attendees"]
        }
        assert "alice@example.com" in all_attendee_emails

    def test_owner_email_excluded_from_attendees(self, conflict_env):
        """The calendar owner's own email must not appear as an attendee."""
        ics = textwrap.dedent("""\
            BEGIN:VCALENDAR
            VERSION:2.0
            PRODID:-//Test//Test//EN
            BEGIN:VEVENT
            DTSTART:20260316T090000Z
            DTEND:20260316T100000Z
            SUMMARY:Standup
            UID:uid-a@test
            ATTENDEE;CN=Chris:mailto:chris@example.com
            ATTENDEE;CN=Alice:mailto:alice@example.com
            END:VEVENT
            BEGIN:VEVENT
            DTSTART:20260316T093000Z
            DTEND:20260316T103000Z
            SUMMARY:Budget Review
            UID:uid-b@test
            ATTENDEE;CN=Bob:mailto:bob@example.com
            END:VEVENT
            END:VCALENDAR
        """).encode()
        with patch("email_to_motion.conflict_checker.requests.get",
                   return_value=_mock_ics_response(ics)):
            conflicts = cc.find_conflicts(date(2026, 3, 16), date(2026, 3, 16))
        attendee_emails = [att["email"] for att in conflicts[0][0]["attendees"]]
        assert "chris@example.com" not in attendee_emails
        assert "alice@example.com" in attendee_emails


# ── run_morning_check ─────────────────────────────────────────────────────────

class TestRunMorningCheck:

    def test_new_conflict_posts_slack_alert(self, conflict_env):
        with patch.object(cc, "find_conflicts", return_value=[(_EVENT_A, _EVENT_B)]):
            cc.run_morning_check(_CHANNEL)
        conflict_env["slack"].chat_postMessage.assert_called_once()

    def test_alert_mentions_both_event_titles(self, conflict_env):
        with patch.object(cc, "find_conflicts", return_value=[(_EVENT_A, _EVENT_B)]):
            cc.run_morning_check(_CHANNEL)
        call_kwargs = conflict_env["slack"].chat_postMessage.call_args.kwargs
        # Title appears in the blocks text
        blocks_text = str(call_kwargs.get("blocks", ""))
        assert "Morning Standup" in blocks_text
        assert "Budget Review" in blocks_text

    def test_no_conflicts_silent_by_default(self, conflict_env):
        with patch.object(cc, "find_conflicts", return_value=[]):
            cc.run_morning_check(_CHANNEL)
        conflict_env["slack"].chat_postMessage.assert_not_called()

    def test_no_conflicts_verbose_posts_confirmation(self, conflict_env):
        with patch.object(cc, "find_conflicts", return_value=[]):
            cc.run_morning_check(_CHANNEL, verbose=True)
        conflict_env["slack"].chat_postMessage.assert_called_once()
        text = conflict_env["slack"].chat_postMessage.call_args.kwargs["text"]
        assert "No conflicts" in text or "✅" in text

    def test_ics_fetch_error_posts_warning(self, conflict_env):
        with patch.object(cc, "find_conflicts", side_effect=RuntimeError("ICS fetch failed")):
            cc.run_morning_check(_CHANNEL)
        text = conflict_env["slack"].chat_postMessage.call_args.kwargs["text"]
        assert "⚠️" in text

    def test_already_seen_conflict_is_skipped(self, conflict_env, tmp_path):
        """A conflict recorded in seen_conflicts.json must not be re-posted."""
        cid = cc._conflict_id(_EVENT_A, _EVENT_B)
        (tmp_path / "seen_conflicts.json").write_text(
            json.dumps({"week": cc._week_key(), "ids": [cid]})
        )
        with patch.object(cc, "find_conflicts", return_value=[(_EVENT_A, _EVENT_B)]):
            cc.run_morning_check(_CHANNEL)
        conflict_env["slack"].chat_postMessage.assert_not_called()

    def test_force_flag_re_reports_seen_conflict(self, conflict_env, tmp_path):
        """force=True must bypass the seen list and re-report every conflict."""
        cid = cc._conflict_id(_EVENT_A, _EVENT_B)
        (tmp_path / "seen_conflicts.json").write_text(
            json.dumps({"week": cc._week_key(), "ids": [cid]})
        )
        with patch.object(cc, "find_conflicts", return_value=[(_EVENT_A, _EVENT_B)]):
            cc.run_morning_check(_CHANNEL, force=True)
        conflict_env["slack"].chat_postMessage.assert_called_once()

    def test_seen_list_updated_after_posting(self, conflict_env, tmp_path):
        """After posting an alert the conflict ID must be saved to the seen file."""
        seen_file = tmp_path / "seen_conflicts.json"
        assert not seen_file.exists()
        with patch.object(cc, "find_conflicts", return_value=[(_EVENT_A, _EVENT_B)]):
            cc.run_morning_check(_CHANNEL)
        assert seen_file.exists()
        data = json.loads(seen_file.read_text())
        assert cc._conflict_id(_EVENT_A, _EVENT_B) in data["ids"]

    def test_multiple_conflicts_each_get_an_alert(self, conflict_env):
        event_c = {**_EVENT_B, "uid": "uid-c", "title": "Third Meeting",
                   "start": "2026-03-16T09:15:00+00:00"}
        with patch.object(cc, "find_conflicts",
                          return_value=[(_EVENT_A, _EVENT_B), (_EVENT_A, event_c)]):
            cc.run_morning_check(_CHANNEL)
        assert conflict_env["slack"].chat_postMessage.call_count == 2


# ── _post_conflict_alert ──────────────────────────────────────────────────────

class TestPostConflictAlert:

    def test_alert_has_resolve_conflict_button(self, conflict_env):
        cc._post_conflict_alert(_CHANNEL, _EVENT_A, _EVENT_B)
        blocks = conflict_env["slack"].chat_postMessage.call_args.kwargs["blocks"]
        action_ids = [
            el["action_id"]
            for block in blocks if block["type"] == "actions"
            for el in block["elements"]
        ]
        assert "resolve_conflict" in action_ids

    def test_alert_has_ignore_button(self, conflict_env):
        cc._post_conflict_alert(_CHANNEL, _EVENT_A, _EVENT_B)
        blocks = conflict_env["slack"].chat_postMessage.call_args.kwargs["blocks"]
        action_ids = [
            el["action_id"]
            for block in blocks if block["type"] == "actions"
            for el in block["elements"]
        ]
        assert "ignore_conflict" in action_ids

    def test_resolve_button_value_contains_both_events(self, conflict_env):
        cc._post_conflict_alert(_CHANNEL, _EVENT_A, _EVENT_B)
        blocks = conflict_env["slack"].chat_postMessage.call_args.kwargs["blocks"]
        resolve_btn = next(
            el for block in blocks if block["type"] == "actions"
            for el in block["elements"] if el["action_id"] == "resolve_conflict"
        )
        payload = json.loads(resolve_btn["value"])
        assert payload["event_a"]["uid"] == "standup-uid-001"
        assert payload["event_b"]["uid"] == "budget-uid-002"

    def test_resolve_button_value_contains_channel_id(self, conflict_env):
        cc._post_conflict_alert(_CHANNEL, _EVENT_A, _EVENT_B)
        blocks = conflict_env["slack"].chat_postMessage.call_args.kwargs["blocks"]
        resolve_btn = next(
            el for block in blocks if block["type"] == "actions"
            for el in block["elements"] if el["action_id"] == "resolve_conflict"
        )
        payload = json.loads(resolve_btn["value"])
        assert payload["channel_id"] == _CHANNEL


# ── handle_ignore ─────────────────────────────────────────────────────────────

class TestHandleIgnore:

    def _ignore_payload(self):
        return {
            "channel": {"id": _CHANNEL},
            "message": {"ts": "1234567890.000001"},
        }

    def test_chat_update_called(self, conflict_env):
        cc.handle_ignore(self._ignore_payload())
        conflict_env["slack"].chat_update.assert_called_once()

    def test_update_uses_correct_channel_and_ts(self, conflict_env):
        cc.handle_ignore(self._ignore_payload())
        kwargs = conflict_env["slack"].chat_update.call_args.kwargs
        assert kwargs["channel"] == _CHANNEL
        assert kwargs["ts"] == "1234567890.000001"

    def test_update_text_indicates_ignored(self, conflict_env):
        cc.handle_ignore(self._ignore_payload())
        kwargs = conflict_env["slack"].chat_update.call_args.kwargs
        assert "Ignored" in kwargs["text"] or "ignored" in kwargs["text"]

    def test_slack_api_error_does_not_raise(self, conflict_env):
        """handle_ignore swallows exceptions so a failed update never crashes the bot."""
        conflict_env["slack"].chat_update.side_effect = RuntimeError("Slack error")
        # Should not raise
        cc.handle_ignore(self._ignore_payload())


# ── open_resolve_modal ────────────────────────────────────────────────────────

class TestOpenResolveModal:

    def _action_payload(self):
        return {
            "trigger_id": "TRIGGER_ID_XYZ",
            "channel":    {"id": _CHANNEL},
            "message":    {"ts": "9999999999.000001"},
        }

    def _action(self):
        return {
            "value": json.dumps({
                "channel_id": _CHANNEL,
                "event_a":    _EVENT_A,
                "event_b":    _EVENT_B,
            })
        }

    def test_views_open_called(self, conflict_env):
        cc.open_resolve_modal(self._action_payload(), self._action())
        conflict_env["slack"].views_open.assert_called_once()

    def test_views_open_uses_trigger_id(self, conflict_env):
        cc.open_resolve_modal(self._action_payload(), self._action())
        kwargs = conflict_env["slack"].views_open.call_args.kwargs
        assert kwargs["trigger_id"] == "TRIGGER_ID_XYZ"

    def test_modal_type_is_modal(self, conflict_env):
        cc.open_resolve_modal(self._action_payload(), self._action())
        view = conflict_env["slack"].views_open.call_args.kwargs["view"]
        assert view["type"] == "modal"

    def test_modal_metadata_contains_both_events(self, conflict_env):
        cc.open_resolve_modal(self._action_payload(), self._action())
        view = conflict_env["slack"].views_open.call_args.kwargs["view"]
        metadata = json.loads(view["private_metadata"])
        assert metadata["event_a"]["uid"] == "standup-uid-001"
        assert metadata["event_b"]["uid"] == "budget-uid-002"

    def test_modal_has_two_radio_options(self, conflict_env):
        cc.open_resolve_modal(self._action_payload(), self._action())
        view = conflict_env["slack"].views_open.call_args.kwargs["view"]
        radio_block = next(
            b for b in view["blocks"] if b.get("type") == "input"
        )
        options = radio_block["element"]["options"]
        assert len(options) == 2
        values = {o["value"] for o in options}
        assert values == {"event_a", "event_b"}


# ── handle_resolve_submit ─────────────────────────────────────────────────────

class TestHandleResolveSubmit:

    def test_claude_called_to_generate_draft(self, conflict_env):
        payload = _resolve_payload(_EVENT_A, _EVENT_B, choice="event_a")
        with patch.object(cc, "_build_rescheduling_availability", return_value="Mon 9–10 AM"):
            cc.handle_resolve_submit(payload)
        conflict_env["claude"].messages.create.assert_called_once()

    def test_draft_posted_to_slack(self, conflict_env):
        payload = _resolve_payload(_EVENT_A, _EVENT_B, choice="event_a")
        with patch.object(cc, "_build_rescheduling_availability", return_value="Mon 9–10 AM"):
            cc.handle_resolve_submit(payload)
        # At least one chat_postMessage call should contain the draft email text
        all_calls = conflict_env["slack"].chat_postMessage.call_args_list
        assert len(all_calls) >= 1
        all_text = " ".join(str(c) for c in all_calls)
        assert "Rescheduling" in all_text or "reschedul" in all_text.lower()

    def test_original_alert_updated(self, conflict_env):
        payload = _resolve_payload(_EVENT_A, _EVENT_B, choice="event_a")
        with patch.object(cc, "_build_rescheduling_availability", return_value="Mon 9–10 AM"):
            cc.handle_resolve_submit(payload)
        conflict_env["slack"].chat_update.assert_called_once()
        kwargs = conflict_env["slack"].chat_update.call_args.kwargs
        assert kwargs["channel"] == _CHANNEL
        assert kwargs["ts"] == "9999999999.000001"

    def test_chose_event_a_to_move(self, conflict_env):
        """When user picks event_a, Claude prompt should reference event_a's title."""
        payload = _resolve_payload(_EVENT_A, _EVENT_B, choice="event_a")
        with patch.object(cc, "_build_rescheduling_availability", return_value="Mon 9–10 AM"):
            cc.handle_resolve_submit(payload)
        prompt_content = str(conflict_env["claude"].messages.create.call_args)
        assert "Morning Standup" in prompt_content   # event_a title
        assert "Budget Review" in prompt_content     # event_b title (the conflict)

    def test_chose_event_b_to_move(self, conflict_env):
        """When user picks event_b, Claude prompt should reference event_b's title."""
        payload = _resolve_payload(_EVENT_A, _EVENT_B, choice="event_b")
        with patch.object(cc, "_build_rescheduling_availability", return_value="Mon 9–10 AM"):
            cc.handle_resolve_submit(payload)
        prompt_content = str(conflict_env["claude"].messages.create.call_args)
        assert "Budget Review" in prompt_content  # event_b is the one being moved

    def test_claude_error_posts_warning(self, conflict_env):
        conflict_env["claude"].messages.create.side_effect = RuntimeError("Claude unavailable")
        payload = _resolve_payload(_EVENT_A, _EVENT_B)
        with patch.object(cc, "_build_rescheduling_availability", return_value="Mon 9–10 AM"):
            cc.handle_resolve_submit(payload)
        text = conflict_env["slack"].chat_postMessage.call_args.kwargs["text"]
        assert "⚠️" in text
