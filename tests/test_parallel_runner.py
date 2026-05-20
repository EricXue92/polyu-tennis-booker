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
