"""Orchestrates N parallel booking sessions racing for the same target date.

Each session has three phases — prepare, click_through, submit. The first two
run concurrently across sessions. submit is serialized in priority rank order
by a single coordinator, so at most one session ever clicks Submit at a time.
The first SUCCESS sets a shared win event and stops the rest.
"""
from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Protocol

from playwright.async_api import BrowserContext, Page

ARTIFACTS = Path("artifacts")


class BookingResult(enum.Enum):
    SUCCESS = enum.auto()
    OCCUPIED = enum.auto()
    ERROR = enum.auto()


def artifact_path(kind: str, session_id: str) -> Path:
    """Per-session screenshot path, e.g. artifact_path('pre_submit', 's0')."""
    return ARTIFACTS / f"{kind}_{session_id}.png"


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
            for t in pending:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
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
            _, pending2 = await asyncio.wait(
                {done_w2, win_w2}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending2:
                t.cancel()
            for t in pending2:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

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
