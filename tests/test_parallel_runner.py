from src.parallel_runner import BookingResult, artifact_path


def test_booking_result_has_expected_members():
    assert BookingResult.SUCCESS.name == "SUCCESS"
    assert BookingResult.OCCUPIED.name == "OCCUPIED"
    assert BookingResult.ERROR.name == "ERROR"


def test_artifact_path_namespaces_by_session():
    assert artifact_path("pre_submit", "s0").name == "pre_submit_s0.png"
    assert artifact_path("post_submit", "s2").name == "post_submit_s2.png"


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
