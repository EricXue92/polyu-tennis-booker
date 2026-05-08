from datetime import date, time

import pytest

from src.booker import pick_slot
from src.config import SLOT_PRIORITY


def make_probe(available: set[tuple[time, time]]):
    async def probe(d: date, start: time, end: time) -> bool:
        return (start, end) in available
    return probe


@pytest.mark.asyncio
async def test_picks_first_priority_when_all_available():
    probe = make_probe(set(SLOT_PRIORITY))
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result == (time(19, 30), time(20, 30))


@pytest.mark.asyncio
async def test_falls_back_to_second_priority():
    probe = make_probe({(time(18, 30), time(19, 30)), (time(20, 30), time(21, 30))})
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result == (time(18, 30), time(19, 30))


@pytest.mark.asyncio
async def test_falls_back_to_third_priority():
    probe = make_probe({(time(20, 30), time(21, 30))})
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result == (time(20, 30), time(21, 30))


@pytest.mark.asyncio
async def test_returns_none_when_nothing_available():
    probe = make_probe(set())
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
