# HTTP integration — Phase 2b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the Phase-2a `PolyUHttpClient` into the production booker. After this branch ships, the booker's hot path uses raw httpx instead of Playwright clicks. `src/parallel_runner.py` and `pick_slot` are deleted as dead code.

**Architecture:** `src/booker.py:run` keeps a single Playwright session for login only, extracts cookies + CSRFToken + fbUserId, closes the browser, sleeps to 08:30 HKT, then drives a new `src/http_booker.py:book_via_http` orchestrator that iterates priority slots and calls `client.try_book()` serially (first SUCCESS wins, OCCUPIED advances to next rank, ERROR aborts).

**Tech Stack:** Same as Phase 2a — Python 3.12+, httpx, Playwright (login only).

**Spec:** `docs/superpowers/specs/2026-06-03-http-replay-booking-design.md`
**Phase 2a plan (prerequisite, merged):** `docs/superpowers/plans/2026-06-03-http-client-phase-2a.md`

**User-confirmed design decisions:**
- **Pre-login stays at 08:29** (same `PRELOGIN_LEAD_SECONDS=60`). HTTP login is faster than Playwright, but we keep the buffer — session-staleness over a 60-min idle window is untested and the cost of one extra minute is zero.
- **`pick_slot` and `tests/test_slot_finder.py` are deleted** as part of this branch. `pick_slot` was already documented as a "test-only utility"; `search()` returns slots grouped by time-of-day, so the orchestrator iterates priority directly without it.

---

## File structure

- Modify: `src/booker.py` — rewrite `run()`, add `bootstrap_http_client()`, delete `prepare_search` / `submit_search` / `slot_has_availability` / `click_through` / `submit_and_resolve` / `pick_slot` (all become dead code)
- Create: `src/http_booker.py` — `book_via_http()` orchestrator
- Create: `tests/test_http_booker.py` — offline tests of the orchestrator with a fake client
- Create: `tests/test_bootstrap.py` — offline tests of the bootstrap helper (uses captured HTML fixture)
- Create: `tests/fixtures/make_book_post_login.html` — minimal real-shape HTML for bootstrap testing
- Delete: `src/parallel_runner.py`
- Delete: `tests/test_slot_finder.py`
- Modify: `CLAUDE.md` — rewrite the parallel-sessions and two-phase-sleep paragraphs; update timing numbers

---

## Task B1: Session bootstrap helper

