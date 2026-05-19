"""Static configuration: URLs, slot priorities, and CSS selectors.

Selectors below were discovered by running `scripts/discover_selectors.py`
against the live PolyU booking system on 2026-05-09. If the UI changes,
re-run discovery, inspect `artifacts/*.html`, and update the values here.

Date format conventions used by the live page:
- searchDate input expects DD/MM/YYYY (slashes)
- timeslot cells carry data-slot-date as DD-MM-YYYY (dashes)

The booker formats target_date both ways and passes them through
`available_slot_cell.format(date=..., start=..., end=...)`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time

LOGIN_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
SUBMIT_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"

# Try in this order. Stop after first successful booking.
SLOT_PRIORITY: tuple[tuple[time, time], ...] = (
    (time(19, 30), time(20, 30)),
    (time(18, 30), time(19, 30)),
    (time(20, 30), time(21, 30)),
    (time(17, 30), time(18, 30))
)

# Tuesday 18:30-20:30 is reserved for PolyU staff every week, so those cells
# never become bookable in the search results. Excluding them on Tuesdays
# stops the booker from wasting candidates on always-unavailable slots and
# falling back to the slower retry path.
_TUESDAY_STAFF_RESERVED: frozenset[tuple[time, time]] = frozenset({
    (time(18, 30), time(19, 30)),
    (time(19, 30), time(20, 30)),
})


def slot_priority_for(target_date: date) -> tuple[tuple[time, time], ...]:
    """Return SLOT_PRIORITY with weekday-specific exclusions applied."""
    if target_date.weekday() == 1:  # Tuesday
        return tuple(s for s in SLOT_PRIORITY if s not in _TUESDAY_STAFF_RESERVED)
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

    # Make-booking form. After login we land on make_book.do directly with
    # Sports Facility (dataSetId=18) preselected; the menu click is harmless
    # but kept for symmetry with the manual flow.
    sports_facility_link: str | _Pending = 'a:has-text("Sports Facility")'
    activity_dropdown: str | _Pending = 'select[name="actvId"]'
    activity_tennis_value: str | _Pending = "10"  # <option value="10">Tennis</option>
    search_button: str | _Pending = 'button:has-text("Search")'

    # The searchDate input is a jQuery-UI datepicker (readonly), set via JS.
    # Format: DD/MM/YYYY. The booker calls page.evaluate to set it.
    search_date_input_id: str | _Pending = "searchDate"

    # Search results render in-place (AJAX). A bookable cell:
    #   - has class "timeslot"
    #   - lacks "not-selectable" (which is added for >7-days-ahead) and
    #     "no-vacancy" (which is added for full slots)
    #   - data-slot-date is DD-MM-YYYY, data-slot-start-time is HH:MM
    # Format placeholders: {date}=DD-MM-YYYY, {start}=HH:MM, {end}=HH:MM (unused)
    available_slot_cell: str | _Pending = (
        'div.timeslot:not(.not-selectable):not(.no-vacancy)'
        '[data-slot-date="{date}"][data-slot-start-time="{start}"]'
    )
    next_button: str | _Pending = 'button:visible:has-text("Next")'

    # Confirmation page (make_book_submit.do)
    agreement_checkbox: str | _Pending = 'input[name="declare"]'
    submit_button: str | _Pending = 'button:visible:has-text("Submit")'
    # Success indicator: after Submit we leave make_book_submit.do. The booker
    # uses URL change as the primary success signal; this marker is a
    # belt-and-braces text check. Common PolyU success pages contain
    # "successful" / "successfully" / a reference number.
    confirmation_marker: str | _Pending = 'text=/successful|reference no/i'


SELECTORS = Selectors()
