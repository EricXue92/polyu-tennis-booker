# Tennis Booking Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python program that runs daily on GitHub Actions at 08:30 HKT, logs into the PolyU sports booking system, and books a tennis court 7 days ahead, preferring 19:30–20:30 with fallbacks to 18:30–19:30 then 20:30–21:30.

**Architecture:** Python 3.12 + Playwright (Chromium, headless) for browser automation. GitHub Actions cron fires at 08:20 HKT (10 min early to absorb GH cron drift); the script then sleeps in-process until exactly 08:30:00.000 HKT before issuing the first booking request. Single Python package under `src/`, unit-tested with pytest + freezegun + Playwright's `Page` mocked. Selectors for the live PolyU UI are captured via a one-time interactive discovery script (Task 6) and committed into `config.py`.

**Tech Stack:** Python 3.12, Playwright, pytest, freezegun, GitHub Actions, uv (project already initialised), Asia/Hong_Kong timezone via stdlib `zoneinfo`.

---

## File Structure

```
TennisBooking/
├── .github/workflows/book.yml      # GH Actions cron + Playwright runner
├── .gitignore
├── pyproject.toml                  # deps: playwright, pytest, freezegun
├── README.md                       # deployment + Secrets setup
├── src/
│   ├── __init__.py
│   ├── booker.py                   # async main flow + orchestration
│   ├── config.py                   # URLs, slot priority, selectors (frozen dataclass)
│   ├── dates.py                    # compute_target_date, sleep_until_hkt
│   └── log.py                      # logger with password redaction
├── scripts/
│   └── discover_selectors.py       # interactive: open visible browser, dump HTML
├── tests/
│   ├── __init__.py
│   ├── test_dates.py
│   ├── test_log.py
│   └── test_slot_finder.py
└── docs/superpowers/
    ├── specs/2026-05-09-tennis-booking-design.md
    └── plans/2026-05-09-tennis-booking.md   # (this file)
```

---

## Task 1: Project Skeleton & Dependencies

**Files:**
- Modify: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/__init__.py` (empty)
- Create: `tests/__init__.py` (empty)
- Delete: `main.py` (uv default scaffold; not needed)

- [ ] **Step 1: Update pyproject.toml**

Replace the entire file contents with:

```toml
[project]
name = "tennisbooking"
version = "0.1.0"
description = "Daily auto-booker for PolyU tennis courts"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "playwright>=1.48",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "freezegun>=1.5",
    "pytest-asyncio>=0.24",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src"]
```

- [ ] **Step 2: Create .gitignore**

```gitignore
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.python-version-local
*.egg-info/
dist/
build/
# Playwright artifacts
test-results/
playwright-report/
# Local-only screenshots from discovery
artifacts/
*.png
*.html
!docs/**/*.png
```

- [ ] **Step 3: Create empty package files**

```bash
touch src/__init__.py tests/__init__.py
```

- [ ] **Step 4: Remove the uv default scaffold**

```bash
rm main.py
```

- [ ] **Step 5: Install deps and Playwright Chromium**

```bash
uv sync
uv run playwright install chromium
```

Expected: dependencies install; Chromium browser downloads (~150 MB). This is a one-time local setup; CI installs separately via the `microsoft/playwright-python` image strategy in Task 11.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: project skeleton, deps, gitignore"
```

---

## Task 2: Logger with Password Redaction (TDD)

**Why first:** every later task uses logging; we want password-redaction guarantees baked in before any credentials touch a logger call.

**Files:**
- Create: `src/log.py`
- Create: `tests/test_log.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_log.py`:

```python
import logging

from src.log import build_logger, redact


def test_redact_replaces_password_substring():
    redacted = redact("login attempt user=alice pw=hunter2 done", "hunter2")
    assert "hunter2" not in redacted
    assert "***" in redacted


def test_redact_handles_empty_secret():
    assert redact("nothing to redact", "") == "nothing to redact"


def test_redact_replaces_all_occurrences():
    out = redact("hunter2 then hunter2 again", "hunter2")
    assert "hunter2" not in out
    assert out.count("***") == 2


def test_logger_redacts_password_in_messages(caplog):
    logger = build_logger("test", secret="hunter2")
    with caplog.at_level(logging.INFO, logger="test"):
        logger.info("submitting password=hunter2 to server")
    record = caplog.records[-1]
    assert "hunter2" not in record.getMessage()
    assert "***" in record.getMessage()


def test_logger_writes_to_stderr(capsys):
    logger = build_logger("test2", secret="x")
    logger.info("hello")
    captured = capsys.readouterr()
    assert "hello" in captured.err
```

