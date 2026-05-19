from datetime import date, time

import pytest

from src.booker import pick_slot
from src.config import SLOT_PRIORITY


def make_probe(available: set[tuple[time, time]]):
    async def probe(d: date, start: time, end: time) -> bool:
        return (start, end) in available
    return probe


@pytest.mark.asyncio
async def test_returns_all_when_all_available():
    probe = make_probe(set(SLOT_PRIORITY))
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result == list(SLOT_PRIORITY)


@pytest.mark.asyncio
async def test_preserves_priority_order_with_partial_availability():
    probe = make_probe({(time(18, 30), time(19, 30)), (time(20, 30), time(21, 30))})
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result == [(time(18, 30), time(19, 30)), (time(20, 30), time(21, 30))]


@pytest.mark.asyncio
async def test_single_availability_returns_singleton_list():
    probe = make_probe({(time(20, 30), time(21, 30))})
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result == [(time(20, 30), time(21, 30))]


@pytest.mark.asyncio
async def test_returns_empty_when_nothing_available():
    probe = make_probe(set())
    result = await pick_slot(date(2026, 5, 16), probe)
    assert result == []


@pytest.mark.asyncio
async def test_iteration_order_matches_config():
    seen: list[tuple[time, time]] = []

    async def probe(d, start, end):
        seen.append((start, end))
        return False

    await pick_slot(date(2026, 5, 16), probe)
    assert seen == list(SLOT_PRIORITY)


@pytest.mark.asyncio
async def test_tuesday_excludes_staff_reserved_slots():
    # 2026-05-26 is a Tuesday — 18:30-19:30 and 19:30-20:30 are staff-only.
    probed: list[tuple[time, time]] = []

    async def probe(d, start, end):
        probed.append((start, end))
        return True

    result = await pick_slot(date(2026, 5, 26), probe)
    assert probed == [(time(20, 30), time(21, 30)), (time(17, 30), time(18, 30))]
    assert result == [(time(20, 30), time(21, 30)), (time(17, 30), time(18, 30))]
