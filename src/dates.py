"""Date and time helpers anchored to Asia/Hong_Kong."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

HKT = ZoneInfo("Asia/Hong_Kong")
DAYS_AHEAD = 7


def now_hkt() -> datetime:
    return datetime.now(tz=HKT)


def compute_target_date() -> date:
    """Return the date `DAYS_AHEAD` days after the current HKT calendar day."""
    return now_hkt().date() + timedelta(days=DAYS_AHEAD)
