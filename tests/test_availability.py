"""
Tests for availability.py — pure scheduling logic only.
No Slack, no ICS fetching, no Claude API calls.
"""

import pytest
from datetime import date, datetime, time, timedelta

from email_to_motion.availability import (
    TORONTO_TZ,
    WORK_END,
    WORK_START,
    _parse_availability_args,
    _round_up_to_half_hour,
    compute_free_slots,
    parse_date_range,
)

# A Monday safely in the future — used as a fixed test day for slot tests.
TEST_DAY = date(2027, 6, 14)


def at(h: int, m: int) -> datetime:
    """Build a Toronto-aware datetime on TEST_DAY at h:m."""
    return TORONTO_TZ.localize(datetime.combine(TEST_DAY, time(h, m)))


def busy(h_start: int, m_start: int, h_end: int, m_end: int):
    """Convenience factory for a busy block on TEST_DAY."""
    return (at(h_start, m_start), at(h_end, m_end))


# ── parse_date_range ───────────────────────────────────────────────────────────

class TestParseDateRange:

    def test_compact_same_month(self):
        start, end = parse_date_range("Apr 1-15")
        assert start.month == 4 and start.day == 1
        assert end.month == 4 and end.day == 15
        assert start.year == end.year

    def test_written_with_to(self):
        start, end = parse_date_range("April 5 to April 10")
        assert start.day == 5 and end.day == 10
        assert start.month == end.month == 4

    def test_spaced_hyphen(self):
        start, end = parse_date_range("May 1 - May 20")
        assert start.day == 1 and end.day == 20
        assert start.month == end.month == 5

    def test_en_dash_normalised(self):
        # en-dash should be treated the same as a hyphen
        start, end = parse_date_range("Apr 3\u20137")
        assert start.day == 3 and end.day == 7

    def test_em_dash_normalised(self):
        start, end = parse_date_range("Apr 3\u20147")
        assert start.day == 3 and end.day == 7

    def test_past_month_rolls_to_next_year(self):
        # January 2026 has already passed (today ≈ March 2026)
        start, end = parse_date_range("Jan 5-10")
        assert start >= date.today()

    def test_start_before_end(self):
        start, end = parse_date_range("Jun 3-20")
        assert start <= end

    def test_invalid_text_raises(self):
        with pytest.raises(ValueError):
            parse_date_range("not a date at all")

    def test_garbage_month_raises(self):
        with pytest.raises(ValueError):
            parse_date_range("Xyz 1-5")


# ── _parse_availability_args ───────────────────────────────────────────────────

class TestParseAvailabilityArgs:

    def test_no_duration_defaults_30(self):
        text, dur = _parse_availability_args("Mar 10-14")
        assert text == "Mar 10-14"
        assert dur == 30

    def test_trailing_integer_extracted(self):
        text, dur = _parse_availability_args("Mar 10-14 60")
        assert text == "Mar 10-14"
        assert dur == 60

    def test_leading_and_trailing_spaces_stripped(self):
        text, dur = _parse_availability_args("  Apr 1-5  90  ")
        assert text == "Apr 1-5"
        assert dur == 90

    def test_duration_not_left_in_text(self):
        text, dur = _parse_availability_args("May 3 - May 7 45")
        assert "45" not in text
        assert dur == 45

    def test_multi_word_range_with_duration(self):
        text, dur = _parse_availability_args("June 1 to June 5 90")
        assert dur == 90
        assert "90" not in text


# ── _round_up_to_half_hour ────────────────────────────────────────────────────

