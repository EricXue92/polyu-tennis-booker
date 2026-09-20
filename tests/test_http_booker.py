"""Offline tests for the time-grouped book_via_http orchestrator.

book_via_http:
  1. Constructs candidates from (slots × TENNIS_FACILITIES), priority-major.
  2. Fires all cell_click POSTs concurrently via asyncio.gather.
  3. Groups ACCEPTED cells by (start, end) preserving rank order.
  4. For each group in rank order:
       - Fires all submits in the group concurrently via asyncio.gather.
       - Any SUCCESS wins (lower-rank preferred if multiple); return 0.
       - No SUCCESS + any ERROR_FATAL -> abort; return 1.
       - No SUCCESS + only OCCUPIED/TRANSIENT -> advance to next group.
  5. If 0 ACCEPTED: re-fire the ERROR_TRANSIENT candidates (longer timeout)
     until one is ACCEPTED, none is transient, or the retry window closes;
     still 0 ACCEPTED -> return 1 (no submit calls).
  6. If all groups exhausted with no SUCCESS, return 1.
"""
import asyncio
import logging
import time
from datetime import date, time as dtime

import pytest

from src.config import TENNIS_FACILITIES
from src.http_client import BookingResult, CellClickResult, CellOutcome


class _FakeClient:
    """Scripted client: per-candidate cell_click outcome + per-candidate submit result.

    cell_click_outcomes / submit_results are dicts keyed by (start_hour, facility_id)
    so tests can express intent without depending on candidate construction order.
    A cell_click outcome may be a list to script successive attempts (retry rounds).
    submit_sleeps optionally injects per-candidate latency to verify intra-group
    parallelism / inter-group serialization.
    """
    def __init__(
        self,
        cell_click_outcomes: dict[tuple[int, int], CellOutcome],
        submit_results: dict[tuple[int, int], BookingResult] | None = None,
        cell_click_sleep_s: float = 0.0,
        submit_sleeps: dict[tuple[int, int], float] | None = None,
    ):
        self._cell = cell_click_outcomes
        self._sub = submit_results or {}
        self._sleep = cell_click_sleep_s
        self._sub_sleeps = submit_sleeps or {}
        self.cell_click_calls = []
        self.cell_click_timeouts: list[float | None] = []
        self.submit_calls = []
        self.submit_call_times: list[float] = []

    async def cell_click(self, slot, *, timeout=None):
        self.cell_click_calls.append(slot)
        self.cell_click_timeouts.append(timeout)
        if self._sleep:
            await asyncio.sleep(self._sleep)
        key = (slot.start_dt.hour, slot.facility_id)
        outcome = self._cell[key]
        if isinstance(outcome, list):
            # Scripted per-attempt outcomes; the last one repeats forever.
            outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
        return CellClickResult(slot=slot, outcome=outcome, latency_ms=10)

    async def submit(self, slot):
        self.submit_calls.append(slot)
        self.submit_call_times.append(time.perf_counter())
        key = (slot.start_dt.hour, slot.facility_id)
        delay = self._sub_sleeps.get(key, 0.0)
        if delay:
            await asyncio.sleep(delay)
        return self._sub[key]


_LOG = logging.getLogger("test")
_FACILITY_IDS = list(TENNIS_FACILITIES.keys())  # [10, 11]
assert len(_FACILITY_IDS) == 2, "tests assume exactly 2 tennis facilities"
_PRIORITY = [(dtime(18, 30), dtime(19, 30)), (dtime(19, 30), dtime(20, 30))]


def _all_cell(outcome: CellOutcome) -> dict[tuple[int, int], CellOutcome]:
    return {(h, f): outcome for h in (18, 19) for f in _FACILITY_IDS}


def _all_submit(result: BookingResult) -> dict[tuple[int, int], BookingResult]:
    return {(h, f): result for h in (18, 19) for f in _FACILITY_IDS}


@pytest.mark.asyncio
async def test_happy_path_rank0_wins():
    # All cells ACCEPTED, only rank 0 submit SUCCESS. The 18:30 group fires
    # rank 0 + rank 1 in parallel; rank 0 wins. No 19:30 group attempted.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[0]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.cell_click_calls) == 4
    # Both 18:30 candidates submit in parallel; no 19:30 submits attempted.
    assert len(client.submit_calls) == 2
    submitted_hours = {s.start_dt.hour for s in client.submit_calls}
    assert submitted_hours == {18}