- [ ] **Step 2: Run tests — expect failure**

Run: `uv run pytest tests/test_log.py -v`
Expected: ImportError / ModuleNotFoundError on `src.log`.

- [ ] **Step 3: Implement `src/log.py`**

```python
"""Logger with built-in secret redaction.

The booker passes the user's password into log messages indirectly (Playwright
errors can quote field values). We install a Filter that substitutes the
password with `***` before any handler sees the record.
"""
from __future__ import annotations

import logging
import sys


def redact(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "***")


class _RedactFilter(logging.Filter):
    def __init__(self, secret: str) -> None:
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secret:
            return True
        if isinstance(record.msg, str):
            record.msg = redact(record.msg, self._secret)
        if record.args:
            record.args = tuple(
                redact(a, self._secret) if isinstance(a, str) else a
                for a in record.args
            )
        return True


def build_logger(name: str, *, secret: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(_RedactFilter(secret))
    logger.addHandler(handler)
    logger.propagate = True  # let caplog capture in tests
    return logger
```

- [ ] **Step 4: Run tests — expect pass**

Run: `uv run pytest tests/test_log.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/log.py tests/test_log.py
git commit -m "feat: redacting logger"
```

---

## Task 3: Date Math — `compute_target_date` (TDD)

**Files:**
- Create: `src/dates.py`
- Create: `tests/test_dates.py`

- [ ] **Step 1: Write the failing test**

`tests/test_dates.py`:

```python
from datetime import date

from freezegun import freeze_time

from src.dates import compute_target_date


@freeze_time("2026-05-09 00:30:00", tz_offset=0)  # 08:30 HKT
def test_target_is_seven_days_after_today_hkt():
    assert compute_target_date() == date(2026, 5, 16)


@freeze_time("2026-05-09 15:59:00", tz_offset=0)  # 23:59 HKT same day
def test_target_uses_hkt_calendar_day_late_evening():
    assert compute_target_date() == date(2026, 5, 16)


@freeze_time("2026-05-09 16:00:00", tz_offset=0)  # 00:00 HKT next day
def test_target_rolls_over_at_hkt_midnight():
    assert compute_target_date() == date(2026, 5, 17)
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/test_dates.py::test_target_is_seven_days_after_today_hkt -v`
Expected: ImportError.

- [ ] **Step 3: Implement `src/dates.py` (partial — just date function)**

```python
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
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/test_dates.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dates.py tests/test_dates.py
git commit -m "feat: target date computation in HKT"
```

---

## Task 4: `sleep_until_hkt` (TDD)

**Files:**
- Modify: `src/dates.py`
- Modify: `tests/test_dates.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_dates.py`:

```python
from datetime import time
from unittest.mock import patch

import pytest

from src.dates import seconds_until_hkt_time, sleep_until_hkt


@freeze_time("2026-05-09 00:25:00", tz_offset=0)  # 08:25 HKT, target 08:30 HKT
def test_seconds_until_future_time_today():
    assert seconds_until_hkt_time(time(8, 30)) == pytest.approx(300, abs=1)


@freeze_time("2026-05-09 00:30:00", tz_offset=0)  # exactly 08:30 HKT
def test_seconds_until_past_time_returns_zero():
    # If target already passed today, return 0 (don't wait until tomorrow).
    assert seconds_until_hkt_time(time(8, 0)) == 0


@freeze_time("2026-05-09 00:25:00", tz_offset=0)
def test_sleep_until_calls_sleep_with_correct_duration():
    with patch("src.dates.time.sleep") as mock_sleep:
        sleep_until_hkt(time(8, 30))
    mock_sleep.assert_called_once()
    duration = mock_sleep.call_args[0][0]
    assert 299 <= duration <= 301


@freeze_time("2026-05-09 00:30:00", tz_offset=0)
def test_sleep_until_skips_when_already_past():
    with patch("src.dates.time.sleep") as mock_sleep:
        sleep_until_hkt(time(8, 0))
    mock_sleep.assert_not_called()
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/test_dates.py -v`
Expected: 4 new tests fail with ImportError on `seconds_until_hkt_time` / `sleep_until_hkt`.

