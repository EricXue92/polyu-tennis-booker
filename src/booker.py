"""Main booking orchestration."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, datetime, timedelta

from playwright.async_api import (
    Page,
)

from src.config import (
    LOGIN_URL,
    SELECTORS,
    TRIGGER_TIME_HKT,
    require,
    slot_priority_for,
)
from src.dates import compute_target_date, seconds_until_hkt_time
from src.log import build_logger


DEFAULT_TIMEOUT_MS = 20_000
# How early (before TRIGGER_TIME_HKT) to start the login+prep work, so we can
# sit on a fully-loaded search form and only fire Search at 08:30:00.000 sharp.
# Empirically login + dropdown + date-set takes ~18 seconds; 60s gives slack.
PRELOGIN_LEAD_SECONDS = 60
# How early to send a GET against make_book.do to keep the TLS connection hot.
# Without warmup, the first POST at 08:30:00 pays a full TCP+TLS handshake
# (5.5s observed on 2026-06-05). 2s gives the warmup GET time to complete
# (~400-800ms on a cold connection) with margin before 08:30:00.000.
WARMUP_LEAD_SECONDS = 2


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


async def bootstrap_http_client(page, *, log: logging.Logger):
    """Extract session state from a post-login make_book.do page into a PolyUHttpClient.

    Caller is responsible for calling `client.aclose()`. Raises HtmlParseError
    if the page is not the expected post-login HTML (e.g. password-expired
    redirect, unexpected error page).

    Path-scoped cookies are preserved: PolyU runs two J2EE apps on the same
    host (`/possns` login + `/starspossfbns` booking), each with its own
    path-scoped JSESSIONID. Collapsing them by name would silently send the
    wrong one and produce a 403 with a fresh anonymous session.
    """
    import httpx

    from src.http_client import PolyUHttpClient, parse_csrf_token, parse_fb_user_id

    html = await page.content()
    csrf_token = parse_csrf_token(html)
    fb_user_id = parse_fb_user_id(html)
    raw_cookies = await page.context.cookies()
    polyu_cookies = [
        c for c in raw_cookies if "polyu.edu.hk" in c.get("domain", "")
    ]
    cookies = httpx.Cookies()
    for c in polyu_cookies:
        cookies.set(
            c["name"],
            c["value"],
            domain=c.get("domain", ""),
            path=c.get("path", "/"),
        )
    # Diagnostic: log unique cookie names AND any name that appears with
    # multiple paths (the original collision symptom).
    by_name: dict[str, list[str]] = {}
    for c in polyu_cookies:
        by_name.setdefault(c["name"], []).append(c.get("path", "/"))
    multipath = {n: paths for n, paths in by_name.items() if len(paths) > 1}
    log.info(
        "bootstrap_http_client: %d cookies (%d unique names), fbUserId=%s, csrf=%s...",
        len(polyu_cookies), len(by_name), fb_user_id, csrf_token[:8],
    )
    if multipath:
        # repr() the dict so Python logging doesn't treat the single dict
        # argument as a %(name)s mapping and crash with TypeError on %s.
        log.info("path-scoped duplicates: %s", repr(multipath))
    return PolyUHttpClient(
        cookies=cookies,
        csrf_token=csrf_token,
        fb_user_id=fb_user_id,
    )



async def run(*, dry_run: bool = False, skip_sleep: bool = False) -> int:
    """Returns 0 on successful booking, 1 on no-slot-available or any failure."""
    from playwright.async_api import async_playwright

    from src.config import MAKE_BOOK_URL
    from src.http_booker import book_via_http

    username = os.environ["POLYU_USERNAME"]
    password = os.environ["POLYU_PASSWORD"]
    log = build_logger("booker", secret=password)

    target_date = compute_target_date()
    log.info("target booking date: %s", target_date)

    slots = list(slot_priority_for(target_date))
    if not slots:
        # Rest weekday (e.g. Tuesday): nothing to book. Exit 0 so the
        # watchdog treats the day as accounted for and doesn't open an issue.
        log.info("no slots configured for %s (rest day); skipping run", target_date)
        return 0

    prelogin_target = (
        datetime.combine(date.today(), TRIGGER_TIME_HKT)
        - timedelta(seconds=PRELOGIN_LEAD_SECONDS)
    ).time()

    if not skip_sleep:
        delay = seconds_until_hkt_time(prelogin_target)
        log.info("sleeping %.1fs until HKT %s (pre-login)", delay, prelogin_target)
        await asyncio.sleep(delay)
        log.info("woke up for pre-login phase")

    client = None
    try:
        # Phase 1: Playwright login → extract session state → close browser.
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                page.set_default_timeout(20_000)
                await login(page, username, password, log)
                # Defensive: make sure we're on make_book.do (login normally
                # redirects there but PolyU could theoretically land us on a
                # password-expired page or a different post-login screen).
                if "make_book.do" not in page.url:
                    log.info("post-login url=%s; navigating to make_book.do", page.url)
                    await page.goto(MAKE_BOOK_URL, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
                client = await bootstrap_http_client(page, log=log)
            finally:
                await browser.close()

        # Phase 2: warm up the HTTP connection ~2s before trigger, then sleep
        # the last sliver and fire the booking flow.
        if not skip_sleep:
            warmup_target = (
                datetime.combine(date.today(), TRIGGER_TIME_HKT)
                - timedelta(seconds=WARMUP_LEAD_SECONDS)
            ).time()
            delay = seconds_until_hkt_time(warmup_target)
            log.info("sleeping %.3fs until HKT %s (pre-warmup)", delay, warmup_target)
            await asyncio.sleep(delay)
            from src.config import TENNIS_FACILITIES
            n_candidates = len(slots) * len(TENNIS_FACILITIES)
            log.info("warming up %d HTTP connections", n_candidates)
            statuses = await client.warmup(n=n_candidates)
            log.info("warmup complete (statuses=%s)", statuses)

            delay = seconds_until_hkt_time(TRIGGER_TIME_HKT)
            log.info("sleeping %.3fs until HKT %s (trigger)", delay, TRIGGER_TIME_HKT)
            await asyncio.sleep(delay)
            log.info("woke up at trigger time, firing predictive booking")
        return await book_via_http(client, target_date, slots, dry_run, log=log)
    finally:
        if client is not None:
            await client.aclose()


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
