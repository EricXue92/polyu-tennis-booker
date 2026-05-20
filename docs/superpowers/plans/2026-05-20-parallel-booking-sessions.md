# Parallel Booking Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-session sequential booking flow with N parallel Playwright contexts (one per priority slot) that race through cell-click and agreement-tick in parallel, with the final Submit serialized in priority order by a single coordinator.

**Architecture:** A new `src/parallel_runner.py` orchestrates N sessions via asyncio. Each session is a `SessionPhase` (Protocol) with three async phases — `prepare`, `click_through`, `submit`. Sessions race phases 1 and 2 in parallel; a coordinator awaits each session's "ready-to-submit" signal in rank order and grants the Submit turn one at a time. The first SUCCESS sets `win_event` and stops the others.

**Tech Stack:** Python 3.12+ · asyncio · Playwright (async) · pytest-asyncio · uv

**Spec:** `docs/superpowers/specs/2026-05-20-parallel-booking-sessions-design.md`

---

## File Structure

**New files:**
- `src/parallel_runner.py` — orchestrator + `SessionPhase` Protocol + `BookingResult` enum + `run_parallel()`.
- `tests/test_parallel_runner.py` — unit tests using `FakeSession` (no Playwright).

**Modified:**
- `src/config.py` — drop `(20:30, 21:30)` from `SLOT_PRIORITY`.
- `src/log.py` — accept optional `session_id` and prepend `[sN]` to records.
- `src/booker.py` — split `book_slot()` into `click_through()` + `submit_and_resolve()`; remove `restart_to_results()` and the retry loop in `run()`; replace `run()` body with a call into `parallel_runner.run_parallel()`. `main()` stays the same so `pyproject.toml` doesn't need changes.
- `tests/test_slot_finder.py` — update expectations for dropped 20:30 slot.
- `tests/test_log.py` — add session_id prefix tests.

**Responsibilities (one purpose per file):**
- `booker.py`: PolyU-specific Playwright primitives (login, search, click-through, submit-and-resolve). One session's worth of action.
- `parallel_runner.py`: coordination — many sessions, asyncio primitives, no PolyU-specific knowledge.
- `log.py`: logger construction + redaction + session prefix. No PolyU/Playwright knowledge.

---

## Task 1: Drop 20:30 slot from SLOT_PRIORITY and update tests

**Files:**
- Modify: `src/config.py:23-28`
- Modify: `tests/test_slot_finder.py`

- [ ] **Step 1: Update SLOT_PRIORITY in config.py**

Edit `src/config.py` lines 23–28:

```python
SLOT_PRIORITY: tuple[tuple[time, time], ...] = (
    (time(19, 30), time(20, 30)),
    (time(18, 30), time(19, 30)),
    (time(17, 30), time(18, 30)),
)
```

- [ ] **Step 2: Update affected tests in test_slot_finder.py**

Three tests reference the dropped `(20:30, 21:30)` slot. Replace them:

```python
@pytest.mark.asyncio
async def test_preserves_priority_order_with_partial_availability():
    probe = make_probe({(time(18, 30), time(19, 30)), (time(17, 30), time(18, 30))})
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result == [(time(18, 30), time(19, 30)), (time(17, 30), time(18, 30))]


@pytest.mark.asyncio
async def test_single_availability_returns_singleton_list():
    probe = make_probe({(time(17, 30), time(18, 30))})
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result == [(time(17, 30), time(18, 30))]


@pytest.mark.asyncio
async def test_tuesday_excludes_staff_reserved_slots():
    # 2026-05-26 is a Tuesday — 18:30-19:30 and 19:30-20:30 are staff-only.
    # After exclusion only 17:30 remains.
    probed: list[tuple[time, time]] = []

    async def probe(d, start, end):
        probed.append((start, end))
        return True

    result = await pick_slot(date(2026, 5, 26), probe)
    assert probed == [(time(17, 30), time(18, 30))]
    assert result == [(time(17, 30), time(18, 30))]
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `uv run pytest tests/test_slot_finder.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/config.py tests/test_slot_finder.py
git commit -m "config: drop 20:30 slot from SLOT_PRIORITY"
```

---

## Task 2: Add session_id prefix to logger

**Files:**
- Modify: `src/log.py`
- Modify: `tests/test_log.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_log.py`:

```python
def test_logger_with_session_id_prefixes_messages(capsys):
    logger = build_logger("booker", secret="x", session_id="s2")
    logger.info("hello")
    captured = capsys.readouterr()
    assert "[s2] hello" in captured.err


