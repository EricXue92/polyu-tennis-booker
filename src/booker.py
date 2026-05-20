"""Main booking orchestration."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from src.config import (
    LOGIN_URL,
    SELECTORS,
    SUBMIT_URL,
    TRIGGER_TIME_HKT,
    require,
    slot_priority_for,
)
from src.dates import compute_target_date, sleep_until_hkt
from src.log import build_logger


class BookingFailed(RuntimeError):
    pass

ARTIFACTS = Path("artifacts")
DEFAULT_TIMEOUT_MS = 20_000
# How early (before TRIGGER_TIME_HKT) to start the login+prep work, so we can
# sit on a fully-loaded search form and only fire Search at 08:30:00.000 sharp.
# Empirically login + dropdown + date-set takes ~18 seconds; 60s gives slack.
PRELOGIN_LEAD_SECONDS = 60

SlotProbe = Callable[[date, time, time], Awaitable[bool]]


class LoginFailed(RuntimeError):
    pass


async def login(page: Page, username: str, password: str, log: logging.Logger) -> None:
    log.info("loading login page")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
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
    # Verify we're past the login form. If creds are wrong we stay on
    # loginhome.do with an error banner; surface that distinctly so the GH
    # email subject line is meaningful.
    if "loginhome" in page.url or await page.locator(
        require(SELECTORS.login_username, "login_username")
    ).count() > 0:
        raise LoginFailed(
            f"still on login page after submit (url={page.url!r}); "
            f"check POLYU_USERNAME / POLYU_PASSWORD secrets"
        )
    log.info("login complete (url=%s)", page.url)


async def prepare_search(
    page: Page,
    target_date: date,
    log: logging.Logger,
) -> None:
    """Open Sports Facility -> Tennis and set the date filter.

    Stops *before* clicking Search so the caller can do the final click at
    exactly 08:30:00.000 HKT, when PolyU releases the day+7 slots.
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


async def submit_search(page: Page, log: logging.Logger) -> None:
    """Click Search and wait for the AJAX timetable to render."""
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
) -> list[tuple[time, time]]:
    """Return available (start, end) slots in SLOT_PRIORITY order.

    All probes fire concurrently via asyncio.gather to minimize the wall-clock
    gap between Search results landing and the click on a free cell. The
    caller tries the first slot; if booking fails (e.g. another user committed
    in the click→Submit window) it can fall back to the next entry.

    The priority list is filtered per-weekday by `slot_priority_for` (e.g.
    Tuesday 18:30-20:30 is excluded because it's always staff-reserved).
    """
    priority = slot_priority_for(target_date)
    results = await asyncio.gather(
        *(is_available(target_date, start, end) for start, end in priority)
    )
    return [slot for slot, available in zip(priority, results) if available]


async def restart_to_results(
    page: Page,
    target_date: date,
    log: logging.Logger,
) -> None:
    """Re-arm the search results page after a failed booking attempt.

    On Submit-occupied or click-timeout failures we're left on the wrong page
    (make_book_submit.do with an error banner, or a half-loaded confirmation).
    Navigate back to make_book.do — LOGIN_URL points there and authenticated
    sessions skip the j_security_check — then re-run the Tennis + date prep
    and re-fire Search to repopulate the timetable.
    """
    log.info("resetting to search results for retry")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
    await prepare_search(page, target_date, log)
    await submit_search(page, log)


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
    )
    return await page.locator(selector).count() > 0


