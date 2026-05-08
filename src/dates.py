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


import time as _time_mod
from datetime import time as dtime

# Re-exported so tests can patch a stable path.
time = _time_mod


def seconds_until_hkt_time(target: dtime) -> float:
    """Seconds from now until today's `target` time in HKT.

    Returns 0.0 if the target has already passed today (we never wait until
    tomorrow — the workflow runs once per day, so missing the window today
    means failing the run, not delaying 24 hours).
    """
    now = now_hkt()
    today_target = now.replace(
        hour=target.hour, minute=target.minute, second=target.second,
        microsecond=0,
    )
    delta = (today_target - now).total_seconds()
    return max(delta, 0.0)


def sleep_until_hkt(target: dtime) -> None:
    """Block until HKT clock reaches `target` today, or return immediately if past."""
    delay = seconds_until_hkt_time(target)
    if delay > 0:
        time.sleep(delay)