def test_logger_without_session_id_unchanged(capsys):
    logger = build_logger("booker", secret="x")
    logger.info("hello")
    captured = capsys.readouterr()
    assert "[s" not in captured.err
    assert "hello" in captured.err


def test_logger_session_id_does_not_break_redaction(caplog):
    logger = build_logger("test_redact", secret="hunter2", session_id="s0")
    with caplog.at_level(logging.INFO, logger="test_redact"):
        logger.info("password=hunter2")
    record = caplog.records[-1]
    assert "hunter2" not in record.getMessage()
    assert "***" in record.getMessage()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_log.py -v`
Expected: 3 new tests FAIL (TypeError on `session_id` kwarg).

- [ ] **Step 3: Add session_id support to build_logger**

Replace `build_logger` in `src/log.py`:

```python
def build_logger(name: str, *, secret: str, session_id: str | None = None) -> logging.Logger:
    logger = logging.getLogger(name if session_id is None else f"{name}.{session_id}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if session_id is not None:
        fmt = f"%(asctime)s %(levelname)s %(name)s: [{session_id}] %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(_RedactFilter(secret))
    logger.addHandler(handler)
    logger.propagate = True  # let caplog capture in tests
    return logger
```

- [ ] **Step 4: Run tests to verify all pass**

Run: `uv run pytest tests/test_log.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/log.py tests/test_log.py
git commit -m "log: optional session_id prefix for parallel sessions"
```

---

## Task 3: Add BookingResult enum and ARTIFACTS helper in parallel_runner.py

**Files:**
- Create: `src/parallel_runner.py`
- Create: `tests/test_parallel_runner.py`

- [ ] **Step 1: Write failing test for BookingResult enum and artifact path**

Create `tests/test_parallel_runner.py`:

```python
from src.parallel_runner import BookingResult, artifact_path


def test_booking_result_has_expected_members():
    assert BookingResult.SUCCESS.name == "SUCCESS"
    assert BookingResult.OCCUPIED.name == "OCCUPIED"
    assert BookingResult.ERROR.name == "ERROR"


def test_artifact_path_namespaces_by_session():
    assert artifact_path("pre_submit", "s0").name == "pre_submit_s0.png"
    assert artifact_path("post_submit", "s2").name == "post_submit_s2.png"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parallel_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: src.parallel_runner`.

- [ ] **Step 3: Create parallel_runner.py with the enum and helper**

Create `src/parallel_runner.py`:

```python
"""Orchestrates N parallel booking sessions racing for the same target date.

Each session has three phases — prepare, click_through, submit. The first two
run concurrently across sessions. submit is serialized in priority rank order
by a single coordinator, so at most one session ever clicks Submit at a time.
The first SUCCESS sets a shared win event and stops the rest.
"""
from __future__ import annotations

import enum
from pathlib import Path

ARTIFACTS = Path("artifacts")


class BookingResult(enum.Enum):
    SUCCESS = enum.auto()
    OCCUPIED = enum.auto()
    ERROR = enum.auto()


def artifact_path(kind: str, session_id: str) -> Path:
    """Per-session screenshot path, e.g. artifact_path('pre_submit', 's0')."""
    return ARTIFACTS / f"{kind}_{session_id}.png"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parallel_runner.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/parallel_runner.py tests/test_parallel_runner.py
git commit -m "parallel_runner: scaffold module with BookingResult + artifact_path"
```

---

## Task 4: Split book_slot into click_through and submit_and_resolve

**Files:**
- Modify: `src/booker.py` (split `book_slot` lines 182–283 into two functions; keep `book_slot` as thin wrapper for now so existing imports don't break).

- [ ] **Step 1: Add `click_through()` to booker.py**

Insert this function in `src/booker.py` immediately before `book_slot` (around line 182):

```python
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
```

- [ ] **Step 2: Add `submit_and_resolve()` to booker.py**

Insert this function after `click_through`:

```python
async def submit_and_resolve(
    page: Page,
    *,
    session_id: str | None = None,
    log: logging.Logger,
):
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
```

- [ ] **Step 3: Verify existing tests still pass**

Run: `uv run pytest -v`
Expected: all tests PASS (we added functions without changing the existing `book_slot`).

- [ ] **Step 4: Commit**

```bash
git add src/booker.py
git commit -m "booker: split click_through and submit_and_resolve helpers"
```

---

## Task 5: Define SessionPhase protocol and PolyUSession adapter

**Files:**
- Modify: `src/parallel_runner.py`

- [ ] **Step 1: Add `SessionPhase` Protocol and `PolyUSession` to parallel_runner.py**

Append to `src/parallel_runner.py`:

```python
import asyncio
import logging
from dataclasses import dataclass
from datetime import date, time
from typing import Protocol

from playwright.async_api import BrowserContext, Page


class SessionPhase(Protocol):
    """A bookable session for one slot. The coordinator drives these phases.

    The three async phases run in this order; the coordinator parallelizes
    `prepare` and `click_through` across sessions and serializes `submit`.
    `close` is always called once at the end (success or failure).
    """
    session_id: str
    rank: int  # 0 = highest priority

    async def prepare(self) -> None: ...
    async def click_through(self) -> None: ...
    async def submit(self) -> BookingResult: ...
    async def close(self) -> None: ...


@dataclass
class PolyUSession:
    """Concrete SessionPhase that drives a real Playwright BrowserContext."""
    session_id: str
    rank: int
    slot: tuple[time, time]
    target_date: date
    context: BrowserContext
    page: Page
    username: str
    password: str
    log: logging.Logger
    dry_run: bool

    async def prepare(self) -> None:
        from src.booker import login, prepare_search
        await login(self.page, self.username, self.password, self.log)
        await prepare_search(self.page, self.target_date, self.log)

    async def click_through(self) -> None:
        from src.booker import submit_search, slot_has_availability, click_through
        await submit_search(self.page, self.log)
        start, end = self.slot
        if not await slot_has_availability(self.page, self.target_date, start, end):
            # Surface as ERROR upward; coordinator will skip this session's Submit.
            raise _SlotUnavailable(f"slot {start}-{end} not in search results")
        await click_through(
            self.page, self.target_date, start, end,
            session_id=self.session_id, log=self.log,
        )

    async def submit(self) -> BookingResult:
        from src.booker import submit_and_resolve
        if self.dry_run:
            self.log.info("DRY RUN: stopping before final Submit")
            return BookingResult.SUCCESS
        return await submit_and_resolve(
            self.page, session_id=self.session_id, log=self.log,
        )

    async def close(self) -> None:
        try:
            await self.context.close()
        except Exception as e:
            self.log.warning("error closing context: %s", e)


class _SlotUnavailable(RuntimeError):
    """Raised by PolyUSession.click_through when the assigned slot is gone."""
```

- [ ] **Step 2: Verify the module still imports**

Run: `uv run python -c "from src.parallel_runner import PolyUSession, SessionPhase, BookingResult; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add src/parallel_runner.py
git commit -m "parallel_runner: SessionPhase protocol and PolyUSession adapter"
```

---

## Task 6: Coordinator — single session happy path

**Files:**
- Modify: `src/parallel_runner.py`
- Modify: `tests/test_parallel_runner.py`

- [ ] **Step 1: Write failing tests with a FakeSession**

Append to `tests/test_parallel_runner.py`:

```python
import asyncio
import logging
from dataclasses import dataclass, field

import pytest

from src.parallel_runner import BookingResult, run_parallel


@dataclass
class FakeSession:
    """Test double for SessionPhase. Records call order; returns canned values."""
    session_id: str
    rank: int
    submit_result: BookingResult = BookingResult.OCCUPIED
    prepare_delay: float = 0.0
    click_delay: float = 0.0
    submit_delay: float = 0.0
    raise_in_prepare: Exception | None = None
    raise_in_click: Exception | None = None
    calls: list[str] = field(default_factory=list)

    async def prepare(self) -> None:
        if self.prepare_delay:
            await asyncio.sleep(self.prepare_delay)
        self.calls.append("prepare")
        if self.raise_in_prepare:
            raise self.raise_in_prepare

    async def click_through(self) -> None:
        if self.click_delay:
            await asyncio.sleep(self.click_delay)
        self.calls.append("click_through")
        if self.raise_in_click:
            raise self.raise_in_click

    async def submit(self) -> BookingResult:
        if self.submit_delay:
            await asyncio.sleep(self.submit_delay)
        self.calls.append("submit")
        return self.submit_result

    async def close(self) -> None:
        self.calls.append("close")


@pytest.mark.asyncio
async def test_single_session_success_returns_zero():
    s = FakeSession("s0", rank=0, submit_result=BookingResult.SUCCESS)
    exit_code = await run_parallel([s])
    assert exit_code == 0
    assert s.calls == ["prepare", "click_through", "submit", "close"]


@pytest.mark.asyncio
async def test_single_session_occupied_returns_one():
    s = FakeSession("s0", rank=0, submit_result=BookingResult.OCCUPIED)
    exit_code = await run_parallel([s])
    assert exit_code == 1
    assert s.calls == ["prepare", "click_through", "submit", "close"]


@pytest.mark.asyncio
async def test_session_close_called_when_prepare_fails():
    s = FakeSession("s0", rank=0, raise_in_prepare=RuntimeError("login broke"))
    exit_code = await run_parallel([s])
    assert exit_code == 1
    assert "close" in s.calls
    assert "submit" not in s.calls
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parallel_runner.py -v`
Expected: FAIL with `ImportError: cannot import name 'run_parallel'`.

- [ ] **Step 3: Implement `run_parallel` in parallel_runner.py**

Append to `src/parallel_runner.py`:

```python
async def run_parallel(sessions: list[SessionPhase]) -> int:
    """Run sessions in parallel, serialize Submit in rank order. Returns exit code."""
    if not sessions:
        return 1

    win_event = asyncio.Event()
    ready_events = {s.session_id: asyncio.Event() for s in sessions}
    proceed_events = {s.session_id: asyncio.Event() for s in sessions}
    done_events = {s.session_id: asyncio.Event() for s in sessions}

    async def run_session(s: SessionPhase) -> None:
        try:
            try:
                await s.prepare()
                if win_event.is_set():
                    return
                await s.click_through()
                if win_event.is_set():
                    return
                ready_events[s.session_id].set()
                # Wait for coordinator to grant our Submit turn (or for a win).
                ready_wait = asyncio.create_task(proceed_events[s.session_id].wait())
                win_wait = asyncio.create_task(win_event.wait())
                done, pending = await asyncio.wait(
                    {ready_wait, win_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                if win_event.is_set():
                    return
                result = await s.submit()
                if result is BookingResult.SUCCESS:
                    win_event.set()
            except Exception as e:
                _module_log().warning("session %s aborted: %s", s.session_id, e)
        finally:
            done_events[s.session_id].set()
            try:
                await s.close()
            except Exception as e:
                _module_log().warning(
                    "session %s close failed: %s", s.session_id, e
                )

    async def coordinator() -> None:
        # Serve Submit turns strictly in rank order.
        ordered = sorted(sessions, key=lambda s: s.rank)
        for s in ordered:
            if win_event.is_set():
                return
            # Wait for THIS session to be ready, OR for it to finish without
            # reaching ready (prepare/click_through error), OR for a win.
            ready_w = asyncio.create_task(ready_events[s.session_id].wait())
            done_w = asyncio.create_task(done_events[s.session_id].wait())
            win_w = asyncio.create_task(win_event.wait())
            done, pending = await asyncio.wait(
                {ready_w, done_w, win_w}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            if win_event.is_set():
                return
            if done_events[s.session_id].is_set():
                # Session aborted before reaching ready — move to next rank.
                continue
            # Grant the Submit turn and wait for this session to finish before
            # serving the next rank.
            proceed_events[s.session_id].set()
            done_w2 = asyncio.create_task(done_events[s.session_id].wait())
            win_w2 = asyncio.create_task(win_event.wait())
            await asyncio.wait(
                {done_w2, win_w2}, return_when=asyncio.FIRST_COMPLETED
            )
            done_w2.cancel()
            win_w2.cancel()

    coord_task = asyncio.create_task(coordinator())
    session_tasks = [asyncio.create_task(run_session(s)) for s in sessions]
    await asyncio.gather(*session_tasks)
    coord_task.cancel()
    try:
        await coord_task
    except asyncio.CancelledError:
        pass

    return 0 if win_event.is_set() else 1


def _module_log() -> logging.Logger:
    return logging.getLogger("parallel_runner")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parallel_runner.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/parallel_runner.py tests/test_parallel_runner.py
git commit -m "parallel_runner: run_parallel coordinator (single-session paths)"
```

---

## Task 7: Coordinator — multi-session priority ordering

**Files:**
- Modify: `tests/test_parallel_runner.py`

- [ ] **Step 1: Write failing tests for multi-session ordering**

Append to `tests/test_parallel_runner.py`:

```python
@pytest.mark.asyncio
async def test_priority_zero_success_skips_others_submit():
    s0 = FakeSession("s0", rank=0, submit_result=BookingResult.SUCCESS)
    s1 = FakeSession("s1", rank=1, submit_result=BookingResult.SUCCESS)
    s2 = FakeSession("s2", rank=2, submit_result=BookingResult.SUCCESS)
    exit_code = await run_parallel([s0, s1, s2])
    assert exit_code == 0
    assert "submit" in s0.calls
    assert "submit" not in s1.calls
    assert "submit" not in s2.calls
    # All three must close even though only s0 submitted.
    assert "close" in s0.calls
    assert "close" in s1.calls
    assert "close" in s2.calls


@pytest.mark.asyncio
async def test_priority_zero_occupied_advances_to_priority_one():
    s0 = FakeSession("s0", rank=0, submit_result=BookingResult.OCCUPIED)
    s1 = FakeSession("s1", rank=1, submit_result=BookingResult.SUCCESS)
    s2 = FakeSession("s2", rank=2, submit_result=BookingResult.SUCCESS)
    exit_code = await run_parallel([s0, s1, s2])
    assert exit_code == 0
    assert "submit" in s0.calls
    assert "submit" in s1.calls
    assert "submit" not in s2.calls


@pytest.mark.asyncio
async def test_all_occupied_returns_one():
    sessions = [
        FakeSession(f"s{i}", rank=i, submit_result=BookingResult.OCCUPIED)
        for i in range(3)
    ]
    exit_code = await run_parallel(sessions)
    assert exit_code == 1
    for s in sessions:
        assert "submit" in s.calls
        assert "close" in s.calls


@pytest.mark.asyncio
async def test_priority_order_holds_when_low_priority_ready_first():
    """s2 finishes click_through fast; s0 is slow. s0 still gets Submit first."""
    s0 = FakeSession(
        "s0", rank=0,
        click_delay=0.05,
        submit_result=BookingResult.SUCCESS,
    )
    s1 = FakeSession("s1", rank=1, submit_result=BookingResult.SUCCESS)
    s2 = FakeSession("s2", rank=2, submit_result=BookingResult.SUCCESS)
    exit_code = await run_parallel([s2, s1, s0])  # list order != rank
    assert exit_code == 0
    assert "submit" in s0.calls
    assert "submit" not in s1.calls
    assert "submit" not in s2.calls


@pytest.mark.asyncio
async def test_priority_zero_aborts_before_ready_advances_to_priority_one():
    """If s0 fails in click_through, s1 should get the next Submit turn."""
    s0 = FakeSession("s0", rank=0, raise_in_click=RuntimeError("slot gone"))
    s1 = FakeSession("s1", rank=1, submit_result=BookingResult.SUCCESS)
    s2 = FakeSession("s2", rank=2, submit_result=BookingResult.SUCCESS)
    exit_code = await run_parallel([s0, s1, s2])
    assert exit_code == 0
    assert "submit" not in s0.calls  # s0 failed before submit
    assert "submit" in s1.calls
    assert "submit" not in s2.calls


@pytest.mark.asyncio
async def test_win_event_short_circuits_pending_sessions_at_yield_points():
    """A slow s1/s2 that hasn't reached click_through yet should bail when s0 wins."""
    s0 = FakeSession("s0", rank=0, submit_result=BookingResult.SUCCESS)
    s1 = FakeSession("s1", rank=1, click_delay=0.2, submit_result=BookingResult.SUCCESS)
    s2 = FakeSession("s2", rank=2, click_delay=0.2, submit_result=BookingResult.SUCCESS)
    exit_code = await run_parallel([s0, s1, s2])
    assert exit_code == 0
    # s1/s2 may or may not have called click_through (race), but they MUST NOT
    # call submit after s0 wins.
    assert "submit" not in s1.calls
    assert "submit" not in s2.calls
```

- [ ] **Step 2: Run tests to verify all pass with the coordinator from Task 6**

Run: `uv run pytest tests/test_parallel_runner.py -v`
Expected: all 11 tests PASS.

If `test_priority_order_holds_when_low_priority_ready_first` fails, the coordinator is serving sessions in list order rather than rank order — re-check the `sorted(sessions, key=lambda s: s.rank)` line in Task 6.

- [ ] **Step 3: Commit**

```bash
git add tests/test_parallel_runner.py
git commit -m "parallel_runner: multi-session priority-ordering tests"
```

---

## Task 8: Wire run_parallel into booker.run() entry point

**Files:**
- Modify: `src/booker.py` (replace the body of `run()` and remove `book_slot` and `restart_to_results`).
- Modify: `src/parallel_runner.py` (add high-level `book_parallel()` helper that builds PolyUSessions and calls `run_parallel`).

- [ ] **Step 1: Add `book_parallel` orchestrator to parallel_runner.py**

Append to `src/parallel_runner.py`:

```python
async def book_parallel(
    *,
    username: str,
    password: str,
    target_date: date,
    slots: list[tuple[time, time]],
    dry_run: bool,
) -> int:
    """Build N PolyUSession instances and run them via run_parallel.

    Returns the process exit code (0 = booked, 1 = nothing booked).
    """
    from playwright.async_api import async_playwright

    from src.log import build_logger

    if not slots:
        build_logger("booker", secret=password).error(
            "no slots in priority list for %s", target_date
        )
        return 1

    ARTIFACTS.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        sessions: list[SessionPhase] = []
        try:
            for rank, slot in enumerate(slots):
                sid = f"s{rank}"
                ctx = await browser.new_context()
                page = await ctx.new_page()
                page.set_default_timeout(20_000)
                sessions.append(PolyUSession(
                    session_id=sid,
                    rank=rank,
                    slot=slot,
                    target_date=target_date,
                    context=ctx,
                    page=page,
                    username=username,
                    password=password,
                    log=build_logger("booker", secret=password, session_id=sid),
                    dry_run=dry_run,
                ))
            return await run_parallel(sessions)
        finally:
            await browser.close()
```

- [ ] **Step 2: Rewrite booker.run() to use book_parallel**

Replace lines 286–393 of `src/booker.py` (the `run()` function body) with:

```python
async def run(*, dry_run: bool = False, skip_sleep: bool = False) -> int:
    """Returns 0 on successful booking, 1 on no-slot-available or any failure."""
    from src.parallel_runner import book_parallel

    username = os.environ["POLYU_USERNAME"]
    password = os.environ["POLYU_PASSWORD"]
    log = build_logger("booker", secret=password)

    target_date = compute_target_date()
    log.info("target booking date: %s", target_date)

    prelogin_target = (
        datetime.combine(date.today(), TRIGGER_TIME_HKT)
        - timedelta(seconds=PRELOGIN_LEAD_SECONDS)
    ).time()

    if not skip_sleep:
        log.info("sleeping until HKT %s (pre-login)", prelogin_target)
        sleep_until_hkt(prelogin_target)
        log.info("woke up for pre-login phase")

    slots = list(slot_priority_for(target_date))
    log.info("preparing %d parallel session(s) for slots %s", len(slots), slots)

    # Build sessions and run them. Sessions do their own login + prepare_search
    # concurrently; once all are ready, this function blocks here briefly while
    # the sessions sleep_until_hkt(TRIGGER_TIME_HKT) inside their click_through.
    # We push the trigger-time sleep down into PolyUSession.click_through so it
    # can fire Search at 08:30:00.000 sharp from each context.
    return await book_parallel(
        username=username,
        password=password,
        target_date=target_date,
        slots=slots,
        dry_run=dry_run,
    )
```

- [ ] **Step 3: Push the trigger-time sleep into PolyUSession.click_through**

In `src/parallel_runner.py`, modify `PolyUSession.click_through`:

```python
    async def click_through(self) -> None:
        from src.booker import (
            submit_search, slot_has_availability, click_through,
        )
        from src.config import TRIGGER_TIME_HKT
        from src.dates import sleep_until_hkt

        # Sleep here (not in prepare) so every session lands Search at 08:30:00.000.
        # Pre-login is already done by prepare; this is the gate.
        if not getattr(self, "_skip_trigger_sleep", False):
            self.log.info("prep complete; sleeping until HKT %s", TRIGGER_TIME_HKT)
            sleep_until_hkt(TRIGGER_TIME_HKT)
            self.log.info("woke up at trigger time, firing Search")

        await submit_search(self.page, self.log)
        start, end = self.slot
        if not await slot_has_availability(self.page, self.target_date, start, end):
            raise _SlotUnavailable(f"slot {start}-{end} not in search results")
        await click_through(
            self.page, self.target_date, start, end,
            session_id=self.session_id, log=self.log,
        )
```

Add `_skip_trigger_sleep: bool = False` as a field on `PolyUSession` and surface it from `book_parallel`:

```python
@dataclass
class PolyUSession:
    session_id: str
    rank: int
    slot: tuple[time, time]
    target_date: date
    context: BrowserContext
    page: Page
    username: str
    password: str
    log: logging.Logger
    dry_run: bool
    _skip_trigger_sleep: bool = False
```

And add a `skip_sleep` parameter to `book_parallel`, passing it through:

```python
async def book_parallel(
    *,
    username: str,
    password: str,
    target_date: date,
    slots: list[tuple[time, time]],
    dry_run: bool,
    skip_sleep: bool = False,
) -> int:
    ...
    sessions.append(PolyUSession(
        ...
        _skip_trigger_sleep=skip_sleep,
    ))
```

Finally update `booker.run()` to pass `skip_sleep=skip_sleep` into `book_parallel`.

- [ ] **Step 4: Remove the now-unused book_slot and restart_to_results from booker.py**

Delete these top-level functions from `src/booker.py`:
- `book_slot` (was lines 182–283)
- `restart_to_results` (was lines 147–163)
- The `BookingFailed` exception class can stay; it's no longer raised but is harmless and a callsite may still reference it from older artifacts.

Also remove the now-unused import of `Awaitable, Callable` from `typing` if pick_slot is no longer imported anywhere outside tests — leave it; `pick_slot` is still used in tests and exported.

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -v`
Expected: every test PASSES.

If `tests/test_slot_finder.py` complains that `pick_slot` is gone, leave it in `booker.py` — it's used by tests and harmless.

- [ ] **Step 6: Commit**

```bash
git add src/booker.py src/parallel_runner.py
git commit -m "booker: route run() through parallel_runner.book_parallel"
```

---

## Task 9: Smoke-test with N=2 locally (manual validation)

**Files:** none (manual validation gate; do NOT commit any temporary edits made during this task).

This step exists because the design's biggest unknown is whether PolyU rejects concurrent same-account sessions. We test with N=2 before promoting N=3 to production.

- [ ] **Step 1: Temporarily reduce SLOT_PRIORITY to 2 slots for the smoke test**

Edit `src/config.py` in your working tree only (do not commit):

```python
SLOT_PRIORITY: tuple[tuple[time, time], ...] = (
    (time(19, 30), time(20, 30)),
    (time(17, 30), time(18, 30)),
)
```

- [ ] **Step 2: Run a local dry-run BEFORE 08:30 HKT (so candidate slots still exist)**

Run:

```bash
POLYU_USERNAME=... POLYU_PASSWORD=... \
    uv run book-tennis --dry-run --skip-sleep
```

Expected outputs:
- Two `[s0]` and `[s1]` log streams interleaved.
- Both sessions complete `login complete`.
- Both sessions reach `clicking available cell` (or one reaches `slot ... not in search results` if the slot is already gone for today).
- Two screenshots: `artifacts/pre_submit_s0.png`, `artifacts/pre_submit_s1.png`.
- Process exits 0 (dry-run returns SUCCESS from `PolyUSession.submit`).

**Pass criterion:** both sessions logged in successfully and reached at least `clicking available cell`. If session s1 failed login or got logged out after s0 logged in → PolyU rejects concurrent sessions; stop here and revisit with Approach B (HTTP POST).

- [ ] **Step 3: Revert the SLOT_PRIORITY change**

```bash
git checkout src/config.py
```

(This restores the 3-slot list from Task 1.)

- [ ] **Step 4: Record the smoke-test outcome in the spec**

If smoke test passed, append to the bottom of `docs/superpowers/specs/2026-05-20-parallel-booking-sessions-design.md`:

```markdown
## Smoke-test result (N=2)

Date: YYYY-MM-DD. PolyU tolerates concurrent same-account sessions: both s0
and s1 reached click-through and produced screenshots. Approach A is viable.
```

Then commit:

```bash
git add docs/superpowers/specs/2026-05-20-parallel-booking-sessions-design.md
git commit -m "spec: record N=2 smoke-test result"
```

If smoke test failed, append the failure mode and **stop the plan**. The HTTP fast-path (Approach B) requires a new spec.

---

## Task 10: Live N=3 dry-run via GitHub Actions

**Files:** none (validation gate using the actual deployment).

- [ ] **Step 1: Push the branch and trigger a dry-run before 08:30 HKT**

```bash
git push origin <your-branch>
gh workflow run "Daily Tennis Booking" --ref <your-branch> \
    -f dry_run=true -f skip_sleep=true
gh run watch <id> --interval 15 --exit-status
```

Run this BEFORE 08:30 HKT so peak slots are still available. (Per CLAUDE.md: dry-runs after 08:30 see empty slot lists and exit 1 before producing pre_submit screenshots.)

- [ ] **Step 2: Inspect uploaded artifacts**

```bash
gh run download <id>
ls booking-<id>/
```

Expected files: `pre_submit_s0.png`, `pre_submit_s1.png`, `pre_submit_s2.png` (one per session that reached the agreement-tick step). At minimum one screenshot should be present.

- [ ] **Step 3: Inspect logs for parallel behavior**

```bash
gh run view <id> --log | grep -E '^\S+ \S+ booker\.s[012]: '
```

Expected: interleaved `[s0]`, `[s1]`, `[s2]` lines; all three sessions reach `login complete` and at least one reaches `clicking Submit` (or `DRY RUN: stopping before final Submit` since this is a dry run).

**Pass criterion:** at least one session reached Submit-ready; no Playwright timeouts in pre-Submit phases.

- [ ] **Step 4: Merge to main**

```bash
gh pr create --title "feat(booker): parallel booking sessions" --body "$(cat <<'EOF'
## Summary
- Replace single-session sequential booking with N parallel Playwright contexts
- Submit serialized in priority order via single-dequeuer coordinator
- Drops 20:30 from SLOT_PRIORITY (user preference)

## Test plan
- [x] Offline unit tests for parallel_runner coordinator
- [x] N=2 local smoke test (PolyU accepts concurrent sessions)
- [x] N=3 live dry-run via gh workflow run --dry-run
- [ ] Live production booking on next scheduled CF-triggered run

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Then merge once approved.

- [ ] **Step 5: Watch the next scheduled CF-triggered run (08:30 HKT next day)**

Confirm exit 0 and a real booking in the artifacts.

If the live run fails despite passing dry-run, the most likely cause is timing: revisit `PRELOGIN_LEAD_SECONDS` — three parallel logins may need more lead time than the current 60s.

---

## Self-review notes (resolved before publishing)

- Verified spec coverage: every section maps to a task. SLOT_PRIORITY drop → Task 1. Session-id logging → Task 2. BookingResult enum → Task 3. Split book_slot → Task 4. SessionPhase protocol + PolyUSession adapter → Task 5. Coordinator → Tasks 6–7. Wire into CLI → Task 8. Smoke test → Tasks 9–10.
- No `book_slot` references in any task after Task 8 (it's deleted there).
- `click_through` and `submit_and_resolve` signatures used in PolyUSession (Task 5) match the definitions added in Task 4.
- `SessionPhase` is structurally satisfied by `FakeSession` (Task 6) and `PolyUSession` (Task 5) — both have `session_id`, `rank`, and the four async methods.
- The coordinator's `done_events` dict (Task 6) is keyed by `session_id` and looked up by `s.session_id` in all sites.
