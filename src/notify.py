"""Result email for the student account.

After every account has finished booking, one email goes to the owner listing
each `notify_result` account's outcome — booked (date, hour, court) or not
(reason + the hours it tried). Only the weekend `student` account carries the
flag; staff and student2 are left out, and a run with no flagged account
sends nothing.

This runs strictly after the 08:30 hot path and is best-effort: a missing SMTP
secret or a send failure is logged and swallowed, and never changes the run's
exit code (which still drives GitHub's failure email).

Credentials come from SMTP_USERNAME / SMTP_PASSWORD (a Gmail app password).
The recipient is NOTIFY_EMAIL_TO, defaulting to SMTP_USERNAME — the repo is
public, so no address is hardcoded here.
"""
from __future__ import annotations

import asyncio
import os
import smtplib
from dataclasses import dataclass
from datetime import date, time
from email.message import EmailMessage
from typing import Callable, Sequence

from src.http_client import AvailableSlot
from src.log import build_logger

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_TIMEOUT_SECONDS = 30

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


@dataclass(frozen=True)
class AccountResult:
    """One account's final outcome for the run.

    `ok` mirrors the account's exit code; `booked` is the winning slot and is
    None both on failure and on a dry run (ok=True, nothing actually booked).
    """
    name: str
    notify: bool
    slots: Sequence[tuple[time, time]]
    ok: bool
    booked: AvailableSlot | None = None
    reason: str = ""


def _hours(start: time, end: time) -> str:
    return f"{start:%H:%M}-{end:%H:%M}"


def build_report(
    target_date: date,
    results: Sequence[AccountResult],
    *,
    dry_run: bool,
) -> tuple[str, str] | None:
    """Return (subject, body), or None when no account wants a report."""
    wanted = [r for r in results if r.notify]
    if not wanted:
        return None

    day = f"{target_date.isoformat()} ({_WEEKDAYS[target_date.weekday()]})"
    headlines: list[str] = []
    lines = [f"日期: {day}", ""]
    for r in wanted:
        if r.booked is not None:
            slot = r.booked
            headlines.append(f"{r.name} 订到 {slot.start_dt:%H:%M} {slot.facility_name}")
            lines += [
                f"账号 {r.name}: 已订到",
                f"  时间: {_hours(slot.start_dt.time(), slot.end_dt.time())}",
                f"  场地: {slot.facility_name} ({slot.center_name})",
            ]
        elif r.ok:
            headlines.append(f"{r.name} 未实际订场")
            lines += [
                f"账号 {r.name}: DRY RUN，登录正常，未实际订场",
                f"  计划时段: {', '.join(_hours(s, e) for s, e in r.slots)}",
            ]
        else:
            headlines.append(f"{r.name} 未订到")
            lines += [
                f"账号 {r.name}: 未订到",
                f"  原因: {r.reason or '未知'}",
                f"  尝试时段: {', '.join(_hours(s, e) for s, e in r.slots)}",
            ]
        lines.append("")

    prefix = "[网球][DRY RUN]" if dry_run else "[网球]"
    subject = f"{prefix} {target_date.isoformat()} " + "; ".join(headlines)
    return subject, "\n".join(lines)


def _smtp_send(msg: EmailMessage, username: str, password: str) -> None:
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
        smtp.login(username, password)
        smtp.send_message(msg)


async def send_report(
    target_date: date,
    results: Sequence[AccountResult],
    *,
    dry_run: bool,
    sender: Callable[[EmailMessage, str, str], None] = _smtp_send,
) -> bool:
    """Email the report. Returns True if sent; never raises."""
    username = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    log = build_logger("booker", secret=password, session_id="notify")

    report = build_report(target_date, results, dry_run=dry_run)
    if report is None:
        return False
    if not username or not password:
        log.warning("SMTP_USERNAME/SMTP_PASSWORD not set; skipping result email")
        return False

    subject, body = report
    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = os.environ.get("NOTIFY_EMAIL_TO") or username
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        await asyncio.to_thread(sender, msg, username, password)
    except Exception as exc:  # noqa: BLE001 - the email must never fail the run
        log.warning("result email failed: %r", exc)
        return False
    log.info("result email sent: %s", subject)
    return True
