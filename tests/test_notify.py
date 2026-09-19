"""Offline tests for the student-account result email (src/notify.py).

No SMTP connection is ever opened: `send_report` takes an injectable sender,
and these tests pass a recording fake.
"""
from datetime import date, datetime, time

import pytest

from src.http_client import AvailableSlot
from src.notify import AccountResult, build_report, send_report

_TARGET = date(2026, 9, 26)  # Saturday
_SLOTS = [(time(17, 30), time(18, 30)), (time(19, 30), time(20, 30))]


def _slot(hour: int, fid: int = 11) -> AvailableSlot:
    return AvailableSlot(
        facility_id=fid,
        facility_name=f"Tennis Court No. {fid - 9}",
        center_id=1,
        center_name="Shaw Sports Complex",
        start_dt=datetime(2026, 9, 26, hour, 30),
        end_dt=datetime(2026, 9, 26, hour + 1, 30),
    )


def _booked(name: str = "student", notify: bool = True) -> AccountResult:
    return AccountResult(name=name, notify=notify, slots=_SLOTS, ok=True, booked=_slot(19))


def _failed(name: str = "student", reason: str = "没抢到") -> AccountResult:
    return AccountResult(name=name, notify=True, slots=_SLOTS, ok=False, reason=reason)


class _Sender:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._exc = exc

    def __call__(self, msg, username, password) -> None:
        self.calls.append((msg, username, password))
        if self._exc is not None:
            raise self._exc


@pytest.fixture
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_USERNAME", "me@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-pass-123")
    monkeypatch.delenv("NOTIFY_EMAIL_TO", raising=False)


def test_build_report_booked_has_date_time_and_court():
    subject, body = build_report(_TARGET, [_booked()], dry_run=False)
    assert "2026-09-26" in subject and "19:30" in subject and "Tennis Court No. 2" in subject
    assert "2026-09-26" in body
    assert "19:30-20:30" in body
    assert "Tennis Court No. 2" in body


def test_build_report_failed_has_reason_and_tried_slots():
    subject, body = build_report(_TARGET, [_failed(reason="登录失败")], dry_run=False)
    assert "未订到" in subject and "2026-09-26" in subject
    assert "登录失败" in body
    assert "17:30-18:30" in body and "19:30-20:30" in body


def test_build_report_covers_every_notify_account():
    subject, body = build_report(
        _TARGET, [_booked("student"), _failed("student2")], dry_run=False,
    )
    assert "student" in body and "student2" in body
    assert "订到" in subject and "未订到" in subject


def test_build_report_skips_accounts_not_flagged_for_notify():
    assert build_report(_TARGET, [_booked("staff", notify=False)], dry_run=False) is None
    _subject, body = build_report(
        _TARGET, [_booked("staff", notify=False), _failed("student")], dry_run=False,
    )
    assert "staff" not in body


def test_build_report_marks_dry_run():
    dry = AccountResult(name="student", notify=True, slots=_SLOTS, ok=True)
    subject, body = build_report(_TARGET, [dry], dry_run=True)
    assert "DRY RUN" in subject
    assert "DRY RUN" in body


@pytest.mark.asyncio
async def test_send_report_sends_to_smtp_username_by_default(smtp_env):
    sender = _Sender()
    assert await send_report(_TARGET, [_booked()], dry_run=False, sender=sender) is True
    (msg, username, password), = sender.calls
    assert msg["To"] == "me@example.com" and msg["From"] == "me@example.com"
    assert "19:30" in msg["Subject"]
    assert (username, password) == ("me@example.com", "app-pass-123")


@pytest.mark.asyncio
async def test_send_report_honours_notify_email_to(smtp_env, monkeypatch):
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "other@example.com")
    sender = _Sender()
    await send_report(_TARGET, [_booked()], dry_run=False, sender=sender)
    assert sender.calls[0][0]["To"] == "other@example.com"


@pytest.mark.asyncio
async def test_send_report_no_notify_accounts_sends_nothing(smtp_env):
    sender = _Sender()
    sent = await send_report(_TARGET, [_booked("staff", notify=False)], dry_run=False, sender=sender)
    assert sent is False and sender.calls == []


@pytest.mark.asyncio
async def test_send_report_missing_credentials_skips_without_raising(monkeypatch):
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    sender = _Sender()
    assert await send_report(_TARGET, [_booked()], dry_run=False, sender=sender) is False
    assert sender.calls == []


@pytest.mark.asyncio
async def test_send_report_swallows_sender_errors(smtp_env):
    sender = _Sender(exc=OSError("smtp down"))
    assert await send_report(_TARGET, [_booked()], dry_run=False, sender=sender) is False
    assert len(sender.calls) == 1