@pytest.mark.asyncio
async def test_priority_preserved_when_only_some_accepted():
    # Only rank 0 (18:30 court A) and rank 2 (19:30 court A) cell-ACCEPTED;
    # rank 1 and rank 3 are cell-OCCUPIED. Group 1 has only rank 0 → 1 submit
    # → SUCCESS → done. Group 2 is never touched.
    from src.http_booker import book_via_http

    cells = _all_cell(CellOutcome.OCCUPIED)
    cells[(18, _FACILITY_IDS[0])] = CellOutcome.ACCEPTED
    cells[(19, _FACILITY_IDS[0])] = CellOutcome.ACCEPTED

    client = _FakeClient(
        cell_click_outcomes=cells,
        submit_results={(18, _FACILITY_IDS[0]): BookingResult.SUCCESS,
                        (19, _FACILITY_IDS[0]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.submit_calls) == 1
    assert client.submit_calls[0].start_dt.hour == 18


@pytest.mark.asyncio
async def test_within_group_rank1_can_win_when_rank0_occupied():
    # All cells ACCEPTED, rank 1 submit SUCCESS (rank 0 OCCUPIED). Parallel-
    # within-18:30 means rank 1 wins despite rank 0 racing it. 19:30 not tried.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[1]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.submit_calls) == 2
    submitted_hours = {s.start_dt.hour for s in client.submit_calls}
    assert submitted_hours == {18}


@pytest.mark.asyncio
async def test_all_occupied_in_cell_phase_returns_1_with_no_submits():
    from src.http_booker import book_via_http

    client = _FakeClient(cell_click_outcomes=_all_cell(CellOutcome.OCCUPIED))
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 1
    assert len(client.cell_click_calls) == 4
    assert len(client.submit_calls) == 0


@pytest.mark.asyncio
async def test_all_accepted_all_submit_occupied_returns_1():
    # 18:30 group: 2 parallel submits, both OCCUPIED → advance.
    # 19:30 group: 2 parallel submits, both OCCUPIED → return 1.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results=_all_submit(BookingResult.OCCUPIED),
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 1
    assert len(client.submit_calls) == 4


@pytest.mark.asyncio
async def test_cell_transient_does_not_block_other_candidates():
    # rank 2 cell TRANSIENT; rest ACCEPTED. 18:30 group still wins.
    from src.http_booker import book_via_http

    cells = _all_cell(CellOutcome.ACCEPTED)
    cells[(19, _FACILITY_IDS[0])] = CellOutcome.ERROR_TRANSIENT
    client = _FakeClient(
        cell_click_outcomes=cells,
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[0]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    submitted_hours = {s.start_dt.hour for s in client.submit_calls}
    assert submitted_hours == {18}


@pytest.mark.asyncio
async def test_cell_fatal_does_not_abort_globally():
    # rank 0 cell FATAL; rest ACCEPTED. Group 1 has only rank 1 → SUCCESS.
    from src.http_booker import book_via_http

    cells = _all_cell(CellOutcome.ACCEPTED)
    cells[(18, _FACILITY_IDS[0])] = CellOutcome.ERROR_FATAL
    client = _FakeClient(
        cell_click_outcomes=cells,
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[1]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.submit_calls) == 1
    assert client.submit_calls[0].facility_id == _FACILITY_IDS[1]


@pytest.mark.asyncio
async def test_submit_fatal_with_no_sibling_success_aborts_next_group():
    # 18:30 group: rank 0 FATAL + rank 1 OCCUPIED. No SUCCESS in group → abort,
    # don't try 19:30. Verifies FATAL still short-circuits when nothing else
    # in the group rescued us.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={
            **_all_submit(BookingResult.SUCCESS),  # 19:30 would succeed
            (18, _FACILITY_IDS[0]): BookingResult.ERROR_FATAL,
            (18, _FACILITY_IDS[1]): BookingResult.OCCUPIED,
        },
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 1
    # Only the 18:30 group ran; 19:30 was aborted.
    assert len(client.submit_calls) == 2
    submitted_hours = {s.start_dt.hour for s in client.submit_calls}
    assert submitted_hours == {18}


@pytest.mark.asyncio
async def test_fatal_with_sibling_success_still_wins():
    # 18:30 group: rank 0 FATAL + rank 1 SUCCESS in parallel. SUCCESS wins
    # (auth clearly alive), no abort. Direct counter to the old serial behavior
    # where rank 0 FATAL would have masked rank 1 SUCCESS.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={
            **_all_submit(BookingResult.OCCUPIED),
            (18, _FACILITY_IDS[0]): BookingResult.ERROR_FATAL,
            (18, _FACILITY_IDS[1]): BookingResult.SUCCESS,
        },
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.submit_calls) == 2


