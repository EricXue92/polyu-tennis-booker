"""Tests for weekday-specific slot priority rules in src/config.py."""
from datetime import date, time

from src.config import SLOT_PRIORITY, slot_priority_for


def test_weekday_returns_base_priority():
    # 2026-09-04 is a Friday.
    assert slot_priority_for(date(2026, 9, 4)) == SLOT_PRIORITY


def test_tuesday_is_rest_day():
    # 2026-09-01 is a Tuesday.
    assert slot_priority_for(date(2026, 9, 1)) == ()


def test_saturday_appends_late_evening_slots():
    # 2026-09-05 is a Saturday: base slots first, then 20:30 and 21:30
    # as lower-priority fallbacks.
    assert slot_priority_for(date(2026, 9, 5)) == SLOT_PRIORITY + (
        (time(20, 30), time(21, 30)),
        (time(21, 30), time(22, 30)),
    )


def test_sunday_appends_late_evening_slots():
    # 2026-09-06 is a Sunday.
    assert slot_priority_for(date(2026, 9, 6)) == SLOT_PRIORITY + (
        (time(20, 30), time(21, 30)),
        (time(21, 30), time(22, 30)),
    )
