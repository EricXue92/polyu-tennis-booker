"""Main booking orchestration."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Sequence

from playwright.async_api import (
    Page,
)

from src.config import (
    ACCOUNTS,
    STAFF_SITE,
    SELECTORS,
    TRIGGER_TIME_HKT,
    Account,
    Site,
    require,
)
from src.dates import compute_target_date, seconds_until_hkt_time
from src.log import build_logger

if TYPE_CHECKING:  # pragma: no cover
    from src.http_client import PolyUHttpClient


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


async def login(
    page: Page,
    username: str,
    password: str,
    log: logging.Logger,
    *,
    site: Site = STAFF_SITE,
) -> None:
    log.info("loading login page (%s)", site.base_path)
    await page.goto(site.login_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
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
            f"check the credentials for site {site.base_path!r}"
        )
    log.info("login complete (url=%s)", page.url)


async def bootstrap_http_client(page, *, log: logging.Logger, site: Site = STAFF_SITE):
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
        site=site,
    )


# --- Multi-account orchestration -------------------------------------------

SlotList = list[tuple[time, time]]


def active_jobs(
    target_date: date,
    accounts: Sequence[Account] = ACCOUNTS,
) -> list[tuple[Account, SlotList]]:
    """Return (account, slots) for every account that books on target_date.

    An account whose slot rule returns an empty tuple (rest day, or the
    student account on a weekday) is left out entirely.
    """
    jobs: list[tuple[Account, SlotList]] = []
    for account in accounts:
        slots = list(account.slot_priority(target_date))
        if slots:
            jobs.append((account, slots))
    return jobs


@dataclass
class AccountJob:
    """One account's fully-prepared booking run: a bootstrapped client plus
    the slots it should try, and a logger that redacts *its* password."""
    account: Account
    slots: SlotList
    client: Any  # PolyUHttpClient (or a test fake with cell_click/submit)
    log: logging.Logger


async def book_all(
    jobs: Sequence[AccountJob],
    target_date: date,
    dry_run: bool,
    *,
    log: logging.Logger,
) -> int:
    """Fire every account's book_via_http concurrently.

    Accounts are independent: one failing (or raising) never aborts the
    others. Returns 0 only if every account booked; 1 otherwise, so the
    owner is emailed whenever any expected court is missing.
    """
    from src.http_booker import book_via_http

    async def _one(job: AccountJob) -> int:
        try:
            return await book_via_http(
                job.client, target_date, job.slots, dry_run, log=job.log,
            )
        except Exception:  # noqa: BLE001 - isolate accounts from each other
            job.log.exception("account %s crashed", job.account.name)
            return 1

    results = await asyncio.gather(*(_one(j) for j in jobs))
    for job, rc in zip(jobs, results):
        log.info("account %s: %s", job.account.name, "BOOKED" if rc == 0 else "FAILED")
    return 0 if all(rc == 0 for rc in results) else 1



def resolve_target_date(override: str | None) -> date:
    """Target date: `override` (ISO YYYY-MM-DD) if given, else 7 days ahead.

    The override exists for manual/dry-run verification of date-specific
    rules (e.g. the student account's dates); the CF Worker never passes it.
    """
    if override:
        return date.fromisoformat(override)
    return compute_target_date()


async def run(
    *,
    dry_run: bool = False,
    skip_sleep: bool = False,
    target_date_override: str | None = None,
) -> int:
    """Returns 0 when every active account booked, 1 on no-slot or any failure."""
    from playwright.async_api import async_playwright

    from src.config import TENNIS_FACILITIES

    log = build_logger("booker", secret="")

    target_date = resolve_target_date(target_date_override)
    log.info("target booking date: %s", target_date)

    jobs = active_jobs(target_date)
    if not jobs:
        # Rest weekday (e.g. Tuesday): nothing to book. Exit 0 so the
        # watchdog treats the day as accounted for and doesn't open an issue.
        log.info("no slots configured for %s (rest day); skipping run", target_date)
        return 0

    # Resolve credentials up front so a missing secret fails now, not at 08:29.
    creds: list[tuple[Account, SlotList, str, str, logging.Logger]] = []
    for account, slots in jobs:
        username = os.environ[account.username_env]
        password = os.environ[account.password_env]
        acct_log = build_logger("booker", secret=password, session_id=account.name)
        creds.append((account, slots, username, password, acct_log))
        log.info("account %s (%s): %d slots", account.name, account.site.base_path, len(slots))

    prelogin_target = (
        datetime.combine(date.today(), TRIGGER_TIME_HKT)
        - timedelta(seconds=PRELOGIN_LEAD_SECONDS)
    ).time()

    if not skip_sleep:
        delay = seconds_until_hkt_time(prelogin_target)
        log.info("sleeping %.1fs until HKT %s (pre-login)", delay, prelogin_target)
        await asyncio.sleep(delay)
        log.info("woke up for pre-login phase")

    prepared: list[AccountJob] = []
    login_failed = False
    try:
        # Phase 1: Playwright login per account (fresh context each, so the
        # two sites' path-scoped cookies never mix) -> extract session state
        # -> close browser. Accounts log in concurrently: one login is ~10s
        # in CI, and serialising them would eat the 60s pre-login lead.
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                async def _prepare(account, slots, username, password, acct_log) -> AccountJob:
                    context = await browser.new_context()
                    try:
                        page = await context.new_page()
                        page.set_default_timeout(20_000)
                        await login(page, username, password, acct_log, site=account.site)
                        # Defensive: make sure we're on make_book.do (login
                        # normally redirects there but PolyU could land us on
                        # a password-expired page or another post-login screen).
                        if "make_book.do" not in page.url:
                            acct_log.info("post-login url=%s; navigating to make_book.do", page.url)
                            await page.goto(
                                account.site.make_book_url,
                                wait_until="domcontentloaded",
                                timeout=DEFAULT_TIMEOUT_MS,
                            )
                        client = await bootstrap_http_client(page, log=acct_log, site=account.site)
                    finally:
                        await context.close()
                    return AccountJob(account, slots, client, acct_log)

                outcomes = await asyncio.gather(
                    *(_prepare(*c) for c in creds), return_exceptions=True,
                )
            finally:
                await browser.close()

        # A failed login for one account must not cost the other its court:
        # log it, carry on with whoever got in, and still exit 1 at the end.
        for (account, _slots, _u, _p, acct_log), outcome in zip(creds, outcomes):
            if isinstance(outcome, BaseException):
                # Log via the account's own logger: Playwright errors can
                # quote field values, and only acct_log redacts this password.
                acct_log.error("login/bootstrap failed: %r", outcome)
                login_failed = True
            else:
                prepared.append(outcome)
        if not prepared:
            raise RuntimeError("every account failed to log in")

        # Phase 2: warm up every account's connection pool ~2s before trigger,
        # then sleep the last sliver and fire all accounts together.
        if not skip_sleep:
            warmup_target = (
                datetime.combine(date.today(), TRIGGER_TIME_HKT)
                - timedelta(seconds=WARMUP_LEAD_SECONDS)
            ).time()
            delay = seconds_until_hkt_time(warmup_target)
            log.info("sleeping %.3fs until HKT %s (pre-warmup)", delay, warmup_target)
            await asyncio.sleep(delay)

            async def _warm(job: AccountJob) -> None:
                n = len(job.slots) * len(TENNIS_FACILITIES)
                job.log.info("warming up %d HTTP connections", n)
                statuses = await job.client.warmup(n=n)
                job.log.info("warmup complete (statuses=%s)", statuses)

            await asyncio.gather(*(_warm(j) for j in prepared))

            delay = seconds_until_hkt_time(TRIGGER_TIME_HKT)
            log.info("sleeping %.3fs until HKT %s (trigger)", delay, TRIGGER_TIME_HKT)
            await asyncio.sleep(delay)
            log.info("woke up at trigger time, firing predictive booking for %d account(s)", len(prepared))
        rc = await book_all(prepared, target_date, dry_run, log=log)
        return 1 if login_failed else rc
    finally:
        for job in prepared:
            await job.client.aclose()


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
    parser.add_argument(
        "--target-date", default=None, metavar="YYYY-MM-DD",
        help="override the 7-days-ahead target date (manual verification only)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(
        dry_run=args.dry_run,
        skip_sleep=args.skip_sleep,
        target_date_override=args.target_date,
    )))


if __name__ == "__main__":
    main()