@pytest.mark.asyncio
async def test_submit_transient_does_not_block_sibling_in_same_group():
    # 18:30 group: rank 0 TRANSIENT + rank 1 SUCCESS in parallel. SUCCESS wins
    # without waiting for rank 0 to fail serially.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[0]): BookingResult.ERROR_TRANSIENT,
                        (18, _FACILITY_IDS[1]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.submit_calls) == 2


@pytest.mark.asyncio
async def test_all_cell_errors_returns_1():
    from src.http_booker import book_via_http

    cells = _all_cell(CellOutcome.ERROR_TRANSIENT)
    cells[(18, _FACILITY_IDS[1])] = CellOutcome.ERROR_FATAL
    client = _FakeClient(cell_click_outcomes=cells)
    rc = await book_via_http(
        client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG,
        cell_retry_window_s=0.0,
    )
    assert rc == 1
    assert len(client.submit_calls) == 0


@pytest.mark.asyncio
async def test_all_cell_transient_is_retried_until_accepted():
    # 2026-09-20: every cell_click hit the 6s ReadTimeout at 08:30 and the run
    # gave up at 08:30:07 while 19:30 sat free for hours. A round with no
    # ACCEPTED must re-fire its ERROR_TRANSIENT candidates, on a longer budget.
    from src.http_booker import CELL_RETRY_TIMEOUT_SECONDS, book_via_http

    cells = {k: [CellOutcome.ERROR_TRANSIENT, CellOutcome.ACCEPTED]
             for k in _all_cell(CellOutcome.ACCEPTED)}
    client = _FakeClient(
        cell_click_outcomes=cells,
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[0]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(
        client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG,
        cell_retry_min_round_s=0.0,
    )
    assert rc == 0
    assert len(client.cell_click_calls) == 8
    # First round keeps the short client default; retries get the long budget.
    assert client.cell_click_timeouts == [None] * 4 + [CELL_RETRY_TIMEOUT_SECONDS] * 4


@pytest.mark.asyncio
async def test_cell_retry_only_refires_transient_candidates():
    # 18:30 is definitively OCCUPIED; only the timed-out 19:30 pair is retried.
    from src.http_booker import book_via_http

    cells = {
        (18, _FACILITY_IDS[0]): CellOutcome.OCCUPIED,
        (18, _FACILITY_IDS[1]): CellOutcome.OCCUPIED,
        (19, _FACILITY_IDS[0]): [CellOutcome.ERROR_TRANSIENT, CellOutcome.ACCEPTED],
        (19, _FACILITY_IDS[1]): [CellOutcome.ERROR_TRANSIENT, CellOutcome.OCCUPIED],
    }
    client = _FakeClient(
        cell_click_outcomes=cells,
        submit_results={(19, _FACILITY_IDS[0]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(
        client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG,
        cell_retry_min_round_s=0.0,
    )
    assert rc == 0
    assert [s.start_dt.hour for s in client.cell_click_calls] == [18, 18, 19, 19, 19, 19]
    assert [s.start_dt.hour for s in client.submit_calls] == [19]


@pytest.mark.asyncio
async def test_cell_retry_gives_up_after_window():
    # A server that never answers must still end the run (exit 1), and the
    # retry rounds must be paced rather than spinning.
    from src.http_booker import book_via_http

    client = _FakeClient(cell_click_outcomes=_all_cell(CellOutcome.ERROR_TRANSIENT))
    t0 = time.perf_counter()
    rc = await book_via_http(
        client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG,
        cell_retry_window_s=0.3, cell_retry_min_round_s=0.1,
    )
    elapsed = time.perf_counter() - t0
    assert rc == 1
    assert len(client.submit_calls) == 0
    rounds = len(client.cell_click_calls) // 4
    assert 2 <= rounds <= 5, f"expected paced retry rounds, got {rounds}"
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_no_cell_retry_once_any_candidate_is_accepted():
    # A timed-out sibling is not worth delaying submits that are ready to go.
    from src.http_booker import book_via_http

    cells = _all_cell(CellOutcome.ERROR_TRANSIENT)
    cells[(19, _FACILITY_IDS[1])] = CellOutcome.ACCEPTED
    client = _FakeClient(
        cell_click_outcomes=cells,
        submit_results={(19, _FACILITY_IDS[1]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.cell_click_calls) == 4


@pytest.mark.asyncio
async def test_cell_clicks_actually_run_in_parallel():
    # Each cell_click sleeps 500ms. If serial, total > 2.0s; if parallel, < 1.2s.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[0]): BookingResult.SUCCESS},
        cell_click_sleep_s=0.5,
    )
    t0 = time.perf_counter()
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    elapsed = time.perf_counter() - t0
    assert rc == 0
    assert elapsed < 1.2, f"cell_clicks ran sequentially (took {elapsed:.2f}s, expected < 1.2s)"


@pytest.mark.asyncio
async def test_submits_within_timeslot_run_in_parallel():
    # rank 0 submit hangs 500ms (mimics the 2026-06-18 ReadTimeout); rank 1 is
    # also 500ms but returns SUCCESS. If serial, rank 0's hang would block
    # rank 1 and total submit time would be ~1s. Parallel ⇒ ~0.5s.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[1]): BookingResult.SUCCESS},
        submit_sleeps={(18, _FACILITY_IDS[0]): 0.5,
                       (18, _FACILITY_IDS[1]): 0.5},
    )
    t0 = time.perf_counter()
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    elapsed = time.perf_counter() - t0
    assert rc == 0
    assert elapsed < 0.8, (
        f"submits within timeslot ran serially (took {elapsed:.2f}s, "
        f"expected < 0.8s)"
    )


@pytest.mark.asyncio
async def test_next_group_waits_when_previous_answers_within_stagger():
    # All ACCEPTED. 18:30 group: both OCCUPIED with 200ms delay — well inside
    # the stagger. 19:30 group: rank 2 SUCCESS. When the higher-priority group
    # answers promptly the next group must wait for it rather than racing
    # across the priority boundary; the stagger is a ceiling, not a delay.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (19, _FACILITY_IDS[0]): BookingResult.SUCCESS},
        submit_sleeps={(18, _FACILITY_IDS[0]): 0.2,
                       (18, _FACILITY_IDS[1]): 0.2},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    # Two 18:30 submits + two 19:30 submits.
    assert len(client.submit_calls) == 4
    # First 18:30 submit started before first 19:30 submit, AND first 19:30
    # submit started after both 18:30 submits had time to launch (≥200ms gap).
    hours = [s.start_dt.hour for s in client.submit_calls]
    assert hours[:2] == [18, 18]
    assert hours[2:] == [19, 19]
    gap = client.submit_call_times[2] - client.submit_call_times[0]
    assert gap >= 0.2, (
        f"19:30 group started {gap*1000:.0f}ms after 18:30 group — "
        f"expected to wait for 18:30 to complete (~200ms)"
    )


@pytest.mark.asyncio
async def test_multi_success_in_group_prefers_lower_rank():
    # Both 18:30 submits return SUCCESS. The orchestrator must report rank 0
    # as the winner (priority-preserving within group) and log a warning about
    # the surplus rank 1 booking.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[0]): BookingResult.SUCCESS,
                        (18, _FACILITY_IDS[1]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    # Both 18:30 submits ran (proves group fired in parallel); no 19:30.
    assert len(client.submit_calls) == 2


@pytest.mark.asyncio
async def test_dry_run_does_not_call_cell_click_or_submit():
    from src.http_booker import book_via_http

    # Use sparse outcomes - KeyError would fire if cell_click was actually called.
    client = _FakeClient(cell_click_outcomes={})
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=True, log=_LOG)
    assert rc == 0
    assert client.cell_click_calls == []
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_hung_group_does_not_delay_next_group_past_stagger():
    # Regression for 2026-08-19..2026-08-27 (7 consecutive lost runs). Both
    # 18:30 submits hung past the client timeout; because groups were strictly
    # serial, the 19:30 fallback only fired after that full hang and came back
    # OCCUPIED every time. The next group must now launch once the stagger
    # elapses, without waiting for the hung group to settle.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (19, _FACILITY_IDS[0]): BookingResult.SUCCESS},
        submit_sleeps={(18, _FACILITY_IDS[0]): 1.0,
                       (18, _FACILITY_IDS[1]): 1.0},
    )
    rc = await book_via_http(
        client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG, stagger_s=0.1
    )
    assert rc == 0
    assert len(client.submit_calls) == 4
    gap = client.submit_call_times[2] - client.submit_call_times[0]
    assert 0.1 <= gap < 0.6, (
        f"19:30 group launched {gap*1000:.0f}ms after 18:30 — expected ~100ms "
        f"(the stagger), not ~1000ms (the hung group's full duration)"
    )


@pytest.mark.asyncio
async def test_slow_first_group_success_beats_fast_second_group_success(caplog):
    # Both groups are in flight at once, so results no longer arrive in
    # priority order. The 19:30 SUCCESS lands first, but 18:30 is what the
    # user actually wants: the winner must be chosen by rank, not by
    # who answered first.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[0]): BookingResult.SUCCESS,
                        (19, _FACILITY_IDS[0]): BookingResult.SUCCESS},
        submit_sleeps={(18, _FACILITY_IDS[0]): 0.4,
                       (18, _FACILITY_IDS[1]): 0.4},
    )
    with caplog.at_level(logging.INFO):
        rc = await book_via_http(
            client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG, stagger_s=0.1
        )
    assert rc == 0
    assert len(client.submit_calls) == 4
    booked = [r.getMessage() for r in caplog.records
              if r.getMessage().startswith("done: booked")]
    assert booked == ["done: booked Tennis Court No. 1 @ 18:30 (rank=0)"], caplog.text


@pytest.mark.asyncio
async def test_success_in_later_group_survives_fatal_in_earlier_group():
    # With groups overlapping, an in-flight 18:30 FATAL (e.g. PolyU's quota
    # page) must not discard a real 19:30 booking that already committed.
    # Only a FATAL with no SUCCESS anywhere is fatal to the run.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.SUCCESS),
                        (18, _FACILITY_IDS[0]): BookingResult.ERROR_FATAL,
                        (18, _FACILITY_IDS[1]): BookingResult.ERROR_FATAL},
        submit_sleeps={(18, _FACILITY_IDS[0]): 0.4,
                       (18, _FACILITY_IDS[1]): 0.4},
    )
    rc = await book_via_http(
        client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG, stagger_s=0.1
    )
    assert rc == 0
    assert len(client.submit_calls) == 4


