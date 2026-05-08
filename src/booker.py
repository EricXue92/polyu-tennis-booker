"""Main booking orchestration."""
from __future__ import annotations

import logging
from datetime import date, time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import Page

from src.config import LOGIN_URL, SELECTORS, SLOT_PRIORITY, SUBMIT_URL, require


class BookingFailed(RuntimeError):
    pass

ARTIFACTS = Path("artifacts")
DEFAULT_TIMEOUT_MS = 20_000

SlotProbe = Callable[[date, time, time], Awaitable[bool]]


async def login(page: Page, username: str, password: str, log: logging.Logger) -> None:
    log.info("loading login page")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    # POSS issues a meta-refresh redirect to loginhome.do; wait for the form.
    await page.wait_for_selector(
        require(SELECTORS.login_username, "login_username"),
        timeout=DEFAULT_TIMEOUT_MS,
    )
    await page.fill(require(SELECTORS.login_username, "login_username"), username)
    await page.fill(require(SELECTORS.login_password, "login_password"), password)
    log.info("submitting login")
    await page.click(require(SELECTORS.login_submit, "login_submit"))
    await page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)
    log.info("login complete")


async def navigate_to_search(
    page: Page,
    target_date: date,
    log: logging.Logger,
) -> None:
    """Open Sports Facility -> Tennis, set date filter, click Search.

    Search renders results in-place via AJAX, so we wait for the timetable
    table to appear rather than for full-page navigation.
    """
    log.info("opening sports facility menu")
    # Menu is href="#"; clicking opens the dropdown but doesn't navigate.
    # We're already on make_book.do after login, so this is mostly cosmetic.
    try:
        await page.locator(
            require(SELECTORS.sports_facility_link, "sports_facility_link")
        ).first.click(timeout=3000)
    except Exception as e:
        log.info("sports facility menu click skipped: %s", e)

    log.info("selecting Tennis activity")
    await page.select_option(
        require(SELECTORS.activity_dropdown, "activity_dropdown"),
        require(SELECTORS.activity_tennis_value, "activity_tennis_value"),
    )
    # Allow any dependent dropdowns to settle.
    await page.wait_for_timeout(500)

    log.info("setting search date to %s", target_date)
    date_str = target_date.strftime("%d/%m/%Y")
    input_id = require(SELECTORS.search_date_input_id, "search_date_input_id")
    # The datepicker input is readonly; set via jQuery and trigger change.
    await page.evaluate(
        f"(val) => {{ $('#{input_id}').val(val).trigger('change'); }}",
        date_str,
    )

    log.info("clicking Search")
    await page.locator(
        require(SELECTORS.search_button, "search_button")
    ).first.click()
    # Wait for the timetable to render. table.tt-timetable is the result grid.
    await page.wait_for_selector(
        "table.tt-timetable", timeout=DEFAULT_TIMEOUT_MS
    )
    log.info("search results loaded")


async def pick_slot(
    target_date: date,
    is_available: SlotProbe,
) -> Optional[tuple[time, time]]:
    """Return the first available (start, end) slot in priority order, else None.

    `is_available` probes the live page (or a fake, in tests). Iteration order
    matches SLOT_PRIORITY exactly so failing slots can be diagnosed by log.
    """
    for start, end in SLOT_PRIORITY:
        if await is_available(target_date, start, end):
            return start, end
    return None


async def slot_has_availability(
    page: Page,
    target_date: date,
    start: time,
    end: time,
) -> bool:
    """Probe the search results page for a bookable cell of this slot."""
    selector = require(
        SELECTORS.available_slot_cell, "available_slot_cell"
    ).format(
        date=target_date.strftime("%d-%m-%Y"),
        start=start.strftime("%H:%M"),
        end=end.strftime("%H:%M"),
    )
    return await page.locator(selector).count() > 0


async def book_slot(
    page: Page,
    target_date: date,
    start: time,
    end: time,
    *,
    dry_run: bool,
    log: logging.Logger,
) -> None:
    """Click an available cell, advance to confirmation, submit, verify.

    Success is detected by URL navigating away from SUBMIT_URL after clicking
    Submit (the confirmation_marker text is checked too as a sanity signal,
    but URL change is authoritative). On failure raises BookingFailed.
    """
    cell_selector = require(
        SELECTORS.available_slot_cell, "available_slot_cell"
    ).format(
        date=target_date.strftime("%d-%m-%Y"),
        start=start.strftime("%H:%M"),
        end=end.strftime("%H:%M"),
    )
    log.info("clicking available cell for %s %s-%s", target_date, start, end)
    await page.locator(cell_selector).first.click()
    await page.wait_for_timeout(800)  # let cell-selection state settle

    log.info("clicking Next")
    await page.locator(
        require(SELECTORS.next_button, "next_button")
    ).first.click()
    await page.wait_for_url(f"**{SUBMIT_URL.split('//')[1]}", timeout=DEFAULT_TIMEOUT_MS)
    await page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)
    ARTIFACTS.mkdir(exist_ok=True)
    await page.screenshot(path=str(ARTIFACTS / "pre_submit.png"))

    log.info("ticking agreement checkbox")
    await page.check(require(SELECTORS.agreement_checkbox, "agreement_checkbox"))

    if dry_run:
        log.info("DRY RUN: stopping before final Submit")
        return

    log.info("clicking Submit")
    await page.locator(
        require(SELECTORS.submit_button, "submit_button")
    ).first.click()
    # Success: URL navigates away from make_book_submit.do.
    try:
        await page.wait_for_url(
            lambda url: SUBMIT_URL not in url, timeout=DEFAULT_TIMEOUT_MS
        )
    except Exception:
        # No nav happened — still on the submit page, likely an error banner.
        await page.screenshot(path=str(ARTIFACTS / "post_submit.png"))
        body = await page.content()
        raise BookingFailed(
            f"still on {SUBMIT_URL} after Submit; page (truncated): {body[:500]!r}"
        )
    await page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)
    await page.screenshot(path=str(ARTIFACTS / "post_submit.png"))

    marker = require(SELECTORS.confirmation_marker, "confirmation_marker")
    if await page.locator(marker).count() == 0:
        log.warning(
            "confirmation marker %r not visible, but URL did change — treating "
            "as success. Inspect post_submit.png to confirm.", marker,
        )
    log.info("booking confirmed for %s %s-%s", target_date, start, end)
