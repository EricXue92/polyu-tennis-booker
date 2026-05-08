"""Static configuration: URLs, slot priorities, and CSS selectors.

Selectors marked `PENDING_DISCOVERY` are placeholders. Run
`scripts/discover_selectors.py` once with real PolyU credentials, inspect the
saved HTML in `artifacts/`, and fill them in here. Until then, any code path
that uses them will raise `RuntimeError` at runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

LOGIN_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
SUBMIT_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"

# Try in this order. Stop after first successful booking.
SLOT_PRIORITY: tuple[tuple[time, time], ...] = (
    (time(19, 30), time(20, 30)),
    (time(18, 30), time(19, 30)),
    (time(20, 30), time(21, 30)),
)

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
    # Login page
    login_username: str | _Pending = field(default_factory=lambda: PENDING_DISCOVERY)
    login_password: str | _Pending = field(default_factory=lambda: PENDING_DISCOVERY)
    login_submit: str | _Pending = field(default_factory=lambda: PENDING_DISCOVERY)

    # Sports facility / activity page
    sports_facility_link: str | _Pending = field(default_factory=lambda: PENDING_DISCOVERY)
    activity_dropdown: str | _Pending = field(default_factory=lambda: PENDING_DISCOVERY)
    activity_tennis_value: str | _Pending = field(default_factory=lambda: PENDING_DISCOVERY)
    search_button: str | _Pending = field(default_factory=lambda: PENDING_DISCOVERY)

    # Search results page
    # Format string: {date} = ISO date YYYY-MM-DD, {start} = HH:MM, {end} = HH:MM
    available_slot_cell: str | _Pending = field(default_factory=lambda: PENDING_DISCOVERY)
    next_button: str | _Pending = field(default_factory=lambda: PENDING_DISCOVERY)

    # Confirmation page (make_book_submit.do)
    agreement_checkbox: str | _Pending = field(default_factory=lambda: PENDING_DISCOVERY)
    submit_button: str | _Pending = field(default_factory=lambda: PENDING_DISCOVERY)
    confirmation_marker: str | _Pending = field(default_factory=lambda: PENDING_DISCOVERY)


SELECTORS = Selectors()