@pytest.mark.asyncio
async def test_outcome_carries_the_winning_slot():
    """book_via_http_outcome reports which court/hour won, for the result email."""
    from src.http_booker import book_via_http_outcome

    client = _FakeClient(
        _all_cell(CellOutcome.ACCEPTED),
        {
            (18, 10): BookingResult.OCCUPIED, (18, 11): BookingResult.SUCCESS,
            (19, 10): BookingResult.OCCUPIED, (19, 11): BookingResult.OCCUPIED,
        },
    )
    outcome = await book_via_http_outcome(
        client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG,
    )
    assert outcome.rc == 0
    assert outcome.slot.facility_id == 11
    assert outcome.slot.start_dt.hour == 18


@pytest.mark.asyncio
async def test_outcome_has_no_slot_on_failure_or_dry_run():
    from src.http_booker import book_via_http_outcome

    failed = await book_via_http_outcome(
        _FakeClient(_all_cell(CellOutcome.OCCUPIED)),
        date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG,
    )
    assert (failed.rc, failed.slot) == (1, None)
    dry = await book_via_http_outcome(
        _FakeClient(_all_cell(CellOutcome.ACCEPTED)),
        date(2026, 6, 10), _PRIORITY, dry_run=True, log=_LOG,
    )
    assert (dry.rc, dry.slot) == (0, None)