- [ ] **Step 3: Extend `src/dates.py`**

Append to `src/dates.py`:

```python
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
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/test_dates.py -v`
Expected: 7 passed total.

- [ ] **Step 5: Commit**

```bash
git add src/dates.py tests/test_dates.py
git commit -m "feat: sleep_until_hkt for sub-second cron alignment"
```

---

## Task 5: Config Module with Placeholder Selectors

The selectors will be filled in by Task 6 (discovery). Until then, they're explicit `None` sentinels so an early run blows up loudly rather than silently passing the wrong CSS to Playwright.

**Files:**
- Create: `src/config.py`

- [ ] **Step 1: Write `src/config.py`**

```python
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
```

- [ ] **Step 2: Smoke-test it imports**

Run: `uv run python -c "from src.config import SLOT_PRIORITY, SELECTORS, require; print(SLOT_PRIORITY); print(SELECTORS)"`
Expected: prints the slot tuples and a `Selectors(login_username=PENDING_DISCOVERY, ...)` repr.

- [ ] **Step 3: Commit**

```bash
git add src/config.py
git commit -m "feat: config module with placeholder selectors"
```

---

## Task 6: Discovery Script — Capture Real Selectors

This is an **interactive, one-time, locally-run** task. It opens a visible Chromium, the user logs in (or we do it programmatically with credentials), navigates to the search results page, and dumps the HTML + screenshots so we can read the structure and fill `src/config.py`.

**Files:**
- Create: `scripts/discover_selectors.py`
- Modify: `src/config.py` (to fill in real selectors after running this)

- [ ] **Step 1: Write the discovery script**

`scripts/discover_selectors.py`:

```python
"""Interactive selector discovery.

Run locally (NOT in CI):
    POLYU_USERNAME=... POLYU_PASSWORD=... uv run python scripts/discover_selectors.py

Opens a visible Chromium, logs in, walks to the tennis search results page,
saves HTML + screenshots to artifacts/. Use the dumps to fill in
src/config.py:Selectors.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

from src.config import LOGIN_URL

ARTIFACTS = Path("artifacts")


async def dump(page, label: str) -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    html = await page.content()
    (ARTIFACTS / f"{label}.html").write_text(html, encoding="utf-8")
    await page.screenshot(path=str(ARTIFACTS / f"{label}.png"), full_page=True)
    print(f"  saved artifacts/{label}.html and .png")


async def main() -> None:
    user = os.environ["POLYU_USERNAME"]
    pw = os.environ["POLYU_PASSWORD"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=400)
        context = await browser.new_context()
        page = await context.new_page()

        print("Step 1: login page")
        await page.goto(LOGIN_URL)
        await dump(page, "01_login")

        print("Pause for manual inspection. Press Enter to attempt programmatic login,")
        print("or fill the form manually in the visible browser then press Enter.")
        input()

        # If the user filled it manually, this is a no-op-ish; if not, attempt
        # standard field names. Adjust here if it fails.
        try:
            await page.fill('input[name="username"]', user, timeout=2000)
            await page.fill('input[name="password"]', pw, timeout=2000)
            await page.click('input[type="submit"], button[type="submit"]')
        except Exception as e:
            print(f"  programmatic fill failed ({e}); assuming manual login")

        await page.wait_for_load_state("networkidle")
        await dump(page, "02_after_login")

        print("Step 2: navigate to Sports Facility -> Tennis -> Search.")
        print("Do this manually in the browser, then press Enter.")
        input()
        await dump(page, "03_search_results")

        print("Step 3: click an available 19:30 slot, then click Next.")
        print("(Pick any available slot for the discovery dump.)")
        print("Do it manually in the browser, then press Enter.")
        input()
        await dump(page, "04_pre_submit")

        print("DO NOT click Submit. Close the browser when done.")
        input("Press Enter to close.")
        await browser.close()

    print()
    print("Done. Open artifacts/*.html in a browser or editor.")
    print("Look for:")
    print("  - login form: input names for username/password, submit button selector")
    print("  - sports facility menu: link/button selector")
    print("  - activity dropdown: <select> name and the <option value> for Tennis")
    print("  - search button selector")
    print("  - results page: how cells are marked as available vs booked")
    print("    (class? data-attr? <a> vs <td>? what HTML wraps date/time?)")
    print("  - next button on results page")
    print("  - agreement checkbox name/id, submit button selector on make_book_submit.do")
    print()
    print("Fill these into src/config.py:Selectors and commit.")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run discovery (REQUIRES USER CREDENTIALS)**

Run locally:

```bash
POLYU_USERNAME='your-username' POLYU_PASSWORD='your-password' \
    uv run python scripts/discover_selectors.py
