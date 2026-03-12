"""
Tests for conflict_checker.py — pure logic only.
No ICS fetching, no Slack API calls.
"""

import pytest
from datetime import date, datetime, timedelta

from email_to_motion.conflict_checker import (
    _conflict_id,
    _current_week_range,
    _fmt_event_line,
    _week_key,
)


# ── _week_key ──────────────────────────────────────────────────────────────────

class TestWeekKey:

    def test_returns_iso_date_string(self):
        key = _week_key()
        # Must be parseable as an ISO date
        d = date.fromisoformat(key)
        assert isinstance(d, date)

    def test_is_a_monday(self):
        key = _week_key()
        d = date.fromisoformat(key)
        assert d.weekday() == 0  # 0 = Monday

    def test_is_current_or_past_monday(self):
        key = _week_key()
        d = date.fromisoformat(key)
        assert d <= date.today()

    def test_today_is_within_the_week(self):
        key = _week_key()
        monday = date.fromisoformat(key)
        friday = monday + timedelta(days=4)
        assert monday <= date.today() <= friday + timedelta(days=2)  # allow weekend


# ── _current_week_range ────────────────────────────────────────────────────────

class TestCurrentWeekRange:

    def test_returns_two_dates(self):
        monday, friday = _current_week_range()
        assert isinstance(monday, date)
        assert isinstance(friday, date)

    def test_monday_is_monday(self):
        monday, _ = _current_week_range()
        assert monday.weekday() == 0

    def test_friday_is_friday(self):
        _, friday = _current_week_range()
        assert friday.weekday() == 4

    def test_span_is_four_days(self):
        monday, friday = _current_week_range()
        assert (friday - monday).days == 4

    def test_monday_before_friday(self):
        monday, friday = _current_week_range()
        assert monday < friday


# ── _conflict_id ───────────────────────────────────────────────────────────────

def _event(uid: str, title: str = "Meeting", start: str = "2026-03-10T09:00:00") -> dict:
    return {"uid": uid, "title": title, "start": start, "end": "2026-03-10T10:00:00", "attendees": []}


class TestConflictId:

    def test_is_deterministic(self):
        a = _event("uid-1")
        b = _event("uid-2")
        assert _conflict_id(a, b) == _conflict_id(a, b)

    def test_is_order_independent(self):
        a = _event("uid-1")
        b = _event("uid-2")
        assert _conflict_id(a, b) == _conflict_id(b, a)

    def test_different_pairs_have_different_ids(self):
        a = _event("uid-1")
        b = _event("uid-2")
        c = _event("uid-3")
        assert _conflict_id(a, b) != _conflict_id(a, c)
        assert _conflict_id(a, b) != _conflict_id(b, c)

    def test_empty_uid_falls_back_to_title_start(self):
        a = _event("", title="Standup", start="2026-03-10T09:00:00")
        b = _event("", title="Lunch",   start="2026-03-10T12:00:00")
        # Should not raise and should return a stable string
        cid = _conflict_id(a, b)
        assert isinstance(cid, str) and "|" in cid

    def test_mixed_uid_and_empty(self):
        a = _event("uid-abc")
        b = _event("")
        cid = _conflict_id(a, b)
        assert isinstance(cid, str) and len(cid) > 0

    def test_same_events_same_id(self):
        a = _event("uid-1")
        assert _conflict_id(a, a) == _conflict_id(a, a)


# ── _fmt_event_line ────────────────────────────────────────────────────────────

class TestFmtEventLine:

    def _info(self, title, start, end):
        return {"title": title, "start": start, "end": end, "uid": "", "attendees": []}

    def test_contains_title(self):
        line = _fmt_event_line(self._info("Team Sync", "2026-03-10T09:00:00+00:00", "2026-03-10T10:00:00+00:00"))
        assert "Team Sync" in line

    def test_contains_time_range(self):
        line = _fmt_event_line(self._info("Meeting", "2026-03-10T09:00:00+00:00", "2026-03-10T10:00:00+00:00"))
        # Should contain some time indicator
        assert "AM" in line or "PM" in line or "9" in line

    def test_returns_string(self):
        line = _fmt_event_line(self._info("X", "2026-03-10T14:00:00+00:00", "2026-03-10T15:00:00+00:00"))
        assert isinstance(line, str) and len(line) > 0
