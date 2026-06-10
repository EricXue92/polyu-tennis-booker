# Parallel cell-click + classification fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix submit-stage OCCUPIED misclassification, capture body diagnostics on unexpected responses, and fire all 4 cell-clicks concurrently while keeping submit strictly priority-ordered.

**Architecture:** Split `try_book` into two coroutines (`cell_click` + `submit`). `book_via_http` runs `asyncio.gather` over cell-clicks, then walks ACCEPTED results in priority order calling `submit` sequentially. Warmup expands to N concurrent GETs to prime per-candidate TLS sockets. Strict priority preserved.

**Tech Stack:** Python 3.12+ asyncio, httpx (async client + pool limits), respx (test mocks), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-06-10-parallel-cell-click-design.md`

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `src/http_client.py` | Modify | New types (`CellOutcome`, `CellClickResult`), `_diag_markers` helper, split `try_book` → `cell_click` + `submit`, A.1 classification fix, A.2 diagnostic logging, `warmup(n)` parameter, pool limits |
| `src/http_booker.py` | Modify | Rewrite `book_via_http` for parallel cell-clicks + priority-ordered submit |
| `src/booker.py` | Modify | Pass `n = len(slots) * len(TENNIS_FACILITIES)` to `client.warmup()` |
| `tests/test_http_client.py` | Modify | Add cell_click + submit + warmup-N + diagnostics tests; delete obsolete try_book tests in final task |
| `tests/test_http_booker.py` | Modify | Rewrite `_FakeClient` for split interface; expand orchestrator matrix |

No new files. No changes to `src/config.py`, `src/dates.py`, `src/log.py`, workflows, or scripts.

---

## Task 1: Add `CellOutcome`, `CellClickResult`, `_diag_markers` helper

**Files:**
- Modify: `src/http_client.py` (add after `BookingResult` enum at line 46)
- Test: `tests/test_http_client.py` (append at end)

These are pure additions — no existing behaviour changes. Sets up the type vocabulary used by Tasks 2 and 3.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_http_client.py`:

```python
def test_cell_outcome_has_four_outcomes():
    from src.http_client import CellOutcome
    assert {
        CellOutcome.ACCEPTED,
        CellOutcome.OCCUPIED,
        CellOutcome.ERROR_TRANSIENT,
        CellOutcome.ERROR_FATAL,
    }
    assert len(list(CellOutcome)) == 4


def test_cell_click_result_is_immutable():
    from src.http_client import AvailableSlot, CellClickResult, CellOutcome
    slot = AvailableSlot(
        facility_id=10, facility_name="X", center_id=1, center_name="Y",
        start_dt=datetime(2026, 6, 10, 18, 30),
        end_dt=datetime(2026, 6, 10, 19, 30),
    )
    cr = CellClickResult(slot=slot, outcome=CellOutcome.ACCEPTED, latency_ms=42)
    import pytest
    with pytest.raises(Exception):
        cr.latency_ms = 99


def test_diag_markers_returns_empty_for_empty_body():
    from src.http_client import _diag_markers
    assert _diag_markers("") == []
    assert _diag_markers(None) == []


def test_diag_markers_extracts_known_substrings_case_insensitive():
    from src.http_client import _diag_markers
    body = "Your Quota was EXCEEDED. Session Expired. Please re-login."
    found = _diag_markers(body)
    assert "quota" in found
    assert "exceeded" in found
    assert "session" in found
    assert "expired" in found


def test_diag_markers_returns_empty_when_no_match():
    from src.http_client import _diag_markers
    assert _diag_markers("plain text with no signals") == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_http_client.py::test_cell_outcome_has_four_outcomes tests/test_http_client.py::test_cell_click_result_is_immutable tests/test_http_client.py::test_diag_markers_returns_empty_for_empty_body tests/test_http_client.py::test_diag_markers_extracts_known_substrings_case_insensitive tests/test_http_client.py::test_diag_markers_returns_empty_when_no_match -v
```

Expected: 5 FAILs with `ImportError` / `AttributeError` on `CellOutcome`, `CellClickResult`, `_diag_markers`.

- [ ] **Step 3: Add the types and helper**

In `src/http_client.py`, immediately after the `BookingResult` class (around line 46), add:

```python
class CellOutcome(enum.Enum):
    """Result of the cell-click POST (first stage of try_book)."""
    ACCEPTED = enum.auto()         # 302 -> make_book_submit.do; slot held server-side
    OCCUPIED = enum.auto()         # slot already taken
    ERROR_TRANSIENT = enum.auto()  # 5xx / network — session probably alive
    ERROR_FATAL = enum.auto()      # 4xx / unknown shape — this candidate unbookable


@dataclass(frozen=True)
class CellClickResult:
    """What cell_click returns. The orchestrator uses `outcome` to decide
    whether to call submit, and logs `latency_ms` for diagnostics."""
    slot: "AvailableSlot"
    outcome: CellOutcome
    latency_ms: int


_DIAG_MARKERS = (
    "occupied", "quota", "exceeded", "logout", "expired",
    "successfully", "session", "denied", "invalid", "error",
)


def _diag_markers(body: str | None) -> list[str]:
    """Return which `_DIAG_MARKERS` substrings appear in body (case-insensitive).

    Used when an unexpected response shape falls through to ERROR_*; the marker
    list gets logged so we can root-cause from CI logs without reproducing the
    failure live (e.g. 2026-06-09's 200+empty-Location response).
    """
    if not body:
        return []
    low = body.lower()
    return [m for m in _DIAG_MARKERS if m in low]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_http_client.py::test_cell_outcome_has_four_outcomes tests/test_http_client.py::test_cell_click_result_is_immutable tests/test_http_client.py::test_diag_markers_returns_empty_for_empty_body tests/test_http_client.py::test_diag_markers_extracts_known_substrings_case_insensitive tests/test_http_client.py::test_diag_markers_returns_empty_when_no_match -v
```

Expected: 5 PASS.

- [ ] **Step 5: Run full test suite to confirm no regression**

```bash
uv run pytest
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/http_client.py tests/test_http_client.py
git commit -m "feat(http_client): add CellOutcome, CellClickResult, _diag_markers helper

Type vocabulary for the upcoming try_book split.
_diag_markers will log body fingerprints on unexpected responses."
```