```

Follow on-screen prompts. The script writes `artifacts/01_login.html` … `04_pre_submit.html` plus screenshots.

- [ ] **Step 3: Read the dumps and fill `src/config.py`**

Open each HTML file. For each `PENDING_DISCOVERY` field in `Selectors`, find the matching DOM element and replace the default with the actual selector. Example (illustrative — your selectors may differ):

```python
@dataclass(frozen=True)
class Selectors:
    login_username: str | _Pending = 'input[name="username"]'
    login_password: str | _Pending = 'input[name="password"]'
    login_submit: str | _Pending = 'input[type="submit"][value*="Login"]'

    sports_facility_link: str | _Pending = 'a:has-text("Sports Facility")'
    activity_dropdown: str | _Pending = 'select[name="activity"]'
    activity_tennis_value: str | _Pending = 'TENNIS'  # the <option value>
    search_button: str | _Pending = 'input[type="submit"][value="Search"]'

    available_slot_cell: str | _Pending = (
        'tr[data-date="{date}"] td.available[data-start="{start}"]'
    )
    next_button: str | _Pending = 'input[value="Next"]'

    agreement_checkbox: str | _Pending = 'input[type="checkbox"][name="agree"]'
    submit_button: str | _Pending = 'input[type="submit"][value="Submit"]'
    confirmation_marker: str | _Pending = 'text=Booking Confirmed'