class TestRoundUpToHalfHour:

    def _dt(self, h: int, m: int) -> datetime:
        return TORONTO_TZ.localize(datetime(2026, 4, 1, h, m, 0))

    def test_on_the_hour_unchanged(self):
        assert _round_up_to_half_hour(self._dt(10, 0)) == self._dt(10, 0)

    def test_on_the_half_hour_unchanged(self):
        assert _round_up_to_half_hour(self._dt(10, 30)) == self._dt(10, 30)

    def test_one_minute_past_hour_rounds_to_half(self):
        assert _round_up_to_half_hour(self._dt(10, 1)) == self._dt(10, 30)

    def test_twenty_nine_past_rounds_to_half(self):
        assert _round_up_to_half_hour(self._dt(10, 29)) == self._dt(10, 30)

    def test_one_past_half_rounds_to_next_hour(self):
        assert _round_up_to_half_hour(self._dt(10, 31)) == self._dt(11, 0)

    def test_fifty_nine_rounds_to_next_hour(self):
        assert _round_up_to_half_hour(self._dt(10, 59)) == self._dt(11, 0)

    def test_result_always_on_even_boundary(self):
        for m in range(60):
            result = _round_up_to_half_hour(self._dt(10, m))
            assert result.minute in (0, 30)

    def test_result_never_before_input(self):
        for m in range(60):
            dt = self._dt(10, m)
            assert _round_up_to_half_hour(dt) >= dt


# ── compute_free_slots ────────────────────────────────────────────────────────

class TestComputeFreeSlots:

    def test_no_busy_blocks_is_full_day(self):
        slots = compute_free_slots(TEST_DAY, [])
        assert len(slots) == 1
        s, e = slots[0]
        assert s == TORONTO_TZ.localize(datetime.combine(TEST_DAY, WORK_START))
        assert e == TORONTO_TZ.localize(datetime.combine(TEST_DAY, WORK_END))

    def test_midday_meeting_splits_day(self):
        slots = compute_free_slots(TEST_DAY, [busy(11, 0, 13, 0)])
        assert len(slots) == 2
        # Morning slot ends at 11:00
        assert slots[0][1] == at(11, 0)
        # Afternoon slot starts at 13:00
        assert slots[1][0] == at(13, 0)

    def test_busy_at_start_leaves_afternoon(self):
        slots = compute_free_slots(TEST_DAY, [busy(9, 0, 12, 0)])
        assert len(slots) == 1
        s, _ = slots[0]
        assert s >= at(12, 0)

    def test_busy_at_end_leaves_morning(self):
        slots = compute_free_slots(TEST_DAY, [busy(15, 0, 16, 30)])
        assert len(slots) == 1
        _, e = slots[0]
        # Free slot runs from work start up to the start of the busy block
        assert e == at(15, 0)

    def test_fully_booked_day_returns_empty(self):
        slots = compute_free_slots(TEST_DAY, [busy(9, 0, 16, 30)])
        assert slots == []

    def test_overlapping_blocks_merged(self):
        # Two overlapping blocks should produce the same result as one merged block
        overlapping = [busy(10, 0, 12, 0), busy(11, 0, 13, 0)]
        single      = [busy(10, 0, 13, 0)]
        assert compute_free_slots(TEST_DAY, overlapping) == compute_free_slots(TEST_DAY, single)

    def test_adjacent_blocks_merged(self):
        adjacent = [busy(10, 0, 11, 0), busy(11, 0, 12, 0)]
        single   = [busy(10, 0, 12, 0)]
        assert compute_free_slots(TEST_DAY, adjacent) == compute_free_slots(TEST_DAY, single)

    def test_min_duration_filters_short_slots(self):
        # Only a 20-minute gap between 9:20 and 9:40 — too short for 30-min meeting
        blocks = [busy(9, 0, 9, 20), busy(9, 40, 16, 30)]
        slots  = compute_free_slots(TEST_DAY, blocks, min_duration=timedelta(minutes=30))
        for s, e in slots:
            assert e - s >= timedelta(minutes=30)

    def test_slot_start_times_on_half_hour_boundaries(self):
        # Busy ends at 10:15 — free slot should start at 10:30 (rounded up)
        slots = compute_free_slots(TEST_DAY, [busy(9, 0, 10, 15)])
        assert len(slots) >= 1
        s = slots[0][0]
        assert s.minute in (0, 30)

    def test_free_slots_do_not_overlap_busy_blocks(self):
        b = busy(11, 0, 14, 0)
        slots = compute_free_slots(TEST_DAY, [b])
        for s, e in slots:
            # No free slot should overlap with the busy block
            assert e <= b[0] or s >= b[1]