---

## Task 2: Add `cell_click` method on `PolyUHttpClient`

**Files:**
- Modify: `src/http_client.py` (add new method in `PolyUHttpClient` class; do NOT touch existing `try_book` yet)
- Test: `tests/test_http_client.py` (append)

Extract the cell-click POST + classification logic into a standalone coroutine. Apply A.1 case-insensitive `"occupied"` matching. Track latency. Log body diagnostics on ERROR_*.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_http_client.py`:

```python
@pytest.mark.asyncio
@respx.mock
async def test_cell_click_returns_accepted_on_redirect_to_submit():
    from src.http_client import PolyUHttpClient, CellOutcome

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(
        302,
        headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"},
    ))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        cr = await client.cell_click(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert cr.outcome is CellOutcome.ACCEPTED
    assert cr.slot.facility_id == 11
    assert cr.latency_ms >= 0


@pytest.mark.asyncio
@respx.mock
async def test_cell_click_returns_occupied_on_redirect_back_to_make_book():
    # Slot grabbed by another user — PolyU bounces us back to the listing.
    from src.http_client import PolyUHttpClient, CellOutcome

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(
        302,
        headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"},
    ))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        cr = await client.cell_click(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert cr.outcome is CellOutcome.OCCUPIED


@pytest.mark.asyncio
@respx.mock
async def test_cell_click_returns_occupied_on_lowercase_occupied_body():
    # A.1 consistency: case-insensitive substring, broader than the literal
    # "Facility is occupied" string we previously matched.
    from src.http_client import PolyUHttpClient, CellOutcome

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(
        200, text="<html><body>this slot is OCCUPIED already</body></html>",
    ))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        cr = await client.cell_click(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert cr.outcome is CellOutcome.OCCUPIED


@pytest.mark.asyncio
@respx.mock
async def test_cell_click_returns_transient_on_5xx():
    from src.http_client import PolyUHttpClient, CellOutcome

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(503, text="Service Unavailable"))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        cr = await client.cell_click(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert cr.outcome is CellOutcome.ERROR_TRANSIENT


@pytest.mark.asyncio
@respx.mock
async def test_cell_click_returns_fatal_on_4xx():
    from src.http_client import PolyUHttpClient, CellOutcome

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(403, text="Forbidden"))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        cr = await client.cell_click(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert cr.outcome is CellOutcome.ERROR_FATAL


@pytest.mark.asyncio
@respx.mock
async def test_cell_click_returns_transient_on_network_error():
    import httpx
    from src.http_client import PolyUHttpClient, CellOutcome

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(side_effect=httpx.ConnectError("boom"))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        cr = await client.cell_click(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert cr.outcome is CellOutcome.ERROR_TRANSIENT


@pytest.mark.asyncio
@respx.mock
async def test_cell_click_logs_diagnostics_on_error_fatal(caplog):
    # Unknown shape (200 + empty Location + no marker words) -> FATAL + diag log.
    from src.http_client import PolyUHttpClient, CellOutcome
    import logging

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(200, text="<html>nothing recognisable</html>"))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        with caplog.at_level(logging.WARNING, logger="booker"):
            cr = await client.cell_click(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert cr.outcome is CellOutcome.ERROR_FATAL
    rec = [r for r in caplog.records if "cell_click unexpected" in r.message]
    assert rec, "expected a cell_click unexpected WARNING with diagnostics"
    msg = rec[0].message
    assert "body_len=" in msg
    assert "preview=" in msg
    assert "markers=" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_http_client.py -k cell_click -v
```

Expected: 7 FAILs with `AttributeError: 'PolyUHttpClient' object has no attribute 'cell_click'`.

- [ ] **Step 3: Add `cell_click` method on `PolyUHttpClient`**

In `src/http_client.py`, inside the `PolyUHttpClient` class, add this method (place it BEFORE the existing `try_book`):

```python
    async def cell_click(self, slot: AvailableSlot) -> CellClickResult:
        """POST make_book.do for one (facility, time) candidate.

        Returns CellClickResult with one of:
          ACCEPTED        — 302 -> make_book_submit.do; slot is server-side held,
                            caller can call submit() to commit.
          OCCUPIED        — 200/302 with "occupied" body OR Location bouncing back
                            to make_book*; slot was taken between our search and
                            this POST.
          ERROR_TRANSIENT — 5xx, network/timeout; safe to advance to the next
                            candidate, our session is probably still alive.
          ERROR_FATAL     — 4xx or unrecognised shape; this candidate is unbookable
                            (auth, schema, or quota).

        On ERROR_*, logs body diagnostics so 2026-06-09-style anomalies are
        root-causeable from CI logs alone.
        """
        from src.config import (
            MAKE_BOOK_URL,
            TENNIS_ACTV_ID,
            TENNIS_DATA_SET_ID,
        )
        import time as _time
        date_str = slot.start_dt.strftime("%d/%m/%Y")
        inner_date = quote(date_str, safe="")
        search_form_str = (
            f"fbUserId={self.fb_user_id}&bookType=INDV"
            f"&dataSetId={TENNIS_DATA_SET_ID}&actvId={TENNIS_ACTV_ID}"
            f"&searchDate={inner_date}&ctrId={slot.center_id}&facilityId="
        )
        cell_form = {
            "brcdNo": "",
            "phone": "",
            "extlPtyDclrId": "",
            "dataSetId": str(TENNIS_DATA_SET_ID),
            "actvId": str(TENNIS_ACTV_ID),
            "onBehalfOfFbUserId": "",
            "byPassQuota": "false",
            "byPassChrgSchm": "false",
            "byPassBookingDaysLimit": "false",
            "repeatOccurrence": "false",
            "grpFacilityIds": "",
            "searchFormString": search_form_str,
            "boMakeBookFacilities[0].ctrId": str(slot.center_id),
            "boMakeBookFacilities[0].facilityId": str(slot.facility_id),
            "boMakeBookFacilities[0].startDateTime": _fmt_polyu_dt(slot.start_dt),
            "boMakeBookFacilities[0].endDateTime": _fmt_polyu_dt(slot.end_dt),
            "CSRFToken": self.csrf_token,
        }

        t0 = _time.perf_counter()
        try:
            resp = await self._http.post(
                MAKE_BOOK_URL,
                data=cell_form,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Origin": _ORIGIN,
                    "Referer": _REFERER_MAKE_BOOK,
                    "Upgrade-Insecure-Requests": "1",
                },
            )
        except httpx.HTTPError as exc:
            _LOG.warning("cell_click transport error: %r", exc)
            return CellClickResult(
                slot=slot,
                outcome=CellOutcome.ERROR_TRANSIENT,
                latency_ms=int((_time.perf_counter() - t0) * 1000),
            )

        latency_ms = int((_time.perf_counter() - t0) * 1000)
        location = resp.headers.get("location", "")
        body = resp.text or ""

        if resp.status_code == 302 and "make_book_submit" in location:
            outcome = CellOutcome.ACCEPTED
        elif resp.status_code in (200, 302) and (
            "occupied" in body.lower() or "make_book" in location
        ):
            outcome = CellOutcome.OCCUPIED
        else:
            outcome = _classify_http_error(resp.status_code)  # ERROR_TRANSIENT or ERROR_FATAL
            # _classify_http_error returns BookingResult.ERROR_*; translate.
            outcome = (
                CellOutcome.ERROR_TRANSIENT
                if outcome is BookingResult.ERROR_TRANSIENT
                else CellOutcome.ERROR_FATAL
            )
            _LOG.warning(
                "cell_click unexpected (status=%d, location=%r, body_len=%d, "
                "preview=%r, markers=%s) -> %s",
                resp.status_code, location, len(body),
                " ".join(body[:300].split()),
                _diag_markers(body),
                outcome.name,
            )
        return CellClickResult(slot=slot, outcome=outcome, latency_ms=latency_ms)
```

`MAKE_BOOK_URL` and constants are imported lazily inside the method (mirroring the existing `try_book` style). `_ORIGIN`, `_REFERER_MAKE_BOOK`, `_fmt_polyu_dt`, `_classify_http_error`, `quote`, `httpx`, and `_LOG` are all already in module scope.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_http_client.py -k cell_click -v
```

Expected: 7 PASS.

- [ ] **Step 5: Run full test suite**

```bash
uv run pytest
```

Expected: all tests PASS (existing `try_book` tests still work — we haven't touched it).

- [ ] **Step 6: Commit**

```bash
git add src/http_client.py tests/test_http_client.py
git commit -m "feat(http_client): add cell_click method with diagnostics

Standalone first-stage POST. Classifies the response into
CellOutcome.{ACCEPTED, OCCUPIED, ERROR_TRANSIENT, ERROR_FATAL}.
Uses case-insensitive 'occupied' substring match (A.1).
Logs body fingerprint on ERROR_* for offline root-cause (A.2).
try_book still in place; orchestrator switchover comes later."
```

---

## Task 3: Add `submit` method with A.1 fix + A.2 diagnostics

**Files:**
- Modify: `src/http_client.py` (add new method in `PolyUHttpClient`; do NOT touch `try_book`)
- Test: `tests/test_http_client.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_http_client.py`:

```python
@pytest.mark.asyncio
@respx.mock
async def test_submit_returns_success_on_make_book_result_redirect():
    from src.http_client import PolyUHttpClient, BookingResult

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
    ).mock(return_value=Response(
        302,
        headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_result.do"},
    ))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        result = await client.submit(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert result is BookingResult.SUCCESS


@pytest.mark.asyncio
@respx.mock
async def test_submit_returns_occupied_on_redirect_to_make_book_do():
    # Regression for 2026-06-07: submit returned 302 -> make_book.do (NOT
    # _submit). Old code mis-classified as ERROR_FATAL; new code treats it
    # as OCCUPIED so the orchestrator advances to the next candidate.
    from src.http_client import PolyUHttpClient, BookingResult

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
    ).mock(return_value=Response(
        302,
        headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"},
    ))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        result = await client.submit(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert result is BookingResult.OCCUPIED


@pytest.mark.asyncio
@respx.mock
async def test_submit_returns_occupied_on_redirect_back_to_submit():
    # Existing behaviour preserved.
    from src.http_client import PolyUHttpClient, BookingResult

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
    ).mock(return_value=Response(
        302,
        headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"},
    ))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        result = await client.submit(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert result is BookingResult.OCCUPIED


@pytest.mark.asyncio
@respx.mock
async def test_submit_returns_occupied_on_lowercase_body():
    from src.http_client import PolyUHttpClient, BookingResult

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
    ).mock(return_value=Response(200, text="<html>OCCUPIED!!!</html>"))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        result = await client.submit(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert result is BookingResult.OCCUPIED


@pytest.mark.asyncio
@respx.mock
async def test_submit_returns_transient_on_5xx():
    from src.http_client import PolyUHttpClient, BookingResult

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
    ).mock(return_value=Response(503, text="Service Unavailable"))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        result = await client.submit(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert result is BookingResult.ERROR_TRANSIENT


@pytest.mark.asyncio
@respx.mock
async def test_submit_returns_fatal_on_4xx():
    from src.http_client import PolyUHttpClient, BookingResult

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
    ).mock(return_value=Response(403, text="Forbidden"))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        result = await client.submit(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert result is BookingResult.ERROR_FATAL


@pytest.mark.asyncio
@respx.mock
async def test_submit_returns_fatal_on_unknown_shape_with_diagnostics(caplog):
    # Regression for 2026-06-09: 200 + empty Location + body without known
    # marker words. Stays FATAL, but now logs body_len/preview/markers.
    from src.http_client import PolyUHttpClient, BookingResult
    import logging

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
    ).mock(return_value=Response(200, text="<html>weird page no banner</html>"))
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        with caplog.at_level(logging.WARNING, logger="booker"):
            result = await client.submit(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert result is BookingResult.ERROR_FATAL
    rec = [r for r in caplog.records if "submit unexpected" in r.message]
    assert rec, "expected a submit unexpected WARNING with diagnostics"
    msg = rec[0].message
    assert "body_len=" in msg
    assert "preview=" in msg
    assert "markers=" in msg


@pytest.mark.asyncio
@respx.mock
async def test_submit_sends_multipart_with_csrf_and_declare():
    from src.http_client import PolyUHttpClient

    captured = {}
    def record(request):
        captured["body"] = request.content.decode("utf-8", errors="replace")
        captured["ct"] = request.headers.get("content-type", "")
        return Response(302, headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_result.do"})

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
    ).mock(side_effect=record)

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="tok-S", fb_user_id="432567")
    try:
        await client.submit(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert "multipart/form-data" in captured["ct"]
    assert "tok-S" in captured["body"]
    assert 'name="declare"' in captured["body"]
    assert 'name="CSRFToken"' in captured["body"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_http_client.py -k "submit and not search and not try_book" -v
```

Expected: 8 FAILs with `AttributeError: 'PolyUHttpClient' object has no attribute 'submit'`.

- [ ] **Step 3: Add `submit` method**

In `src/http_client.py`, inside the `PolyUHttpClient` class (place after `cell_click`), add:

```python
    async def submit(self, slot: AvailableSlot) -> BookingResult:
        """POST make_book_submit.do to commit a slot whose cell_click was ACCEPTED.

        Returns:
          SUCCESS         — 302 -> make_book_result.do. Booking confirmed.
          OCCUPIED        — 302 back to make_book* OR body contains 'occupied'.
                            Slot was grabbed between our cell_click and this POST,
                            or PolyU's quota check rejected us.
          ERROR_TRANSIENT — 5xx, network/timeout.
          ERROR_FATAL     — 4xx or unrecognised shape; orchestrator should stop
                            trying further submits (auth is presumed dead).

        A.1 fix: OCCUPIED detection now matches 'make_book' broadly (covering
        both make_book.do and make_book_submit.do redirect targets) and uses a
        case-insensitive 'occupied' substring for body content. The previous
        narrow detector misclassified 2026-06-07's response as ERROR_FATAL.
        """
        from src.config import (
            MAKE_BOOK_SUBMIT_URL,
            TENNIS_ACTV_ID,
            TENNIS_DATA_SET_ID,
        )
        date_str = slot.start_dt.strftime("%d/%m/%Y")
        inner_date = quote(date_str, safe="")
        search_form_str = (
            f"fbUserId={self.fb_user_id}&bookType=INDV"
            f"&dataSetId={TENNIS_DATA_SET_ID}&actvId={TENNIS_ACTV_ID}"
            f"&searchDate={inner_date}&ctrId={slot.center_id}&facilityId="
        )
        submit_fields = {
            "dataSetId": str(TENNIS_DATA_SET_ID),
            "boBookingType.id": "1",
            "boBookingType.value": "INDV",
            "boBookingMode.value": "SPORT",
            "boBookingMode.id": "1",
            "userRefNum": "",
            "fbUserId": self.fb_user_id,
            "grpFacilityIds": "",
            "repeatOccurrence": "false",
            "startDate": "",
            "startTime": "",
            "endDate": "",
            "endTime": "",
            "dayOfWeeks": "",
            "functionsAvailable": "false",
            "brcdNo": "",
            "phone": "",
            "onBehalfOfFbUserId": "",
            "byPassQuota": "false",
            "byPassChrgSchm": "false",
            "byPassBookingDaysLimit": "false",
            "searchFormString": search_form_str,
            "extlPtyDclrId": "",
            "boMakeBookFacilities[0].ctrId": str(slot.center_id),
            "boMakeBookFacilities[0].centerName": slot.center_name,
            "boMakeBookFacilities[0].facilityId": str(slot.facility_id),
            "boMakeBookFacilities[0].facilityName": slot.facility_name,
            "boMakeBookFacilities[0].startDateTime": _fmt_polyu_dt(slot.start_dt),
            "boMakeBookFacilities[0].endDateTime": _fmt_polyu_dt(slot.end_dt),
            "declare": "on",
            "CSRFToken": self.csrf_token,
        }
        multipart_files = {name: (None, value) for name, value in submit_fields.items()}
        try:
            resp = await self._http.post(
                MAKE_BOOK_SUBMIT_URL,
                files=multipart_files,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Origin": _ORIGIN,
                    "Referer": _REFERER_MAKE_BOOK_SUBMIT,
                    "Upgrade-Insecure-Requests": "1",
                },
            )
        except httpx.HTTPError as exc:
            _LOG.warning("submit transport error: %r", exc)
            return BookingResult.ERROR_TRANSIENT

        location = resp.headers.get("location", "")
        body = resp.text or ""

        if resp.status_code == 302 and "make_book_result" in location:
            return BookingResult.SUCCESS
        if resp.status_code in (200, 302) and (
            "occupied" in body.lower() or "make_book" in location
        ):
            return BookingResult.OCCUPIED
        err = _classify_http_error(resp.status_code)
        _LOG.warning(
            "submit unexpected (status=%d, location=%r, body_len=%d, "
            "preview=%r, markers=%s) -> %s",
            resp.status_code, location, len(body),
            " ".join(body[:300].split()),
            _diag_markers(body),
            err.name,
        )
        return err
```

- [ ] **Step 4: Run new tests to verify they pass**

```bash
uv run pytest tests/test_http_client.py -k "submit and not search and not try_book" -v
```

Expected: 8 PASS.

- [ ] **Step 5: Run full test suite**

```bash
uv run pytest
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/http_client.py tests/test_http_client.py
git commit -m "feat(http_client): add submit method with classification fix + diagnostics

Splits the second-stage POST out of try_book. A.1 fix: case-insensitive
'occupied' + broader 'make_book' Location match covers 2026-06-07's
302 -> make_book.do redirect that previously aborted the run.
A.2 diagnostics: body length + preview + marker substrings logged on
ERROR_* so 2026-06-09-style anomalies are root-causeable from CI logs."
```

---

## Task 4: Extend `warmup(n)` to fire N concurrent GETs

**Files:**
- Modify: `src/http_client.py:164-187` (existing `warmup` method)
- Test: `tests/test_http_client.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_http_client.py`:

```python
@pytest.mark.asyncio
@respx.mock
async def test_warmup_fires_n_concurrent_gets():
    from src.http_client import PolyUHttpClient

    route = respx.get(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(200, text="<html>ok</html>"))

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        results = await client.warmup(n=4)
    finally:
        await client.aclose()
    assert results == [200, 200, 200, 200]
    assert route.call_count == 4


@pytest.mark.asyncio
@respx.mock
async def test_warmup_default_n_is_1():
    # Back-compat: warmup() with no arg returns a list of length 1.
    from src.http_client import PolyUHttpClient

    route = respx.get(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(200, text="<html>ok</html>"))

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        results = await client.warmup()
    finally:
        await client.aclose()
    assert results == [200]
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_warmup_returns_negative_one_per_failed_get():
    import httpx
    from src.http_client import PolyUHttpClient

    respx.get(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(side_effect=httpx.ConnectError("boom"))

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        results = await client.warmup(n=4)
    finally:
        await client.aclose()
    assert results == [-1, -1, -1, -1]
```

Also delete the now-obsolete singular tests (their behaviour is fully covered by the new list-returning tests):

```bash
# Note: do this in Step 3 alongside the implementation change.
```

- [ ] **Step 2: Run new tests to verify they fail**

```bash
uv run pytest tests/test_http_client.py -k warmup -v
```

Expected: the 3 new tests FAIL (return-type mismatch: `int` vs `list[int]`), the 2 OLD tests (`test_warmup_sends_get_to_make_book_and_returns_status`, `test_warmup_returns_negative_one_on_transport_error`) currently PASS.

- [ ] **Step 3: Replace `warmup` with concurrent-N version**

In `src/http_client.py`, replace the existing `warmup` method (around L164-187) with:

```python
    async def warmup(self, n: int = 1) -> list[int]:
        """Open n warm TLS connections in the pool by firing n concurrent GETs.

        The booking flow sleeps ~50s between bootstrap and the 08:30 trigger.
        Servers typically drop idle keepalive connections in 15-30s, so the
        first POST at 08:30:00.000 pays a full TCP+TLS handshake (~5s observed
        on 2026-06-05). With N concurrent POSTs (one per candidate), we need N
        warm connections in the pool — a single warm connection means only the
        first POST is fast, others cold-handshake.

        Each httpx GET that overlaps in time forces a new connection because
        none has yet returned to the pool. After all return, the pool holds n
        hot keepalive sockets. The subsequent POST burst reuses them all.

        Best-effort: each entry in the returned list is an HTTP status code,
        or -1 on transport error. Never raises — a failed warmup must not
        prevent the real booking.
        """
        from src.config import MAKE_BOOK_URL
        import asyncio

        async def _one() -> int:
            try:
                resp = await self._http.get(
                    MAKE_BOOK_URL,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": _REFERER_MAKE_BOOK,
                    },
                )
                return resp.status_code
            except httpx.HTTPError:
                return -1
        return await asyncio.gather(*(_one() for _ in range(n)))
```

Then DELETE the two now-obsolete tests from `tests/test_http_client.py`:
- `test_warmup_sends_get_to_make_book_and_returns_status`
- `test_warmup_returns_negative_one_on_transport_error`

(Their assertions — single int return — are no longer valid; the replacements above cover the same behaviour.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_http_client.py -k warmup -v
```

Expected: 3 PASS, 0 obsolete (deleted).

- [ ] **Step 5: Run full test suite (booker.py callers of `warmup` still pass single int)**

```bash
uv run pytest
```

Expected: all tests PASS. Note that `src/booker.py:183` calls `await client.warmup()` and logs `status=%d`. With the new signature, `warmup()` returns `[200]` (list of one int). The line `log.info("warmup complete (status=%d)", status)` would now log `status=[200]` via `%d` against a list → **TypeError at runtime**.

The booker.py is not under test directly (no `test_booker.py` exercising `run()`), so the test suite won't catch this. We fix it in Task 6. For now the test suite is green.

- [ ] **Step 6: Commit**

```bash
git add src/http_client.py tests/test_http_client.py
git commit -m "feat(http_client): warmup(n) opens N concurrent TLS connections

A single warm connection only helps the first POST; with N concurrent
cell-clicks at 08:30:00 the remaining N-1 still cold-handshake.
warmup(n) fires n concurrent GETs so all N stay hot.
booker.py callsite updated in a follow-up task."
```

---

## Task 5: Add connection-pool limits to `PolyUHttpClient.__init__`

**Files:**
- Modify: `src/http_client.py:153-159` (existing `httpx.AsyncClient(...)` construction)
- Test: `tests/test_http_client.py` (append)

Small explicit-limits addition. Documents intent and survives candidate-set changes.

- [ ] **Step 1: Write failing test**

Append to `tests/test_http_client.py`:

```python
@pytest.mark.asyncio
async def test_client_sets_connection_pool_limits():
    from src.http_client import PolyUHttpClient
    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        # httpx.Limits is stored on the transport pool; access via the private
        # _pool attribute. Brittle vs httpx internals but worth the lock-in.
        pool = client._http._transport._pool
        assert pool._max_connections == 8
        assert pool._max_keepalive_connections == 8
    finally:
        await client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_http_client.py::test_client_sets_connection_pool_limits -v
```

Expected: FAIL (default httpx limits aren't 8).

- [ ] **Step 3: Add `limits=httpx.Limits(...)` to the AsyncClient construction**

In `src/http_client.py`, modify the `PolyUHttpClient.__init__` body. Replace the existing `httpx.AsyncClient(...)` block (around L154-159) with:

```python
        self._http = httpx.AsyncClient(
            cookies=cookies,
            headers=_DEFAULT_HEADERS,
            timeout=timeout,
            follow_redirects=False,  # We need to inspect 302 Location ourselves.
            limits=httpx.Limits(
                max_connections=8,
                max_keepalive_connections=8,
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_http_client.py::test_client_sets_connection_pool_limits -v
```

Expected: PASS.

- [ ] **Step 5: Run full test suite**

```bash
uv run pytest
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/http_client.py tests/test_http_client.py
git commit -m "feat(http_client): set explicit connection-pool limits to 8

Documents intent: 4 candidates today, headroom up to 8 with no surprises.
Future candidate-set expansion will be a deliberate audit point."
```

---

## Task 6: Wire `warmup(n=N)` and rewrite log line in `src/booker.py`

**Files:**
- Modify: `src/booker.py:183-184` (warmup call + log line)
- Test: none (booker.py is integration glue without direct unit tests)

This makes the full test suite + the live booker consistent with the new `warmup` signature.

- [ ] **Step 1: Update `booker.py:run` to call `warmup(n=N)` and log the list**

In `src/booker.py`, find the block:

```python
            log.info("warming up HTTP connection")
            status = await client.warmup()
            log.info("warmup complete (status=%d)", status)
```

Replace it with:

```python
            from src.config import TENNIS_FACILITIES
            n_candidates = len(slots) * len(TENNIS_FACILITIES)
            log.info("warming up %d HTTP connections", n_candidates)
            statuses = await client.warmup(n=n_candidates)
            log.info("warmup complete (statuses=%s)", statuses)
```

Rationale for placement: `slots` is already available at L134; `TENNIS_FACILITIES` is in `src.config`. The import sits inside the function to match the lazy-import style used elsewhere in this module.

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest
```

Expected: all PASS (no booker-level unit tests; this change is exercised by manual dry-run in Step 4).

- [ ] **Step 3: Manual dry-run sanity check**

Requires `POLYU_USERNAME` / `POLYU_PASSWORD` in the environment. Skip this step if those secrets aren't available locally.

```bash
POLYU_USERNAME=... POLYU_PASSWORD=... uv run book-tennis --dry-run --skip-sleep
```

Expected log line:
```
warming up 4 HTTP connections
warmup complete (statuses=[200, 200, 200, 200])
```

(Numbers may vary if PolyU returns 302 or any other valid status. The key is `statuses=[...]` is a list of 4 ints, not a single int and not a crash.)

- [ ] **Step 4: Commit**

```bash
git add src/booker.py
git commit -m "feat(booker): warm up N TLS connections matching candidate count

slots × TENNIS_FACILITIES candidates fire concurrently at 08:30:00;
each needs its own warm socket. Single-connection warmup was the
likely root cause of the 5-6s first-POST latency after 2026-06-05."
```

---

## Task 7: Rewrite `book_via_http` for parallel cell-clicks + priority-ordered submit

**Files:**
- Modify: `src/http_booker.py` (full rewrite)
- Modify: `tests/test_http_booker.py` (full rewrite)

The heart of B1. The `_ClientLike` Protocol changes from `try_book` to `cell_click` + `submit`. The orchestrator runs `asyncio.gather` over cell-clicks then iterates ACCEPTED results in priority order, calling submit sequentially.

- [ ] **Step 1: Write failing tests by rewriting `tests/test_http_booker.py`**

Replace the entire contents of `tests/test_http_booker.py` with:

```python
"""Offline tests for the parallel-cell-click book_via_http orchestrator.

book_via_http:
  1. Constructs candidates from (slots × TENNIS_FACILITIES), priority-major.
  2. Fires all cell_click POSTs concurrently via asyncio.gather.
  3. Walks results in priority order; for each ACCEPTED, calls submit.
     - SUCCESS         -> return 0
     - OCCUPIED        -> advance to next ACCEPTED
     - ERROR_TRANSIENT -> advance to next ACCEPTED
     - ERROR_FATAL     -> abort remaining submits, return 1
  4. If 0 ACCEPTED, return 1 (no submit calls).
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
    """
    def __init__(
        self,
        cell_click_outcomes: dict[tuple[int, int], CellOutcome],
        submit_results: dict[tuple[int, int], BookingResult] | None = None,
        cell_click_sleep_s: float = 0.0,
    ):
        self._cell = cell_click_outcomes
        self._sub = submit_results or {}
        self._sleep = cell_click_sleep_s
        self.cell_click_calls = []
        self.submit_calls = []

    async def cell_click(self, slot):
        self.cell_click_calls.append(slot)
        if self._sleep:
            await asyncio.sleep(self._sleep)
        key = (slot.start_dt.hour, slot.facility_id)
        outcome = self._cell[key]
        return CellClickResult(slot=slot, outcome=outcome, latency_ms=10)

    async def submit(self, slot):
        self.submit_calls.append(slot)
        key = (slot.start_dt.hour, slot.facility_id)
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
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[0]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.cell_click_calls) == 4
    assert len(client.submit_calls) == 1
    assert (client.submit_calls[0].start_dt.hour, client.submit_calls[0].facility_id) == (18, _FACILITY_IDS[0])


@pytest.mark.asyncio
async def test_priority_preserved_when_only_some_accepted():
    # Rank 0 (18:30 court A) and rank 2 (19:30 court A) ACCEPTED;
    # rank 1, 3 OCCUPIED. Submit must hit rank 0, not rank 2.
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
async def test_fallback_to_rank1_after_rank0_submit_occupied():
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[1]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.submit_calls) == 2
    assert client.submit_calls[0].facility_id == _FACILITY_IDS[0]  # rank 0
    assert client.submit_calls[1].facility_id == _FACILITY_IDS[1]  # rank 1


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
    assert len(client.submit_calls) == 1
    assert client.submit_calls[0].start_dt.hour == 18


@pytest.mark.asyncio
async def test_cell_fatal_does_not_abort_globally():
    # rank 0 cell FATAL; rank 1 ACCEPTED → SUCCESS. Cell FATAL is per-candidate.
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
async def test_submit_fatal_aborts_remaining_submits():
    # All ACCEPTED, but rank 0 submit returns FATAL. Don't try rank 1/2/3.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.SUCCESS),
                        (18, _FACILITY_IDS[0]): BookingResult.ERROR_FATAL},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 1
    assert len(client.submit_calls) == 1


@pytest.mark.asyncio
async def test_submit_transient_continues_to_next_rank():
    # rank 0 submit TRANSIENT, rank 1 submit SUCCESS.
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
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 1
    assert len(client.submit_calls) == 0


@pytest.mark.asyncio
async def test_cell_clicks_actually_run_in_parallel():
    # Each cell_click sleeps 500ms. If serial, total > 2.0s; if parallel, < 1.2s.
    # Use 1.2s threshold to absorb CI scheduler jitter.
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
async def test_dry_run_does_not_call_cell_click_or_submit():
    from src.http_booker import book_via_http

    # Use sparse outcomes — KeyError would fire if cell_click was actually called.
    client = _FakeClient(cell_click_outcomes={})
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=True, log=_LOG)
    assert rc == 0
    assert client.cell_click_calls == []
    assert client.submit_calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_http_booker.py -v
```

Expected: all 12 tests FAIL. Old `book_via_http` calls `client.try_book(slot)`, which `_FakeClient` doesn't implement; the old behaviour also doesn't match the new test expectations (serial vs parallel, exit codes for partial cell-click failure, etc.).

- [ ] **Step 3: Rewrite `src/http_booker.py`**

Replace the entire file with:

```python
"""HTTP-based booking orchestrator (parallel cell-clicks).

Phase 1: fire all (priority × facility) cell_click POSTs concurrently via
asyncio.gather. They finish in roughly first-POST latency, not stacked, so
the "first-POST 5-6s cold tax" no longer cascades onto candidates 2-4.

Phase 2: walk cell_click results in priority order. For each ACCEPTED slot,
call submit serially. SUCCESS returns 0 immediately. OCCUPIED or
ERROR_TRANSIENT advances to the next ACCEPTED candidate. ERROR_FATAL aborts
remaining submits (auth presumed dead — further submits will hit the same
wall).

Strict priority guarantee: submit always runs sequentially in priority order.
It is impossible to book rank 3 when rank 0 also succeeded.

Cell-click ERROR_TRANSIENT and ERROR_FATAL are tolerated at the candidate
level — a facility-specific failure does not poison sibling candidates.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time
from typing import Protocol

from src.http_client import (
    AvailableSlot,
    BookingResult,
    CellClickResult,
    CellOutcome,
)


class _ClientLike(Protocol):
    """Minimal subset of PolyUHttpClient used by the orchestrator."""
    async def cell_click(self, slot: AvailableSlot) -> CellClickResult: ...
    async def submit(self, slot: AvailableSlot) -> BookingResult: ...


def _build_candidates(
    target_date: date,
    slots: list[tuple[time, time]],
) -> list[AvailableSlot]:
    from src.config import TENNIS_CENTER_NAME, TENNIS_CTR_ID, TENNIS_FACILITIES

    candidates: list[AvailableSlot] = []
    for start, end in slots:
        start_dt = datetime.combine(target_date, start)
        end_dt = datetime.combine(target_date, end)
        for fid, fname in TENNIS_FACILITIES.items():
            candidates.append(AvailableSlot(
                facility_id=fid,
                facility_name=fname,
                center_id=TENNIS_CTR_ID,
                center_name=TENNIS_CENTER_NAME,
                start_dt=start_dt,
                end_dt=end_dt,
            ))
    return candidates


async def book_via_http(
    client: _ClientLike,
    target_date: date,
    slots: list[tuple[time, time]],
    dry_run: bool,
    *,
    log: logging.Logger,
) -> int:
    """Run the parallel cell-click + priority-ordered submit flow.

    Returns 0 on SUCCESS, 1 otherwise.
    """
    candidates = _build_candidates(target_date, slots)
    log.info("predictive booking: %d candidates queued", len(candidates))

    if dry_run:
        log.info("DRY RUN: stopping before cell_click phase")
        return 0

    # Phase 1: parallel cell-clicks.
    # return_exceptions=False — cell_click catches httpx.HTTPError internally
    # and returns ERROR_TRANSIENT. Any uncaught exception is a real bug and
    # should crash the run with a traceback in CI, not be silently swallowed.
    cell_results: list[CellClickResult] = await asyncio.gather(
        *(client.cell_click(slot) for slot in candidates)
    )

    for rank, cr in enumerate(cell_results):
        log.info(
            "rank=%d %s: cell=%s (latency=%dms)",
            rank, cr.slot.facility_name, cr.outcome.name, cr.latency_ms,
        )

    accepted = [
        (rank, cr.slot)
        for rank, cr in enumerate(cell_results)
        if cr.outcome is CellOutcome.ACCEPTED
    ]
    if not accepted:
        log.warning(
            "no cell_click ACCEPTED; exiting 1 (results: %s)",
            [cr.outcome.name for cr in cell_results],
        )
        return 1

    # Phase 2: sequential submit, strict priority order.
    for rank, slot in accepted:
        result = await client.submit(slot)
        log.info("submit rank=%d %s: %s", rank, slot.facility_name, result.name)
        if result is BookingResult.SUCCESS:
            log.info(
                "done: booked %s @ %s (rank=%d)",
                slot.facility_name, slot.start_dt.strftime("%H:%M"), rank,
            )
            return 0
        if result is BookingResult.ERROR_FATAL:
            log.error("submit ERROR_FATAL; aborting remaining submits (auth presumed dead)")
            return 1
        # OCCUPIED or ERROR_TRANSIENT → try next ACCEPTED candidate.

    log.warning("no submit succeeded among %d ACCEPTED candidates; exiting 1", len(accepted))
    return 1
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_http_booker.py -v
```

Expected: 12 PASS. The parallelism test (`test_cell_clicks_actually_run_in_parallel`) is the load-bearing one — if it fails with "took 2.0s", the implementation accidentally serialised.

- [ ] **Step 5: Run full test suite**

```bash
uv run pytest
```

Expected: all PASS. Note `tests/test_http_client.py` still has `try_book` tests; those still pass because `try_book` still exists (deleted in Task 8).

- [ ] **Step 6: Commit**

```bash
git add src/http_booker.py tests/test_http_booker.py
git commit -m "feat(http_booker): parallel cell-clicks + priority-ordered submit

Phase 1 fires all candidates' cell_click via asyncio.gather; phase 2
walks ACCEPTED slots in priority order calling submit sequentially.
Strict priority preserved: SUCCESS on rank 0 never gets beaten by rank 3.
Cell-click ERROR_FATAL no longer aborts globally (per-candidate semantics).
Submit ERROR_FATAL still aborts remaining submits."
```

---

## Task 8: Delete obsolete `try_book` method and its tests

**Files:**
- Modify: `src/http_client.py` (delete `try_book` method, around L258-405 — the method body, not the dataclass/enum/helper above it)
- Modify: `tests/test_http_client.py` (delete try_book-specific tests, listed below)

`try_book` has no callers after Task 7. Its tests overlap with the new cell_click + submit tests added in Tasks 2 and 3.

- [ ] **Step 1: Verify try_book has no callers**

```bash
rg "try_book" src/ tests/
```

Expected: only references in `src/http_client.py` (the method itself) and `tests/test_http_client.py` (the to-be-deleted tests). No references in `src/http_booker.py`, `src/booker.py`, or anywhere else.

- [ ] **Step 2: Delete `try_book` method from `src/http_client.py`**

Remove the entire `async def try_book(...)` method body (currently L258-405). Leave the `cell_click`, `submit`, `search`, and `warmup` methods intact.

- [ ] **Step 3: Delete obsolete try_book tests from `tests/test_http_client.py`**

Delete these test functions (they're fully superseded by the new `cell_click` and `submit` tests):

- `test_try_book_happy_path_returns_success`
- `test_try_book_occupied_when_submit_redirects_back`
- `test_try_book_occupied_when_cell_click_rejected`
- `test_try_book_transient_error_on_network_failure`
- `test_try_book_sends_correct_cell_click_body`
- `test_try_book_transient_error_on_5xx_cell_click`
- `test_try_book_fatal_error_on_unexpected_4xx_cell_click`
- `test_try_book_transient_error_on_5xx_submit`
- `test_try_book_sends_origin_and_correct_referers`

Keep `test_classify_http_error_splits_on_status` (still valid — exercises `_classify_http_error` directly), and all the non-try_book tests (`test_parse_csrf_token_*`, `test_parse_fb_user_id_*`, `test_available_slot_is_immutable`, `test_booking_result_has_four_outcomes`, `test_client_*`, `test_search_*`, `test_warmup_*`, `test_fmt_polyu_dt_*`).

`test_try_book_sends_correct_cell_click_body` and `test_try_book_sends_origin_and_correct_referers` deserve a second look — they exercise BODY and HEADER details across BOTH cell_click and submit. We added `test_submit_sends_multipart_with_csrf_and_declare` in Task 3 covering submit. For cell_click body + headers, add this replacement test at the end of `tests/test_http_client.py`:

```python
@pytest.mark.asyncio
@respx.mock
async def test_cell_click_sends_correct_form_and_headers():
    from src.http_client import PolyUHttpClient

    captured = {}
    def record(request):
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        captured["headers"] = dict(request.headers)
        return Response(
            302,
            headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"},
        )
    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(side_effect=record)

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="tok-Z", fb_user_id="432567")
    try:
        await client.cell_click(_slot_11_at_1230())
    finally:
        await client.aclose()

    body = captured["body"]
    assert "CSRFToken=tok-Z" in body
    assert "dataSetId=18" in body
    assert "actvId=10" in body
    assert "boMakeBookFacilities%5B0%5D.ctrId=1" in body
    assert "boMakeBookFacilities%5B0%5D.facilityId=11" in body
    assert "boMakeBookFacilities%5B0%5D.startDateTime=10+Jun+2026+12%3A30" in body
    assert "boMakeBookFacilities%5B0%5D.endDateTime=10+Jun+2026+13%3A30" in body
    assert "%252F06%252F2026" in body

    headers = captured["headers"]
    assert headers.get("origin") == "https://www40.polyu.edu.hk"
    assert "make_book.do" in headers.get("referer", "")
    assert "make_book_submit" not in headers.get("referer", "")
    assert headers.get("upgrade-insecure-requests") == "1"
```

- [ ] **Step 4: Run full test suite**

```bash
uv run pytest
```

Expected: all PASS. Test count drops by ~7 (deleted try_book tests, replaced by one consolidated cell_click body+header test).

- [ ] **Step 5: Commit**

```bash
git add src/http_client.py tests/test_http_client.py
git commit -m "chore(http_client): remove obsolete try_book method and tests

cell_click + submit fully cover the previous behaviour; the parallel
orchestrator no longer needs the combined try_book method.
Body + header lock-in test for cell_click added as a replacement."
```

---

## Task 9: Final verification — run full suite + manual dry-run

**Files:** none

Sanity gate before declaring the change done.

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest -v
```

Expected: all tests PASS. New test count vs baseline: roughly +15 (cell_click) +8 (submit) +3 (warmup) +1 (pool limits) +12 (http_booker) -9 (try_book deletions) -2 (warmup old) = ~+28 net new tests.

- [ ] **Step 2: Optional — manual dry-run against live PolyU**

Requires real credentials. Skip if running CI-only.

```bash
POLYU_USERNAME=... POLYU_PASSWORD=... uv run book-tennis --dry-run --skip-sleep
```

Expected log highlights:
- `warming up 4 HTTP connections`
- `warmup complete (statuses=[200, 200, 200, 200])` (or similar — list of 4 ints)
- `predictive booking: 4 candidates queued`
- `DRY RUN: stopping before cell_click phase`
- exit code 0

(`--dry-run` exits before phase 1, so cell_clicks/submits don't fire; this verifies the warmup expansion and orchestrator wiring without touching the booking endpoint.)

- [ ] **Step 3: Confirm no skipped tests, no unexpected warnings**

```bash
uv run pytest -v 2>&1 | grep -E "(SKIPPED|XFAIL|WARNING|warning)" | head -20
```

Expected: only the asyncio default warning (if any). No SKIPPED. No XFAIL.

- [ ] **Step 4: Plan complete**

No commit for Task 9 (verification only, no file changes).

---

## Notes for the executing engineer

- **The codebase uses `respx` for httpx mocking, not `httpx.MockTransport`.** All tests that mock HTTP use `@respx.mock` + `respx.post(...).mock(return_value=...)` or `mock(side_effect=...)`. Follow this style.
- **Lazy imports inside test functions are the local convention** (`from src.http_client import PolyUHttpClient` inside the test body, not at module top). Match it.
- **`uv run pytest` is the only test runner used.** Don't introduce a separate `pytest.ini` or test config.
- **TLS warmup connections are observable through `respx` call counts**, not real socket inspection. The parallelism test for `book_via_http` uses `asyncio.sleep` in the fake client + `time.perf_counter()` wall-clock assertions, which is the cleanest available signal.
- **The 5–6s first-POST latency is NOT addressed by this change directly** — only worked around via concurrency. Section A.2 diagnostics will likely surface the root cause in subsequent CI runs. Don't try to fix the latency itself as part of this plan.
- **`_classify_http_error` returns `BookingResult.ERROR_TRANSIENT` / `BookingResult.ERROR_FATAL`** — i.e. it returns a `BookingResult` enum, not a `CellOutcome`. `cell_click` translates the result; submit uses it directly. Don't conflate the two enums.