```

The `available_slot_cell` selector is a Python format string — Task 8's `find_available_slot` calls `.format(date=..., start=..., end=...)` on it. Make sure the placeholders you write match what the live HTML lets you target.

- [ ] **Step 4: Sanity-check by re-running discovery against the now-filled selectors (optional)**

Add a small extra block at the bottom of `discover_selectors.py` if you want to assert each filled selector resolves on the right page. Skip if confident.

- [ ] **Step 5: Commit**

```bash
git add scripts/discover_selectors.py src/config.py
git commit -m "feat: selector discovery script and filled live selectors"
```

**Note:** `artifacts/` is `.gitignore`d — do NOT commit the HTML/PNG dumps; they may contain your name, student ID, or session cookies.

---

## Task 7: Login Flow

**Files:**
- Create: `src/booker.py` (start of the file)

- [ ] **Step 1: Create `src/booker.py` with login**

```python
"""Main booking orchestration."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, time
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.config import LOGIN_URL, SELECTORS, SLOT_PRIORITY, TRIGGER_TIME_HKT, require
from src.dates import compute_target_date, sleep_until_hkt
from src.log import build_logger

ARTIFACTS = Path("artifacts")
SCREENSHOT_TIMEOUT_MS = 15_000


async def login(page: Page, username: str, password: str, log: logging.Logger) -> None:
    log.info("loading login page")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.fill(require(SELECTORS.login_username, "login_username"), username)
    await page.fill(require(SELECTORS.login_password, "login_password"), password)
    log.info("submitting login")
    await page.click(require(SELECTORS.login_submit, "login_submit"))
    await page.wait_for_load_state("networkidle")
    log.info("login complete")
```

- [ ] **Step 2: Smoke-test that import works**

Run: `uv run python -c "from src.booker import login; print(login)"`
Expected: prints `<function login at ...>`.

- [ ] **Step 3: Commit**

```bash
git add src/booker.py
git commit -m "feat: login flow"
```

---

## Task 8: Search Navigation + Slot Finder (TDD for the pure logic)

The Playwright-driven navigation is hard to unit-test, but the **slot iteration** is pure logic — given a "what's available" probe function, decide which slot to attempt. We split that out and test it.

**Files:**
- Modify: `src/booker.py` (add functions)
- Create: `tests/test_slot_finder.py`

- [ ] **Step 1: Write the failing test**

`tests/test_slot_finder.py`:

```python
from datetime import date, time

import pytest

from src.booker import pick_slot
from src.config import SLOT_PRIORITY


async def make_probe(available: set[tuple[time, time]]):
    async def probe(d: date, start: time, end: time) -> bool:
        return (start, end) in available
    return probe


@pytest.mark.asyncio
async def test_picks_first_priority_when_all_available():
    probe = await make_probe({s for s in SLOT_PRIORITY})
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result == (time(19, 30), time(20, 30))


@pytest.mark.asyncio
async def test_falls_back_to_second_priority():
    probe = await make_probe({(time(18, 30), time(19, 30)), (time(20, 30), time(21, 30))})
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result == (time(18, 30), time(19, 30))


@pytest.mark.asyncio
async def test_falls_back_to_third_priority():
    probe = await make_probe({(time(20, 30), time(21, 30))})
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result == (time(20, 30), time(21, 30))


@pytest.mark.asyncio
async def test_returns_none_when_nothing_available():
    probe = await make_probe(set())
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result is None


@pytest.mark.asyncio
async def test_iteration_order_matches_config():
    seen: list[tuple[time, time]] = []

    async def probe(d, start, end):
        seen.append((start, end))
        return False

    await pick_slot(date(2026, 5, 16), probe)
    assert seen == list(SLOT_PRIORITY)
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/test_slot_finder.py -v`
Expected: ImportError on `pick_slot`.

- [ ] **Step 3: Add `navigate_to_search` and `pick_slot` to `src/booker.py`**

Append to `src/booker.py`:

```python
from typing import Awaitable, Callable, Optional


SlotProbe = Callable[[date, time, time], Awaitable[bool]]


async def navigate_to_search(page: Page, log: logging.Logger) -> None:
    log.info("opening sports facility")
    await page.click(require(SELECTORS.sports_facility_link, "sports_facility_link"))
    await page.wait_for_load_state("networkidle")
    log.info("selecting tennis")
    await page.select_option(
        require(SELECTORS.activity_dropdown, "activity_dropdown"),
        require(SELECTORS.activity_tennis_value, "activity_tennis_value"),
    )
    await page.click(require(SELECTORS.search_button, "search_button"))
    await page.wait_for_load_state("networkidle")
    log.info("on search results page")


async def pick_slot(
    target_date: date,
    is_available: SlotProbe,
) -> Optional[tuple[time, time]]:
    """Return the first available (start, end) slot in priority order, else None.

    `is_available` is an async callable that probes whether a given slot has
    at least one bookable cell. The booker injects a probe that queries the
    live Playwright page; tests inject a fake.
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
    """Probe the search results page for an available cell of this slot."""
    selector = require(SELECTORS.available_slot_cell, "available_slot_cell").format(
        date=target_date.isoformat(),
        start=start.strftime("%H:%M"),
        end=end.strftime("%H:%M"),
    )
    locator = page.locator(selector)
    return await locator.count() > 0
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/test_slot_finder.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/booker.py tests/test_slot_finder.py
git commit -m "feat: search navigation and slot picker with tests"
```

---

## Task 9: Booking Submission (Click → Next → Agree → Submit → Verify)

**Files:**
- Modify: `src/booker.py`

- [ ] **Step 1: Add booking flow**

Append to `src/booker.py`:

```python
class BookingFailed(RuntimeError):
    pass


