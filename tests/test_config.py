"""Tests for weekday-specific slot priority rules in src/config.py."""
from datetime import date, time

from src.config import SLOT_PRIORITY, slot_priority_for


def test_staff_sits_out_weekdays():
    # Mon 2026-09-14 .. Fri 2026-09-18: weekdays are student2's (Wed/Fri) or
    # nobody's since 2026-10-08.
    for day in range(14, 19):
        assert slot_priority_for(date(2026, 9, day)) == ()


def test_saturday_staff_takes_even_hours_only():
    # 2026-09-05 is a Saturday. On weekends the staff account stays on the
    # "even" hours (18:30, 20:30) so it can never overlap the student account,
    # which takes the "odd" hours (17:30, 19:30, 21:30).
    assert slot_priority_for(date(2026, 9, 5)) == (
        (time(18, 30), time(19, 30)),
        (time(20, 30), time(21, 30)),
    )


def test_sunday_staff_takes_even_hours_only():
    # 2026-09-06 is a Sunday.
    assert slot_priority_for(date(2026, 9, 6)) == (
        (time(18, 30), time(19, 30)),
        (time(20, 30), time(21, 30)),
    )


# --- Site / Account (dual-account booking) ---

def test_site_derives_urls_from_base_path():
    from src.config import Site

    site = Site("starspossfbstud")
    prefix = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/"
    assert site.login_url == prefix + "make_book.do"
    assert site.make_book_url == prefix + "make_book.do"
    assert site.make_book_submit_url == prefix + "make_book_submit.do"
    assert site.make_book_result_url == prefix + "make_book_result.do"
    assert site.timetable_url == prefix + "timetable.json"


def test_staff_site_matches_legacy_url_constants():
    from src.config import LOGIN_URL, MAKE_BOOK_SUBMIT_URL, STAFF_SITE

    assert STAFF_SITE.base_path == "starspossfbns"
    assert STAFF_SITE.login_url == LOGIN_URL
    assert STAFF_SITE.make_book_submit_url == MAKE_BOOK_SUBMIT_URL


def test_student_books_odd_hours_on_weekends():
    from src.config import student_slot_priority_for

    expected = (
        (time(17, 30), time(18, 30)),
        (time(19, 30), time(20, 30)),
        (time(21, 30), time(22, 30)),
    )
    assert student_slot_priority_for(date(2026, 9, 19)) == expected  # Saturday
    assert student_slot_priority_for(date(2026, 9, 20)) == expected  # Sunday


def test_student_sits_out_weekdays():
    from src.config import student_slot_priority_for

    for day in range(14, 19):  # Mon 2026-09-14 .. Fri 2026-09-18
        assert student_slot_priority_for(date(2026, 9, day)) == ()


def test_weekend_staff_and_student_slots_never_overlap():
    from src.config import student_slot_priority_for

    for d in (date(2026, 9, 19), date(2026, 9, 20)):
        staff = set(slot_priority_for(d))
        student = set(student_slot_priority_for(d))
        assert staff and student
        assert staff.isdisjoint(student)


def test_student2_books_evening_ladder_on_wednesday_and_friday():
    from src.config import student2_slot_priority_for

    expected = (
        (time(18, 30), time(19, 30)),
        (time(19, 30), time(20, 30)),
        (time(20, 30), time(21, 30)),
    )
    assert SLOT_PRIORITY == expected
    assert student2_slot_priority_for(date(2026, 10, 14)) == expected  # Wednesday
    assert student2_slot_priority_for(date(2026, 10, 16)) == expected  # Friday


def test_student2_sits_out_other_days():
    from src.config import student2_slot_priority_for

    for d in (
        date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 15),  # Mon, Tue, Thu
        date(2026, 10, 17), date(2026, 10, 18),  # Sat, Sun
    ):
        assert student2_slot_priority_for(d) == ()


def test_accounts_staff_first_then_students():
    from src.config import (
        ACCOUNTS, STAFF_SITE, STUDENT_SITE, slot_priority_for,
        student2_slot_priority_for, student_slot_priority_for,
    )

    staff, student, student2 = ACCOUNTS
    assert (student2.name, student2.site, student2.username_env, student2.password_env) == (
        "student2", STUDENT_SITE, "POLYU_STUDENT2_USERNAME", "POLYU_STUDENT2_PASSWORD")
    assert student2.slot_priority is student2_slot_priority_for
    assert (staff.name, staff.site, staff.username_env, staff.password_env) == (
        "staff", STAFF_SITE, "POLYU_USERNAME", "POLYU_PASSWORD")
    assert staff.slot_priority is slot_priority_for
    assert (student.name, student.site, student.username_env, student.password_env) == (
        "student", STUDENT_SITE, "POLYU_STUDENT_USERNAME", "POLYU_STUDENT_PASSWORD")
    assert student.slot_priority is student_slot_priority_for
    assert STUDENT_SITE.base_path == "starspossfbstud"
