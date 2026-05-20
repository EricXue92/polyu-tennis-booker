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