async def book_slot(
    page: Page,
    target_date: date,
    start: time,
    end: time,
    *,
    dry_run: bool,
    log: logging.Logger,
) -> None:
    """Click an available cell, advance to the confirmation page, submit.

    On success, the confirmation marker selector becomes visible. On failure,
    raises BookingFailed with the page text included for diagnosis.
    """
    cell_selector = require(SELECTORS.available_slot_cell, "available_slot_cell").format(
        date=target_date.isoformat(),
        start=start.strftime("%H:%M"),
        end=end.strftime("%H:%M"),
    )
    log.info("clicking available cell for %s %s-%s", target_date, start, end)
    await page.locator(cell_selector).first.click()

    log.info("clicking Next")
    await page.click(require(SELECTORS.next_button, "next_button"))
    await page.wait_for_load_state("networkidle")
    await page.screenshot(path=str(ARTIFACTS / "pre_submit.png"))

    log.info("ticking agreement checkbox")
    await page.check(require(SELECTORS.agreement_checkbox, "agreement_checkbox"))

    if dry_run:
        log.info("DRY RUN: stopping before final Submit")
        return

    log.info("clicking Submit")
    await page.click(require(SELECTORS.submit_button, "submit_button"))
    await page.wait_for_load_state("networkidle")
    await page.screenshot(path=str(ARTIFACTS / "post_submit.png"))

    marker = require(SELECTORS.confirmation_marker, "confirmation_marker")
    if await page.locator(marker).count() == 0:
        body = await page.content()
        raise BookingFailed(
            f"confirmation marker {marker!r} not found after submit; "
            f"page content (truncated): {body[:500]!r}"
        )
    log.info("booking confirmed for %s %s-%s", target_date, start, end)
```

- [ ] **Step 2: Smoke-test import**

Run: `uv run python -c "from src.booker import book_slot, BookingFailed; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add src/booker.py
git commit -m "feat: booking submission with confirmation check"
```

---

## Task 10: Main Orchestration (`run` + CLI)

**Files:**
- Modify: `src/booker.py`

- [ ] **Step 1: Add `run` and CLI entry point**

Append to `src/booker.py`:

```python
async def run(*, dry_run: bool = False, skip_sleep: bool = False) -> int:
    """Returns 0 on successful booking, 1 on no-slot-available or any failure."""
    username = os.environ["POLYU_USERNAME"]
    password = os.environ["POLYU_PASSWORD"]
    log = build_logger("booker", secret=password)

    target_date = compute_target_date()
    log.info("target booking date: %s", target_date)

    if not skip_sleep:
        log.info("sleeping until HKT %s", TRIGGER_TIME_HKT)
        sleep_until_hkt(TRIGGER_TIME_HKT)
        log.info("woke up, starting booking flow")

    ARTIFACTS.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=True)
        context: BrowserContext = await browser.new_context()
        page: Page = await context.new_page()
        page.set_default_timeout(SCREENSHOT_TIMEOUT_MS)

        try:
            await login(page, username, password, log)
            await navigate_to_search(page, log)
            await page.screenshot(path=str(ARTIFACTS / "search_results.png"))

            async def probe(d: date, s: time, e: time) -> bool:
                return await slot_has_availability(page, d, s, e)

            picked = await pick_slot(target_date, probe)
            if picked is None:
                log.error("no slot available for %s in any priority window", target_date)
                return 1

            start, end = picked
            await book_slot(page, target_date, start, end, dry_run=dry_run, log=log)
            return 0

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
    parser.add_argument("--dry-run", action="store_true",
                        help="walk the flow but don't click final Submit")
    parser.add_argument("--skip-sleep", action="store_true",
                        help="don't wait until HKT 08:30; run immediately")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(dry_run=args.dry_run, skip_sleep=args.skip_sleep)))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Add console script in `pyproject.toml`**

Modify `pyproject.toml`, add inside the `[project]` table:

```toml
[project.scripts]
book-tennis = "src.booker:main"
```

- [ ] **Step 3: Local dry-run smoke test**

Run (with real credentials, locally — does NOT click Submit):

```bash
POLYU_USERNAME='your-user' POLYU_PASSWORD='your-pw' \
    uv run book-tennis --dry-run --skip-sleep
```

Expected: full flow runs, stops just before Submit, exit 0. Screenshots in `artifacts/` show the pre-submit page with checkbox ticked.

