"""Static configuration: URLs, slot priorities, and CSS selectors.

Selectors below were discovered by running `scripts/discover_selectors.py`
against the live PolyU booking system on 2026-05-09. If the UI changes,
re-run discovery, inspect `artifacts/*.html`, and update the values here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time

LOGIN_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
SUBMIT_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"

# --- Tennis activity HTTP-API constants (derived from artifacts/http_trace.json) ---
# These are the form-field values PolyU's booking POSTs require for the Tennis
# activity. They are stable across runs — captured from a real booking and
# unchanged for the lifetime of PolyU's current booking system. Re-confirm by
# running scripts/capture_http.py if a booking starts failing with 4xx/5xx.

TIMETABLE_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/timetable.json"
MAKE_BOOK_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
MAKE_BOOK_SUBMIT_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
MAKE_BOOK_RESULT_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_result.do"

TENNIS_DATA_SET_ID = 18
TENNIS_ACTV_ID = 10
TENNIS_CTR_ID = 1
TENNIS_CENTER_NAME = "Shaw Sports Complex"
TENNIS_FACILITIES = {
    10: "Tennis Court No. 1",
    11: "Tennis Court No. 2",
}

# Try in this order. Stop after first successful booking.
SLOT_PRIORITY: tuple[tuple[time, time], ...] = (
    (time(18, 30), time(19, 30)),
    (time(19, 30), time(20, 30)),
)

# Tuesdays are a rest day — no court is booked at all. Tuesday 18:30-20:30
# is staff-reserved anyway, and the remaining priority slots aren't wanted,
# so the booker short-circuits to a no-op success when target_date is Tuesday.
_REST_WEEKDAYS: frozenset[int] = frozenset({1})  # Mon=0, Tue=1, ...


def slot_priority_for(target_date: date) -> tuple[tuple[time, time], ...]:
    """Return SLOT_PRIORITY with weekday-specific exclusions applied.

    Returns an empty tuple on rest weekdays so the booker can skip the run.
    """
    if target_date.weekday() in _REST_WEEKDAYS:
        return ()
    return SLOT_PRIORITY

TRIGGER_TIME_HKT = time(8, 30, 0)


class _Pending:
    """Sentinel marking a selector that has not yet been filled in."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "PENDING_DISCOVERY"

    def __bool__(self) -> bool:
        return False


PENDING_DISCOVERY = _Pending()


def require(value: object, name: str) -> str:
    if isinstance(value, _Pending) or value is None:
        raise RuntimeError(
            f"Selector {name!r} is not configured. "
            f"Run scripts/discover_selectors.py and fill it into src/config.py."
        )
    assert isinstance(value, str)
    return value


@dataclass(frozen=True)
class Selectors:
    # Login form (POSS j_security_check)
    login_username: str | _Pending = 'input[name="j_username"]'
    login_password: str | _Pending = 'input[name="j_password"]'
    login_submit: str | _Pending = 'button[type="submit"][name="buttonAction"]'

SELECTORS = Selectors()