After Playwright login completes (we're on `make_book.do`), extract everything `PolyUHttpClient` needs: cookies, CSRFToken (from inline JS), fbUserId (from hidden input).

**Files:**
- Create: `tests/fixtures/make_book_post_login.html` — captured HTML excerpt
- Create: `tests/test_bootstrap.py`
- Modify: `src/booker.py` — add `bootstrap_http_client` async function (don't touch existing functions yet)

- [ ] **Step 1: Create the HTML fixture**

`tests/fixtures/make_book_post_login.html`:

```html
<!DOCTYPE html>
<html>
<head><title>Make Booking</title></head>
<body>
<script>
    $.ajax({
        type: "POST",
        dataType: "json",
        url: "/starspossfbstud/secure/menu_click_fctn.json?CSRFToken=0cd6a396-5498-4d05-a3f8-a6fefaa2f9ea",
        data: {fctnCode: $(ptr).data('fctncode')}
    });
</script>
<form>
    <input type="hidden" id="fbUserId" name="fbUserId" value="432567"/>
    <input type="hidden" id="bookType" name="bookType" value="INDV"/>
</form>
</body>
</html>
```

- [ ] **Step 2: Failing tests in `tests/test_bootstrap.py`**

```python
"""Offline tests for bootstrap_http_client.

Uses a fake Playwright page (a small async stub) — no real browser launch.
The fixture HTML is a minimal real-shape excerpt of make_book.do.
"""
from pathlib import Path

import pytest

_FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "make_book_post_login.html").read_text()


class _FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return self._cookies


class _FakePage:
    def __init__(self, html, cookies):
        self._html = html
        self.context = _FakeContext(cookies)

    async def content(self):
        return self._html


@pytest.mark.asyncio
async def test_bootstrap_extracts_session_state_from_post_login_page():
    from src.booker import bootstrap_http_client

    page = _FakePage(
        html=_FIXTURE_HTML,
        cookies=[
            {"name": "JSESSIONID", "value": "abc123", "domain": "www40.polyu.edu.hk"},
            {"name": "AWSALB", "value": "lb-token", "domain": "www40.polyu.edu.hk"},
            # A cookie from an unrelated domain — must be filtered out.
            {"name": "ga", "value": "tracking", "domain": "google-analytics.com"},
        ],
    )
    import logging
    log = logging.getLogger("test")
    client = await bootstrap_http_client(page, log=log)
    try:
        assert client.csrf_token == "0cd6a396-5498-4d05-a3f8-a6fefaa2f9ea"
        assert client.fb_user_id == "432567"
        # Cookies from polyu.edu.hk only; the google-analytics one is filtered.
        assert client._http.cookies.get("JSESSIONID") == "abc123"
        assert client._http.cookies.get("AWSALB") == "lb-token"
        assert client._http.cookies.get("ga") is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_bootstrap_raises_when_html_is_unexpected():
    # If PolyU redirects us somewhere that's not make_book.do (e.g. a
    # password-expired prompt), the parsers raise HtmlParseError — surface
    # it so the watchdog email subject is meaningful.
    from src.booker import bootstrap_http_client
    from src.http_client import HtmlParseError

    page = _FakePage(
        html="<html><body>Your password has expired.</body></html>",
        cookies=[{"name": "JSESSIONID", "value": "x", "domain": "www40.polyu.edu.hk"}],
    )
    import logging
    log = logging.getLogger("test")
    with pytest.raises(HtmlParseError):
        await bootstrap_http_client(page, log=log)
```

- [ ] **Step 3: Run tests — verify failure**

`cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_bootstrap.py -v`
Expected: ImportError on `bootstrap_http_client`.

- [ ] **Step 4: Commit failing tests**

```bash
git add tests/test_bootstrap.py tests/fixtures/make_book_post_login.html
git commit -m "test(booker): failing tests for session bootstrap helper"
```

- [ ] **Step 5: Implement `bootstrap_http_client` in `src/booker.py`**

Append the following function to `src/booker.py` (after the existing `login` function, before `prepare_search`):

```python
async def bootstrap_http_client(page, *, log: logging.Logger):
    """Extract session state from a post-login make_book.do page into a PolyUHttpClient.

    Caller is responsible for calling `client.aclose()`. Raises HtmlParseError
    if the page is not the expected post-login HTML (e.g. password-expired
    redirect, unexpected error page).
    """
    from src.http_client import PolyUHttpClient, parse_csrf_token, parse_fb_user_id

    html = await page.content()
    csrf_token = parse_csrf_token(html)
    fb_user_id = parse_fb_user_id(html)
    raw_cookies = await page.context.cookies()
    cookies = {
        c["name"]: c["value"]
        for c in raw_cookies
        if "polyu.edu.hk" in c.get("domain", "")
    }
    log.info(
        "bootstrap_http_client: %d cookies, fbUserId=%s, csrf=%s...",
        len(cookies), fb_user_id, csrf_token[:8],
    )
    return PolyUHttpClient(
        cookies=cookies,
        csrf_token=csrf_token,
        fb_user_id=fb_user_id,
    )
```

- [ ] **Step 6: Run tests — verify pass**

`cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_bootstrap.py -v`
Expected: 2 tests pass.

- [ ] **Step 7: Run full suite**

`cd /Users/xue/polyu-tennis-booker && uv run pytest`
Expected: 65 passed (63 + 2 new).

- [ ] **Step 8: Commit implementation**

```bash
git add src/booker.py
git commit -m "feat(booker): bootstrap_http_client extracts session state from Page"
```

---

## Task B2: http_booker orchestrator

`book_via_http(client, target_date, slots, dry_run, *, log) -> int`. Calls `client.search()`, iterates priority slots, calls `client.try_book()` in rank order. Returns 0 on first SUCCESS, 1 if all slots exhausted, 1 on ERROR.

**Files:**
- Create: `src/http_booker.py`
- Create: `tests/test_http_booker.py`

- [ ] **Step 1: Failing tests in `tests/test_http_booker.py`**

```python
"""Offline tests for book_via_http orchestrator using a fake PolyUHttpClient."""
from datetime import date, datetime, time
import logging

import pytest

from src.http_client import AvailableSlot, BookingResult


class _FakeClient:
    """Minimal duck-typed stand-in for PolyUHttpClient.

    Records every try_book call, returns canned results per call.
    """

    def __init__(self, availability, try_book_results):
        self._availability = availability
        self._try_book_results = list(try_book_results)
        self.search_calls = 0
        self.try_book_calls = []

    async def search(self, target_date):
        self.search_calls += 1
        return self._availability

    async def try_book(self, slot):
        self.try_book_calls.append(slot)
        return self._try_book_results.pop(0)


def _slot(facility_id, hour):
    return AvailableSlot(
        facility_id=facility_id,
        facility_name=f"Tennis Court No. {facility_id - 9}",
        center_id=1,
        center_name="Shaw Sports Complex",
        start_dt=datetime(2026, 6, 10, hour, 30),
        end_dt=datetime(2026, 6, 10, hour + 1, 30),
    )


_LOG = logging.getLogger("test")


@pytest.mark.asyncio
async def test_book_via_http_returns_0_on_first_success():
    from src.http_booker import book_via_http

    client = _FakeClient(
        availability={
            (time(17, 30), time(18, 30)): [_slot(11, 17)],
            (time(18, 30), time(19, 30)): [_slot(10, 18), _slot(11, 18)],
            (time(19, 30), time(20, 30)): [_slot(11, 19)],
        },
        try_book_results=[BookingResult.SUCCESS],  # first attempt wins
    )
    priority = [(time(18, 30), time(19, 30)), (time(19, 30), time(20, 30)), (time(17, 30), time(18, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 0
    assert client.search_calls == 1
    # Rank 0 (18:30-19:30) tried first; OCCUPIED would advance — but SUCCESS short-circuits.
    assert len(client.try_book_calls) == 1
    assert client.try_book_calls[0].start_dt == datetime(2026, 6, 10, 18, 30)


@pytest.mark.asyncio
async def test_book_via_http_advances_through_occupied():
    from src.http_booker import book_via_http

    client = _FakeClient(
        availability={
            (time(17, 30), time(18, 30)): [_slot(11, 17)],
            (time(18, 30), time(19, 30)): [_slot(11, 18)],
            (time(19, 30), time(20, 30)): [_slot(11, 19)],
        },
        try_book_results=[BookingResult.OCCUPIED, BookingResult.OCCUPIED, BookingResult.SUCCESS],
    )
    priority = [(time(18, 30), time(19, 30)), (time(19, 30), time(20, 30)), (time(17, 30), time(18, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 0
    # All three ranks attempted in priority order.
    assert [s.start_dt.hour for s in client.try_book_calls] == [18, 19, 17]


@pytest.mark.asyncio
async def test_book_via_http_returns_1_when_all_occupied():
    from src.http_booker import book_via_http

    client = _FakeClient(
        availability={
            (time(17, 30), time(18, 30)): [_slot(11, 17)],
            (time(18, 30), time(19, 30)): [_slot(11, 18)],
        },
        try_book_results=[BookingResult.OCCUPIED, BookingResult.OCCUPIED],
    )
    priority = [(time(18, 30), time(19, 30)), (time(17, 30), time(18, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 1


@pytest.mark.asyncio
async def test_book_via_http_skips_priority_with_no_free_facility():
    # If rank 0 has no free facility, don't try_book — advance to rank 1.
    from src.http_booker import book_via_http

    client = _FakeClient(
        availability={
            # rank 0 missing from availability
            (time(19, 30), time(20, 30)): [_slot(11, 19)],
        },
        try_book_results=[BookingResult.SUCCESS],
    )
    priority = [(time(18, 30), time(19, 30)), (time(19, 30), time(20, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.try_book_calls) == 1
    assert client.try_book_calls[0].start_dt.hour == 19  # rank 1, not rank 0


@pytest.mark.asyncio
async def test_book_via_http_aborts_on_error():
    # ERROR from try_book means session is broken (auth lost, 500). Don't
    # burn through remaining priorities — return 1 so the watchdog opens an
    # issue and we can investigate.
    from src.http_booker import book_via_http

    client = _FakeClient(
        availability={
            (time(17, 30), time(18, 30)): [_slot(11, 17)],
            (time(18, 30), time(19, 30)): [_slot(11, 18)],
        },
        try_book_results=[BookingResult.ERROR],
    )
    priority = [(time(18, 30), time(19, 30)), (time(17, 30), time(18, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 1
    # Only rank 0 attempted — ERROR aborts before rank 1.
    assert len(client.try_book_calls) == 1


@pytest.mark.asyncio
async def test_book_via_http_dry_run_does_not_call_try_book():
    from src.http_booker import book_via_http

    client = _FakeClient(
        availability={(time(18, 30), time(19, 30)): [_slot(11, 18)]},
        try_book_results=[],  # would explode if try_book is called
    )
    priority = [(time(18, 30), time(19, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=True, log=_LOG)
    assert rc == 0
    assert client.try_book_calls == []
```

- [ ] **Step 2: Run tests — verify failure**

`cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_http_booker.py -v`
Expected: ModuleNotFoundError on `src.http_booker`.

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_http_booker.py
git commit -m "test(http_booker): failing tests for book_via_http orchestrator"
```

- [ ] **Step 4: Implement `src/http_booker.py`**

```python
"""HTTP-based booking orchestrator.

Drives a PolyUHttpClient through the 08:30 HKT booking flow:
 1. Search the target date.
 2. For each priority (start, end) slot, in rank order:
    a. Pick a free facility (any) from the search results.
    b. Call client.try_book(slot).
    c. SUCCESS → return 0. OCCUPIED → advance to next rank. ERROR → abort.
 3. If no rank produces SUCCESS, return 1.

Replaces the old parallel_runner.py + single-dequeuer coordinator design.
With HTTP, attempts are cheap and serial — no need for N concurrent
sessions racing for the same date.
"""
from __future__ import annotations

import logging
from datetime import date, time
from typing import Protocol

from src.http_client import AvailableSlot, BookingResult


class _ClientLike(Protocol):
    """Minimal subset of PolyUHttpClient used by the orchestrator.

    Defined as a Protocol so tests can pass a fake client without
    inheriting from PolyUHttpClient.
    """
    async def search(
        self,
        target_date: date,
    ) -> dict[tuple[time, time], list[AvailableSlot]]: ...

    async def try_book(self, slot: AvailableSlot) -> BookingResult: ...


async def book_via_http(
    client: _ClientLike,
    target_date: date,
    slots: list[tuple[time, time]],
    dry_run: bool,
    *,
    log: logging.Logger,
) -> int:
    """Search, then attempt priority slots serially. Returns 0 on SUCCESS, 1 otherwise."""
    log.info("calling search for %s", target_date)
    availability = await client.search(target_date)
    log.info("search returned %d free time-slots", len(availability))

    for rank, (start, end) in enumerate(slots):
        free = availability.get((start, end), [])
        if not free:
            log.info("rank=%d %s-%s: no free facility, skipping", rank, start, end)
            continue
        slot = free[0]  # any free facility for this time will do
        log.info(
            "rank=%d trying %s on %s (facility=%d)",
            rank, slot.facility_name, slot.start_dt, slot.facility_id,
        )
        if dry_run:
            log.info("DRY RUN: stopping before try_book")
            return 0
        result = await client.try_book(slot)
        log.info("rank=%d %s: result=%s", rank, slot.facility_name, result.name)
        if result is BookingResult.SUCCESS:
            return 0
        if result is BookingResult.ERROR:
            log.error("try_book returned ERROR; aborting to avoid burning priorities on a broken session")
            return 1
        # OCCUPIED → fall through to the next rank.

    log.warning("no priority slot succeeded; exiting with 1")
    return 1
```

- [ ] **Step 5: Run tests — verify they pass**

`cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_http_booker.py -v`
Expected: 6 tests pass.

- [ ] **Step 6: Run full suite**

`cd /Users/xue/polyu-tennis-booker && uv run pytest`
Expected: 71 passed (65 + 6 new).

- [ ] **Step 7: Commit**

```bash
git add src/http_booker.py
git commit -m "feat(http_booker): book_via_http orchestrator (search + serial try_book)"
```

---

## Task B3: Rewrite `src/booker.py:run` to use the HTTP path

Replace the parallel_runner call with the new flow: login via Playwright → bootstrap → close browser → sleep to 08:30 → call book_via_http.

**Files:**
- Modify: `src/booker.py` — rewrite `run()`, leave `login`, `LoginFailed`, `BookingFailed`, `main`. (Other functions become dead code; deleted in B4.)

- [ ] **Step 1: Replace the body of `run()` in `src/booker.py`**

Find the existing `async def run(...)`. Replace its entire body (preserving the signature and docstring) with:

```python
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

    # Phase 2: sleep until 08:30:00.000, then fire the HTTP booking flow.
    try:
        if not skip_sleep:
            delay = seconds_until_hkt_time(TRIGGER_TIME_HKT)
            log.info("sleeping %.3fs until HKT %s (trigger)", delay, TRIGGER_TIME_HKT)
            await asyncio.sleep(delay)
            log.info("woke up at trigger time, calling search")
        return await book_via_http(client, target_date, slots, dry_run, log=log)
    finally:
        await client.aclose()
```

Notes on what changes:
- `from src.parallel_runner import book_parallel` import is removed.
- The call `await book_parallel(...)` is replaced by the new flow.
- A single Playwright session does login only (no more N parallel sessions).
- A single sleep at 08:30 instead of the per-session sleep inside each PolyUSession.
- The browser is closed before the second sleep — saves memory on the CI runner during the idle window.

- [ ] **Step 2: Verify the module imports without launching Playwright**

`cd /Users/xue/polyu-tennis-booker && uv run python -c "from src.booker import run, main, bootstrap_http_client, login; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Run full pytest**

`cd /Users/xue/polyu-tennis-booker && uv run pytest`
Expected: 71 passed (no regressions; existing booker tests use the legacy structure but don't import `run()`'s internals).

If `tests/test_slot_finder.py` fails because `pick_slot` is gone — that's fine, we delete it in B4. For now, leave it and just don't break collection. Actually wait: `pick_slot` is still defined in `src/booker.py` after this task — we don't delete it until B4. So tests should still pass.

- [ ] **Step 4: Commit**

```bash
git add src/booker.py
git commit -m "feat(booker): switch run() to HTTP flow via PolyUHttpClient"
```

---

## Task B4: Delete dead code

Remove `src/parallel_runner.py`, the now-unused functions in `src/booker.py`, and `tests/test_slot_finder.py`.

**Files:**
- Delete: `src/parallel_runner.py`
- Delete: `tests/test_slot_finder.py`
- Modify: `src/booker.py` — remove dead functions

- [ ] **Step 1: Confirm nothing imports the symbols we're about to delete**

Run:
```
cd /Users/xue/polyu-tennis-booker && grep -rn "from src.parallel_runner\|from src.booker import" src/ tests/ scripts/
```

Expected: no result references `parallel_runner`, `prepare_search`, `submit_search`, `slot_has_availability`, `click_through`, `submit_and_resolve`, `pick_slot`, or `BookingResult` (the old one — booker.py imported it from parallel_runner; we use http_client's BookingResult instead).

The only `from src.booker import` lines should be (a) tests importing `compute_target_date` etc., (b) scripts/capture_http.py importing the booker primitives — **this is the one tricky case** (see Step 4 below).

- [ ] **Step 2: Delete `src/parallel_runner.py`**

```
cd /Users/xue/polyu-tennis-booker && rm src/parallel_runner.py
```

- [ ] **Step 3: Delete `tests/test_slot_finder.py`**

```
cd /Users/xue/polyu-tennis-booker && rm tests/test_slot_finder.py
```

- [ ] **Step 4: Update `scripts/capture_http.py`**

`scripts/capture_http.py:main_async` imports several booker primitives:

```python
from src.booker import (
    click_through,
    login,
    prepare_search,
    slot_has_availability,
    submit_and_resolve,
    submit_search,
)
```

We're about to delete most of these from `src/booker.py`. The capture script is a one-shot debugging tool that may be re-run if PolyU's UI changes — but the way it uses these primitives is essentially obsolete now that we have the HTTP trace already.

Decision: **leave the script importing only `login`** (still exists), and have the script just login, navigate to make_book.do, then exit. The "click through the booking flow with hooks attached" mode is no longer needed because Phase 2a/2b already use the HTTP shape; if the shape changes in the future, the user can either (a) capture from a manual browser session via DevTools, or (b) we re-add a Playwright-driven capture mode in a future PR.

Concretely: remove the broken imports and rewrite the post-login section of `scripts/capture_http.py:main_async` to log in, optionally navigate to make_book.do, and exit. Keep all the request/response trace hook plumbing — it still captures login + the navigation. Remove the click_through / slot_has_availability / submit_and_resolve calls.

Edit `scripts/capture_http.py`. Find:

```python
        from src.booker import (
            click_through,
            login,
            prepare_search,
            slot_has_availability,
            submit_and_resolve,
            submit_search,
        )
```

Replace with:

```python
        from src.booker import login
        from src.config import MAKE_BOOK_URL
```

Find the `try:` block that does login → prepare_search → submit_search → slot_has_availability → click_through → submit_and_resolve. Replace the whole block (between `try:` and `finally: await browser.close()`) with:

```python
            await login(page, username, password, log)
            # Navigate to make_book.do so the trace captures its HTML (which
            # contains CSRFToken + fbUserId for any future HTTP re-discovery).
            log.info("navigating to make_book.do to capture its HTML")
            await page.goto(MAKE_BOOK_URL, wait_until="domcontentloaded", timeout=20_000)
            if args.no_submit:
                log.info("--no-submit set; nothing further to capture")
            else:
                log.warning(
                    "Phase 2b: capture_http no longer drives the full booking flow. "
                    "If you need a fresh booking trace (e.g. PolyU shape changed), capture "
                    "manually via Chrome DevTools → Network → Save All As HAR."
                )
```

The `--slot` flag is now unused, but keep it in argparse (it's documented in CLAUDE.md). Add a comment in the script's docstring noting this.

Actually simpler: keep argparse as-is but document the behavior change in a top-of-script note. Append to the docstring:

```
NOTE (Phase 2b): This script no longer drives the full booking flow.
After login + make_book.do navigation, it stops — the HTTP request shapes
are now baked into src/http_client.py. If PolyU changes those shapes,
re-capture via Chrome DevTools → Network → Save All As HAR rather than
extending this script. The --slot flag is retained for argparse backwards
compatibility but is unused.
```

- [ ] **Step 5: Remove dead functions from `src/booker.py`**

Open `src/booker.py`. Delete:
- `BookingFailed` class (no longer raised anywhere)
- `SlotProbe` type alias
- `prepare_search`
- `submit_search`
- `pick_slot`
- `slot_has_availability`
- `click_through`
- `submit_and_resolve`
- The `from playwright.async_api import Page` import line (login still uses Page, but if it's only used inside login's local scope, keep it — verify by reading).

Keep:
- `BookingFailed` — actually no, search through the codebase first. Run `grep -rn "BookingFailed" src/ tests/`. If nothing references it, delete. If anything does, keep.
- `LoginFailed` — `login()` raises it, keep.
- `ARTIFACTS`, `DEFAULT_TIMEOUT_MS`, `PRELOGIN_LEAD_SECONDS` — `run()` uses DEFAULT_TIMEOUT_MS and PRELOGIN_LEAD_SECONDS. ARTIFACTS may no longer be used since we removed screenshots — `grep` to check; delete if unused.
- `login()` — still used.
- `bootstrap_http_client()` — added in B1, still used.
- `run()` — rewritten in B3.
- `main()` — entrypoint.

Run after editing: `cd /Users/xue/polyu-tennis-booker && uv run python -c "import src.booker; print('ok')"` — must print `ok` with no NameError.

- [ ] **Step 6: Run full pytest**

`cd /Users/xue/polyu-tennis-booker && uv run pytest`
Expected: green. Test count will DROP because `tests/test_slot_finder.py` is gone (had ~6 tests of `pick_slot`). Net: ~65 passed (71 - 6 = 65). The exact count depends on how many `pick_slot` tests there were; the assertion is "all remaining tests green".

- [ ] **Step 7: Verify `book-tennis --help` still works**

`cd /Users/xue/polyu-tennis-booker && uv run book-tennis --help`
Expected: argparse help text, no traceback.

- [ ] **Step 8: Commit**

```bash
git add src/booker.py scripts/capture_http.py
git rm src/parallel_runner.py tests/test_slot_finder.py
git commit -m "refactor: delete parallel_runner.py and dead Playwright orchestration"
```

---

## Task B5: Update CLAUDE.md

The architecture section of CLAUDE.md is now substantially wrong. Rewrite the affected paragraphs.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Read the current architecture section**

Run `cd /Users/xue/polyu-tennis-booker && head -120 CLAUDE.md` and find the section that starts with "The booking flow spans two files." That whole architecture description is stale.

- [ ] **Step 2: Replace the architecture section**

Find the paragraph:

```
The booking flow spans two files. `src/booker.py` holds per-session
primitives (`login`, `prepare_search`, `submit_search`,
`slot_has_availability`, `click_through`, `submit_and_resolve`).
`src/parallel_runner.py` orchestrates N `PolyUSession` instances in
parallel (N = `len(slot_priority_for(target_date))`, one per priority
slot). Runtime sequence: all sessions do `login` + `prepare_search`
concurrently (before 08:30) → at 08:30:00.000 all fire `submit_search`
simultaneously → each probes its own assigned slot via
`slot_has_availability` → each calls `click_through` (cell-click + Next
+ agreement-tick) → `submit_and_resolve` is gated by a single-dequeuer
coordinator so Submits happen strictly in priority rank order. The first
SUCCESS sets a shared win event and the others exit cleanly.
```

Replace with:

```
The booking flow spans three files. `src/booker.py:run` does Playwright
login at 08:29, extracts session state (cookies + CSRFToken + fbUserId)
via `bootstrap_http_client`, closes the browser, sleeps to 08:30:00.000,
and hands off to `src/http_booker.py:book_via_http`. That orchestrator
calls `PolyUHttpClient.search()` (one HTTP POST), then iterates priority
slots in rank order calling `client.try_book()` serially: SUCCESS wins,
OCCUPIED advances to the next rank, ERROR aborts. The client
(`src/http_client.py`) issues all three booking POSTs (timetable.json,
make_book.do, make_book_submit.do) over raw httpx — no Playwright on
the hot path.
```

- [ ] **Step 3: Find and remove the stale "Parallel booking via single-dequeuer coordinator" bullet**

It starts with `- **Parallel booking via single-dequeuer coordinator.**` and runs ~10 lines. Delete the entire bullet (the parallel design no longer exists).

- [ ] **Step 4: Update the "Two-phase sleep" bullet**

Find:

```
- **Two-phase sleep — login is intentionally BEFORE 08:30.** `run()` sleeps
  twice: first to `TRIGGER_TIME_HKT - PRELOGIN_LEAD_SECONDS` (08:29:00),
  then runs `login` + `prepare_search` (Tennis dropdown + date), then
  sleeps again to 08:30:00.000, then fires `submit_search`. ...
```

Replace the body with:

```
- **Two-phase sleep — login is intentionally BEFORE 08:30.** `run()`
  sleeps twice: first to `TRIGGER_TIME_HKT - PRELOGIN_LEAD_SECONDS`
  (08:29:00), runs Playwright `login` (~2-3s) and `bootstrap_http_client`
  to extract cookies + CSRFToken + fbUserId, closes the browser, then
  sleeps again to 08:30:00.000 before calling `book_via_http`. HTTP
  login is much faster than the old Playwright login + dropdown + date
  flow, but we keep the 60s lead as a buffer — running closer than that
  risks landing the Search POST a few hundred ms after 08:30 if the CI
  runner is busy. Do not collapse the two sleeps into one — landing
  Search exactly at 08:30:00.000 is the entire point.
```

- [ ] **Step 5: Update the "Race window" bullet**

Find:

```
- **Race window is probe→Submit; failures advance to next rank.**
  PolyU only commits the slot on final Submit, so another user can grab
  it any time during probe → click → Next → checkbox → Submit (~3s
  window). ...
```

Replace with:

```
- **Race window is search→Submit; failures advance to next rank.**
  `book_via_http` POSTs the Search at 08:30:00, parses the JSON response
  to find which (facility, time) pairs are free, then POSTs cell-click +
  final Submit serially in priority rank order. Race window from Search
  fire to first Submit hitting PolyU: ~4.5-5.0s (dominated by PolyU's
  ~4s server-side Search latency, then 2 cheap httpx POSTs). If the
  first try_book returns OCCUPIED, the orchestrator advances to the
  next rank within ~400ms. ERROR (auth lost, 5xx, unexpected redirect)
  aborts the whole run to avoid burning through priorities on a broken
  session — the watchdog email then has a meaningful failure to surface.
```

- [ ] **Step 6: Update or remove the "Submit detects failure in ~1s" bullet**

This bullet talks about Playwright's URL-vs-banner race. With HTTP, the disambiguation is direct (302 Location header check). Replace with:

```
- **Submit success/failure detection is direct, no timeout.** With
  Playwright we raced `wait_for_url` against `wait_for_selector("Facility
  is occupied")` to avoid a 20s URL-waiter eating the rank-advance
  budget. With HTTP, `try_book` inspects the Submit response's Location
  header: `make_book_result.do` ⇒ SUCCESS, banner in body or
  `make_book_submit.do` ⇒ OCCUPIED, anything else ⇒ ERROR. Result is
  decided in one round-trip (~400ms) instead of a 20s waiter race.
```

- [ ] **Step 7: Update the "Artifacts" bullet**

The new flow doesn't produce screenshots. Replace with:

```
- **Artifacts.** The HTTP flow doesn't produce screenshots — the
  request/response shapes are baked into `src/http_client.py` and tested
  offline. For new live failures (e.g. PolyU UI changes), use
  `scripts/capture_http.py` to grab a fresh HTTP trace under Playwright
  (login + make_book.do navigation only, since the full booking flow no
  longer needs Playwright). CI uploads any `artifacts/` content if
  present, but the booker itself doesn't create files anymore.
```

- [ ] **Step 8: Verify the file still renders sensibly**

Run `cd /Users/xue/polyu-tennis-booker && head -160 CLAUDE.md` and skim. Check that paragraph order makes sense and no stale references to `parallel_runner`, `PolyUSession`, `prepare_search`, `click_through`, `submit_and_resolve`, `slot_has_availability`, or `pick_slot` remain (grep: `grep -n "parallel_runner\|PolyUSession\|prepare_search\|click_through\|submit_and_resolve\|slot_has_availability\|pick_slot" CLAUDE.md` — expected: no results).

- [ ] **Step 9: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): rewrite architecture for HTTP-based booking flow"
```

---

## Manual checkpoint: local dry-run

After B5 lands, the user must validate locally before merging. **The CI workflow runs `dry_run=false` daily** — if anything is broken, real bookings will fail. So do this before merging to main:

```bash
# Before 08:30 HKT (so a popular slot is still free), or after 08:30
# with a known-off-peak slot. Pick a slot you actually want.
POLYU_USERNAME=... POLYU_PASSWORD=... \
  uv run book-tennis --dry-run --skip-sleep
```

Expected log shape:

```
target booking date: 2026-06-XX
sleeping 0.0s until HKT 08:29:00 (pre-login)  # or skipped if --skip-sleep
loading login page
submitting login
login complete (url=...)
bootstrap_http_client: N cookies, fbUserId=..., csrf=...
calling search for 2026-06-XX
search returned K free time-slots
rank=0 trying Tennis Court No. X on 2026-06-XX HH:30 (facility=Y)
DRY RUN: stopping before try_book
```

Final return should be 0. Confirm:
- No tracebacks
- bootstrap log line shows non-zero cookies and a non-empty fbUserId
- search returned at least 1 free time-slot
- DRY RUN line appears before try_book is called

If this passes, merge the PR. If not, paste the log to the chat for diagnosis.

---

## Self-review

**Spec coverage (against `2026-06-03-http-replay-booking-design.md`):**

| Spec item | Plan task |
|---|---|
| `src/http_booker.py` orchestrator | B2 |
| `bootstrap_http_client` extracts cookies + CSRF + fbUserId from Page | B1 |
| `src/booker.py:run` rewritten to use HTTP path | B3 |
| Delete `src/parallel_runner.py` | B4 |
| Login still uses Playwright (hybrid) | B3 (Playwright only for `login` + bootstrap) |
| Single login (no `login_lock`) | B3 (no parallel sessions) |
| Browser closed before 08:30 sleep | B3 |
| CLAUDE.md updates | B5 |
| `pick_slot` decision | B4 (deleted, per user decision) |

**Placeholder scan:** No "TODO" / "TBD" / "fill in" in any task. Every code block is complete.

**Type consistency:**
- `bootstrap_http_client(page, *, log) -> PolyUHttpClient` — used by B3.
- `book_via_http(client, target_date, slots, dry_run, *, log) -> int` — used by B3.
- `BookingResult.SUCCESS/OCCUPIED/ERROR` consistent between client and orchestrator.
- `_ClientLike` Protocol matches `PolyUHttpClient`'s method signatures exactly.

**Risk notes:**
- B3 is the highest-risk task because it rewrites production code. Mitigated by the manual dry-run before merge.
- B4's deletion of `BookingFailed` and other names is grep-gated — task explicitly says check before delete.
- B5's CLAUDE.md edits are mechanical; the verification step (grep for stale names) catches misses.
- The `scripts/capture_http.py` simplification in B4 is a soft choice; if a future PolyU UI change forces re-capture, we can re-add the Playwright-driven full-flow mode then.