If a step fails: read the log + screenshots, fix the failing selector in `src/config.py`, re-run.

- [ ] **Step 4: Commit**

```bash
git add src/booker.py pyproject.toml
git commit -m "feat: main run loop, CLI, dry-run mode"
```

---

## Task 11: GitHub Actions Workflow

**Files:**
- Create: `.github/workflows/book.yml`

- [ ] **Step 1: Create the workflow**

```yaml
name: Daily Tennis Booking

on:
  schedule:
    # 00:20 UTC = 08:20 HKT. Script then sleeps to 08:30:00.000 HKT.
    - cron: "20 0 * * *"
  workflow_dispatch:
    inputs:
      dry_run:
        description: "Dry run (don't click final Submit)"
        type: boolean
        default: false
      skip_sleep:
        description: "Skip the wait until 08:30 HKT (run immediately)"
        type: boolean
        default: true

jobs:
  book:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    env:
      TZ: Asia/Hong_Kong
      POLYU_USERNAME: ${{ secrets.POLYU_USERNAME }}
      POLYU_PASSWORD: ${{ secrets.POLYU_PASSWORD }}

    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v3

      - name: Install Python and dependencies
        run: |
          uv python install 3.12
          uv sync

      - name: Install Playwright Chromium
        run: uv run playwright install --with-deps chromium

      - name: Run booker
        id: book
        run: |
          ARGS=""
          if [ "${{ github.event.inputs.dry_run }}" = "true" ]; then ARGS="$ARGS --dry-run"; fi
          if [ "${{ github.event.inputs.skip_sleep }}" = "true" ]; then ARGS="$ARGS --skip-sleep"; fi
          uv run book-tennis $ARGS

      - name: Upload artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: booking-${{ github.run_id }}
          path: artifacts/
          retention-days: 14
          if-no-files-found: ignore
```

**Notes:**
- `TZ: Asia/Hong_Kong` makes the runner's clock match HKT, so `compute_target_date()` and `sleep_until_hkt()` work without further timezone juggling.
- Failure (`exit 1`) bubbles up — GitHub's default behaviour emails the repo owner.
- Manual `workflow_dispatch` defaults to `skip_sleep=true` so you can test on demand without waiting.
- Artifacts (screenshots) retained 14 days for triage.