async def click_through(
    page: Page,
    target_date: date,
    start: time,
    end: time,
    *,
    session_id: str | None = None,
    log: logging.Logger,
) -> None:
    """Click an available cell, advance to confirmation, tick the agreement.

    Stops *before* clicking Submit so the caller can serialize that step.
    Saves pre_submit screenshot (namespaced per session if session_id given).
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
    suffix = f"_{session_id}" if session_id else ""
    await page.screenshot(path=str(ARTIFACTS / f"pre_submit{suffix}.png"))

    log.info("ticking agreement checkbox")
    await page.check(require(SELECTORS.agreement_checkbox, "agreement_checkbox"))


async def submit_and_resolve(
    page: Page,
    *,
    session_id: str | None = None,
    log: logging.Logger,
) -> "BookingResult":
    """Click Submit, race success-URL against the 'occupied' banner, return result.

    Returns parallel_runner.BookingResult. SUCCESS = navigated away from
    submit URL. OCCUPIED = banner shown. ERROR = unknown page state after
    the full DEFAULT_TIMEOUT_MS.
    """
    # Import locally to avoid a top-level cycle (parallel_runner imports booker).
    from src.parallel_runner import BookingResult

    log.info("clicking Submit")
    await page.locator(
        require(SELECTORS.submit_button, "submit_button")
    ).first.click()

    url_task = asyncio.create_task(
        page.wait_for_url(
            lambda url: SUBMIT_URL not in url, timeout=DEFAULT_TIMEOUT_MS
        )
    )
    err_task = asyncio.create_task(
        page.wait_for_selector(
            "text=/Facility is occupied/i",
            state="visible",
            timeout=DEFAULT_TIMEOUT_MS,
        )
    )
    done, pending = await asyncio.wait(
        {url_task, err_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
    for t in pending:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    suffix = f"_{session_id}" if session_id else ""
    if err_task in done and err_task.exception() is None:
        await page.screenshot(path=str(ARTIFACTS / f"post_submit{suffix}.png"))
        log.warning("Facility-occupied banner shown after Submit")
        return BookingResult.OCCUPIED
    if url_task not in done or url_task.exception() is not None:
        await page.screenshot(path=str(ARTIFACTS / f"post_submit{suffix}.png"))
        log.error("Submit produced unknown page state (neither nav nor banner)")
        return BookingResult.ERROR

    await page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)
    await page.screenshot(path=str(ARTIFACTS / f"post_submit{suffix}.png"))
    log.info("booking confirmed")
    return BookingResult.SUCCESS


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
    # Note: probe -> click is two operations with no DOM snapshot between.
    # If another user books in the gap, .click() raises a locator timeout
    # which the caller handles as a normal booking failure. By design — see
    # spec, "Race-condition optimization out of scope".
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
    # Race success (URL navigates away from make_book_submit.do) against the
    # known failure banner ("Facility is occupied ..." — appears within ~1s
    # when someone else committed the slot during our click→Submit window).
    # Without this race, wait_for_url eats the full 20s timeout before we
    # fall through to retry — by which time the next-priority slot is also
    # gone. Detecting the banner in ~1s gives the retry a real chance.
    url_task = asyncio.create_task(
        page.wait_for_url(
            lambda url: SUBMIT_URL not in url, timeout=DEFAULT_TIMEOUT_MS
        )
    )
    err_task = asyncio.create_task(
        page.wait_for_selector(
            "text=/Facility is occupied/i",
            state="visible",
            timeout=DEFAULT_TIMEOUT_MS,
        )
    )
    done, pending = await asyncio.wait(
        {url_task, err_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
    # Swallow CancelledError from the cancelled side.
    for t in pending:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    if err_task in done and err_task.exception() is None:
        await page.screenshot(path=str(ARTIFACTS / "post_submit.png"))
        raise BookingFailed(
            f"Facility-occupied banner shown after Submit at {SUBMIT_URL}"
        )
    if url_task not in done or url_task.exception() is not None:
        # Neither nav nor known banner — capture page state and fail.
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


async def run(*, dry_run: bool = False, skip_sleep: bool = False) -> int:
    """Returns 0 on successful booking, 1 on no-slot-available or any failure."""
    username = os.environ["POLYU_USERNAME"]
    password = os.environ["POLYU_PASSWORD"]
    log = build_logger("booker", secret=password)

    target_date = compute_target_date()
    log.info("target booking date: %s", target_date)

    # Two-phase sleep: wake early to log in and prep the search form, then
    # sleep the remainder so we only click Search at 08:30:00.000 sharp.
    # PolyU releases day+7 slots at exactly 08:30 HKT and popular evening
    # slots are gone within seconds — being pre-logged-in saves ~18s vs
    # starting login at the trigger time. See CLAUDE.md.
    prelogin_target = (
        datetime.combine(date.today(), TRIGGER_TIME_HKT)
        - timedelta(seconds=PRELOGIN_LEAD_SECONDS)
    ).time()

    if not skip_sleep:
        log.info("sleeping until HKT %s (pre-login)", prelogin_target)
        sleep_until_hkt(prelogin_target)
        log.info("woke up for pre-login phase")

    ARTIFACTS.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=True)
        context: BrowserContext = await browser.new_context()
        page: Page = await context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        try:
            await login(page, username, password, log)
            await prepare_search(page, target_date, log)

            if not skip_sleep:
                log.info("prep complete; sleeping until HKT %s", TRIGGER_TIME_HKT)
                sleep_until_hkt(TRIGGER_TIME_HKT)
                log.info("woke up at trigger time, firing Search")

            await submit_search(page, log)
            await page.screenshot(path=str(ARTIFACTS / "search_results.png"))

            async def probe(d: date, s: time, e: time) -> bool:
                return await slot_has_availability(page, d, s, e)

            candidates = await pick_slot(target_date, probe)
            if not candidates:
                log.error(
                    "no slot available for %s in any priority window", target_date
                )
                return 1

            # Walk priority order. Between probe (08:30:04ish) and Submit
            # (08:30:08ish) another user can commit the same slot, leaving us
            # stuck on make_book_submit.do with a "Facility is occupied"
            # banner. On any such failure, reset the page and try the next
            # candidate — re-probing first so we don't click a cell that just
            # got taken.
            for attempt, (start, end) in enumerate(candidates):
                if attempt > 0:
                    try:
                        await restart_to_results(page, target_date, log)
                    except Exception as e:
                        log.error("failed to reset page for retry: %s", e)
                        return 1
                    if not await slot_has_availability(page, target_date, start, end):
                        log.info(
                            "slot %s-%s no longer available, skipping", start, end
                        )
                        continue
                try:
                    await book_slot(
                        page, target_date, start, end, dry_run=dry_run, log=log
                    )
                    return 0
                except (BookingFailed, PlaywrightTimeoutError) as e:
                    log.warning(
                        "booking attempt failed for %s %s-%s: %s",
                        target_date, start, end, e,
                    )
            log.error(
                "all %d candidate slots failed for %s", len(candidates), target_date
            )
            return 1

        except LoginFailed as e:
            log.error("login failed: %s", e)
            try:
                await page.screenshot(path=str(ARTIFACTS / "failure.png"))
            except Exception:
                pass
            return 1
        except BookingFailed as e:
            log.error("booking failed: %s", e)
            await page.screenshot(path=str(ARTIFACTS / "failure.png"))
            return 1
        except Exception as e:
            log.exception("unexpected error: %s", e)
            try:
                await page.screenshot(path=str(ARTIFACTS / "failure.png"))
            except Exception:
                pass
            return 1
        finally:
            await context.close()
            await browser.close()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true",
        help="walk the flow but don't click final Submit",
    )
    parser.add_argument(
        "--skip-sleep", action="store_true",
        help="don't wait until HKT 08:30; run immediately",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(dry_run=args.dry_run, skip_sleep=args.skip_sleep)))


if __name__ == "__main__":
    main()
