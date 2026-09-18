"""Offline tests for the multi-account orchestration in src/booker.py.

Playwright login is not exercised here; these cover the pure pieces that
decide which accounts run and how their concurrent results collapse into
one exit code.
"""
import logging
from datetime import date, time

import pytest

from src.config import ACCOUNTS, STAFF_ACCOUNT, STUDENT_ACCOUNT, STUDENT_SITE
from src.http_client import BookingResult, CellOutcome
from tests.test_bootstrap import _FakePage, _FIXTURE_HTML
from tests.test_http_booker import _FakeClient


def test_active_jobs_both_accounts_on_weekend():
    from src.booker import active_jobs

    jobs = active_jobs(date(2026, 9, 19), ACCOUNTS)  # Saturday
    assert [a.name for a, _ in jobs] == ["staff", "student"]
    staff_slots = dict(jobs)[STAFF_ACCOUNT]
    student_slots = dict(jobs)[STUDENT_ACCOUNT]
    assert staff_slots == [(time(18, 30), time(19, 30)), (time(20, 30), time(21, 30))]
    assert student_slots == [
        (time(17, 30), time(18, 30)),
        (time(19, 30), time(20, 30)),
        (time(21, 30), time(22, 30)),
    ]


def test_active_jobs_staff_only_on_ordinary_day():
    from src.booker import active_jobs

    jobs = active_jobs(date(2026, 9, 17), ACCOUNTS)  # Thursday
    assert [a.name for a, _ in jobs] == ["staff"]


def test_active_jobs_staff_and_student2_on_student2_target_date():
    from src.booker import active_jobs

    jobs = active_jobs(date(2026, 9, 28), ACCOUNTS)  # Monday, a student2 date
    assert [a.name for a, _ in jobs] == ["staff", "student2"]
    assert jobs[1][1] == [(time(20, 30), time(21, 30)), (time(21, 30), time(22, 30))]


def test_active_jobs_empty_on_rest_day():
    from src.booker import active_jobs

    assert active_jobs(date(2026, 9, 15), ACCOUNTS) == []  # Tuesday


@pytest.mark.asyncio
async def test_bootstrap_passes_site_to_client():
    from src.booker import bootstrap_http_client

    page = _FakePage(
        html=_FIXTURE_HTML,
        cookies=[{"name": "JSESSIONID", "value": "x", "domain": "www40.polyu.edu.hk", "path": "/starspossfbstud"}],
    )
    client = await bootstrap_http_client(page, log=logging.getLogger("t"), site=STUDENT_SITE)
    try:
        assert client.site is STUDENT_SITE
    finally:
        await client.aclose()


def _job(account, client, slots):
    from src.booker import AccountJob
    return AccountJob(account=account, slots=slots, client=client, log=logging.getLogger(f"t.{account.name}"))


@pytest.mark.asyncio
async def test_book_all_returns_zero_when_every_account_books():
    from src.booker import book_all

    target = date(2026, 9, 18)
    slots = [(time(18, 30), time(19, 30))]
    staff = _FakeClient(
        {(18, 10): CellOutcome.ACCEPTED, (18, 11): CellOutcome.ACCEPTED},
        {(18, 10): BookingResult.SUCCESS, (18, 11): BookingResult.OCCUPIED},
    )
    student = _FakeClient(
        {(18, 10): CellOutcome.OCCUPIED, (18, 11): CellOutcome.ACCEPTED},
        {(18, 11): BookingResult.SUCCESS},
    )
    rc = await book_all(
        [_job(STAFF_ACCOUNT, staff, slots), _job(STUDENT_ACCOUNT, student, slots)],
        target, dry_run=False, log=logging.getLogger("t"),
    )
    assert rc == 0
    assert len(staff.submit_calls) == 2 and len(student.submit_calls) == 1


@pytest.mark.asyncio
async def test_book_all_returns_one_when_any_account_fails():
    from src.booker import book_all

    target = date(2026, 9, 18)
    slots = [(time(18, 30), time(19, 30))]
    staff = _FakeClient(
        {(18, 10): CellOutcome.ACCEPTED, (18, 11): CellOutcome.ACCEPTED},
        {(18, 10): BookingResult.SUCCESS, (18, 11): BookingResult.OCCUPIED},
    )
    student = _FakeClient({(18, 10): CellOutcome.OCCUPIED, (18, 11): CellOutcome.OCCUPIED})
    rc = await book_all(
        [_job(STAFF_ACCOUNT, staff, slots), _job(STUDENT_ACCOUNT, student, slots)],
        target, dry_run=False, log=logging.getLogger("t"),
    )
    assert rc == 1
    # The staff booking still went through — one account failing must not
    # abort the other.
    assert len(staff.submit_calls) == 2


@pytest.mark.asyncio
async def test_book_all_runs_accounts_concurrently():
    from src.booker import book_all

    target = date(2026, 9, 18)
    slots = [(time(18, 30), time(19, 30))]
    outcomes = {(18, 10): CellOutcome.ACCEPTED, (18, 11): CellOutcome.OCCUPIED}
    results = {(18, 10): BookingResult.SUCCESS}
    staff = _FakeClient(outcomes, results, cell_click_sleep_s=0.3)
    student = _FakeClient(outcomes, results, cell_click_sleep_s=0.3)
    import time as _t
    t0 = _t.perf_counter()
    rc = await book_all(
        [_job(STAFF_ACCOUNT, staff, slots), _job(STUDENT_ACCOUNT, student, slots)],
        target, dry_run=False, log=logging.getLogger("t"),
    )
    elapsed = _t.perf_counter() - t0
    assert rc == 0
    # Serial would be >= 0.6s; concurrent lands near 0.3s.
    assert elapsed < 0.5


def test_resolve_target_date_default_is_seven_days_ahead():
    from freezegun import freeze_time

    from src.booker import resolve_target_date

    with freeze_time("2026-09-09 10:00:00+08:00"):
        assert resolve_target_date(None) == date(2026, 9, 16)


def test_resolve_target_date_override_parses_iso():
    from src.booker import resolve_target_date

    assert resolve_target_date("2026-09-18") == date(2026, 9, 18)