- [ ] **Step 2: Commit (don't push yet)**

```bash
git add .github/workflows/book.yml
git commit -m "feat: GH Actions cron workflow"
```

---

## Task 12: README + Deployment Steps

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Replace `README.md`**

```markdown
# TennisBooking

Auto-books a PolyU tennis court 7 days ahead, daily at 08:30 HKT.

## What it does

- Runs as a GitHub Actions cron job every day at 08:20 HKT.
- Sleeps in-process until 08:30:00.000 HKT, then logs in and tries to book.
- Slot priority: 19:30–20:30, then 18:30–19:30, then 20:30–21:30.
- Books one slot per run; any court. No success notification.
- On no-slot-available or any error: workflow exits 1, GitHub emails you.

See `docs/superpowers/specs/2026-05-09-tennis-booking-design.md` for design.

## Local development

```bash
uv sync
uv run playwright install chromium
uv run pytest
```

Local dry-run (does not click final Submit):

```bash
POLYU_USERNAME='...' POLYU_PASSWORD='...' \
    uv run book-tennis --dry-run --skip-sleep
```

## Deployment

1. **Push to a private GitHub repo.** (Public repos are also free but expose the workflow file; selectors for university systems are not sensitive but credentials must be Secrets either way.)

2. **Add Secrets** in repo Settings → Secrets and variables → Actions → New repository secret:
   - `POLYU_USERNAME` — your student ID / login
   - `POLYU_PASSWORD` — your password

3. **Verify the workflow is enabled.** The Actions tab should show "Daily Tennis Booking". GitHub disables scheduled workflows on inactive repos after 60 days; push any commit periodically to keep alive.

4. **First-day smoke test.** Trigger manually:
   - Actions tab → Daily Tennis Booking → Run workflow → leave `dry_run` off, `skip_sleep` on.
   - Watch the logs. Download the artifact bundle. Confirm `post_submit.png` shows a confirmation page.
   - Cancel the booking on the PolyU site if you didn't actually want it.

5. **Let it run on schedule.** Day 2 onwards it fires daily at 08:20 HKT.

## Updating selectors when the PolyU UI changes

Symptoms: workflow exit 1, log shows a `Selector ... not configured` or Playwright timeout.

Fix:

```bash
POLYU_USERNAME='...' POLYU_PASSWORD='...' \
    uv run python scripts/discover_selectors.py
```

Open `artifacts/*.html`, update `src/config.py:Selectors`, commit, push.

## Costs

GitHub Actions free tier: 2000 min/month for private repos. This workflow uses ~1–2 min/run = 30–60 min/month. Far under quota.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: deployment guide"
```

---

## Task 13: First Live Run on GitHub Actions

This is the integration test. **Do not skip.**

- [ ] **Step 1: Push to GitHub**

```bash
gh repo create TennisBooking --private --source=. --remote=origin --push
```

(Or if repo exists: `git push -u origin main`.)

- [ ] **Step 2: Add Secrets via gh CLI**

```bash
gh secret set POLYU_USERNAME --body 'your-username'
gh secret set POLYU_PASSWORD --body 'your-password'
```

- [ ] **Step 3: Manual dry-run on the runner**

```bash
gh workflow run "Daily Tennis Booking" -f dry_run=true -f skip_sleep=true
gh run watch
```

Expected: green ✓. Download the artifact:

```bash
gh run download $(gh run list --workflow="Daily Tennis Booking" --limit=1 --json databaseId -q '.[0].databaseId')
```

Inspect `pre_submit.png` — should show the confirmation page with checkbox ticked, ready to submit.

- [ ] **Step 4: Manual real run (clicks Submit!)**

Only do this if you actually want a booking 7 days from today.

```bash
gh workflow run "Daily Tennis Booking" -f dry_run=false -f skip_sleep=true
gh run watch
```

Verify on the PolyU site that the booking exists.

- [ ] **Step 5: Wait for the first scheduled run**

Tomorrow at 08:30 HKT (with cron drift, possibly 08:30–08:35), the workflow fires automatically. Check Actions tab for the run; download artifact if anything looks off.

---

## Self-Review Notes

**Spec coverage:**
- Schedule daily 08:30 HKT → Tasks 4 + 11 ✓
- 7-day lookahead → Task 3 ✓
- Slot priority 19:30 > 18:30 > 20:30 → Task 5 + 8 ✓
- Single-slot booking → Task 8 (`pick_slot` returns one) ✓
- Any court → no court filter in selector format string ✓
- No success notification, exit 1 on failure → Task 10 ✓
- Playwright + GitHub Actions private → Tasks 7–11 ✓
- Credentials via Secrets, never logged → Tasks 2 + 11 ✓
- Screenshot artifacts → Tasks 9, 10, 11 ✓
- Open items (selectors) → Task 6 (discovery script) ✓
- Dry-run mode → Tasks 9, 10 ✓
- HKT timezone correctness → Tasks 3, 4, 11 (`TZ` env) ✓
- Cron-drift workaround (sleep-until) → Tasks 4, 11 ✓
- 10-min workflow timeout → Task 11 ✓

**Type/name consistency check:**
- `pick_slot`, `slot_has_availability`, `book_slot`, `BookingFailed`, `run` — names consistent across Tasks 8–10. ✓
- `SLOT_PRIORITY` defined Task 5, used Tasks 8–10. ✓
- `SELECTORS.available_slot_cell` is a format string with `{date}`, `{start}`, `{end}` — used identically in Tasks 8 and 9. ✓
- `compute_target_date` / `sleep_until_hkt` / `TRIGGER_TIME_HKT` — defined Tasks 3–5, used Task 10. ✓
- `build_logger(name, *, secret=...)` — signature consistent Tasks 2 and 10. ✓

**Placeholders:** none. Selector values in `config.py` are explicit `PENDING_DISCOVERY` sentinels that raise on use — that's a runtime guard, not a plan placeholder. Task 6 fills them in with real values from a real run.

**Scope:** single deployable. No decomposition needed.
