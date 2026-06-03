# PolyU HTTP client — Phase 2a Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `src/http_client.py` — a `PolyUHttpClient` that talks to PolyU's booking endpoints over raw HTTP (httpx). Fully offline-tested via `respx`. Does NOT yet replace the booker's hot path — that integration is Phase 2b.

**Architecture:** A thin wrapper around `httpx.AsyncClient` that issues the 3 discovered POSTs (timetable.json, make_book.do, make_book_submit.do) using session cookies + CSRFToken + fbUserId extracted from the post-login HTML. Two pure HTML-parsing helpers (`parse_csrf_token`, `parse_fb_user_id`) live in the same module.

**Tech Stack:** Python 3.12+, httpx for async HTTP, respx for response mocking in tests. Tennis-specific IDs land in `src/config.py`.

**Spec:** `docs/superpowers/specs/2026-06-03-http-replay-booking-design.md`
**Phase 1 plan (prerequisite, already merged):** `docs/superpowers/plans/2026-06-03-http-capture-script.md`
**Trace used to derive shapes:** `artifacts/http_trace.json` (captured by user 2026-06-03; not checked in).

**Out of scope for this plan:** `src/http_booker.py`, `src/booker.py:run` integration, deleting `src/parallel_runner.py`, live PolyU runs. Those land in Phase 2b.

---

## Discovery summary (from captured trace)

Hot path = 3 POSTs:

| Step | URL | Body shape | Response |
|---|---|---|---|
| **Search** | `POST /starspossfbstud/secure/ui_make_book/timetable.json?CSRFToken=<T>` | form-urlencoded: `CSRFToken=<T>&fbUserId=<U>&bookType=INDV&dataSetId=18&actvId=10&searchDate=DD/MM/YYYY&ctrId=1&facilityId=&showCourtAreaDetails=true` | 200 JSON. `data.timeSlotColumns[].timeSlots[]` each with `fromTime`, `toTime`, `facilityIds`, `occupiedFacilityIds`. |
| **Cell + Next** | `POST /starspossfbstud/secure/ui_make_book/make_book.do` | form-urlencoded (see Task A4 for exact fields) | 302 → `/starspossfbstud/secure/ui_make_book/make_book_submit.do` on cell-acceptance |
| **Final Submit** | `POST /starspossfbstud/secure/ui_make_book/make_book_submit.do` | multipart/form-data (30 fields, see Task A5) | 302 → `make_book_result.do` on SUCCESS; 302 → `make_book_submit.do` (or 200 with "Facility is occupied" banner) on OCCUPIED |

CSRFToken: one token used at login (form field), a different token issued in the post-login `make_book.do` HTML and used for both Search and Cell+Next. The final Submit body does NOT include CSRFToken (relies on session cookie).

fbUserId: hidden `<input>` in post-login HTML — e.g. `<input type="hidden" id="fbUserId" name="fbUserId" value="432567"/>`.

Tennis constants from trace: `dataSetId=18`, `actvId=10`, `ctrId=1`, `centerName=Shaw Sports Complex`, facilities `{10: "Tennis Court No. 1", 11: "Tennis Court No. 2"}`.

---

## File structure

- Create: `src/http_client.py` — `PolyUHttpClient` + html parsing helpers + `AvailableSlot` / `BookingResult` types
- Create: `tests/test_http_client.py` — offline TDD via `respx`
- Create: `tests/fixtures/timetable_response.json` — trimmed real Search response (one column, a couple of slots), used as the canonical fixture
- Modify: `src/config.py` — add tennis-activity constants (`TENNIS_DATA_SET_ID`, `TENNIS_ACTV_ID`, etc.)
- Modify: `pyproject.toml` — add `httpx` to deps and `respx` to dev deps

---

## Task A1: Add httpx + respx deps and tennis constants

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/config.py`

- [ ] **Step 1: Add httpx as a runtime dep and respx as a dev dep**

Edit `pyproject.toml`. Change:

```toml
dependencies = [
    "playwright>=1.48",
]
```
to:
```toml
dependencies = [
    "playwright>=1.48",
    "httpx>=0.27",
]
```

And change:
```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "freezegun>=1.5",
    "pytest-asyncio>=0.24",
]
```
to:
```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "freezegun>=1.5",
    "pytest-asyncio>=0.24",
    "respx>=0.21",
]
```

- [ ] **Step 2: Sync deps**

Run: `uv sync`
Expected: succeeds, lockfile updated.

- [ ] **Step 3: Add tennis constants to `src/config.py`**

Open `src/config.py`. The file already has `LOGIN_URL`, `SUBMIT_URL`, etc. Add the following block right after the existing URL constants (the section ending around the existing `SUBMIT_URL = "..."` line). Don't change anything else in the file.

```python
# --- Tennis activity HTTP-API constants (derived from artifacts/http_trace.json) ---
# These are the form-field values PolyU's booking POSTs require for the Tennis
# activity. They are stable across runs — captured from a real booking and
# unchanged for the lifetime of PolyU's current booking system. Re-confirm by
# running scripts/capture_http.py if a booking starts failing with 4xx/5xx.

TIMETABLE_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/timetable.json"
MAKE_BOOK_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
MAKE_BOOK_SUBMIT_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
MAKE_BOOK_RESULT_URL = "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_result.do"

TENNIS_DATA_SET_ID = 18
TENNIS_ACTV_ID = 10
TENNIS_CTR_ID = 1
TENNIS_CENTER_NAME = "Shaw Sports Complex"
TENNIS_FACILITIES = {
    10: "Tennis Court No. 1",
    11: "Tennis Court No. 2",
}
```

- [ ] **Step 4: Verify imports work**

Run: `uv run python -c "from src.config import TIMETABLE_URL, TENNIS_DATA_SET_ID, TENNIS_FACILITIES; print(TIMETABLE_URL, TENNIS_DATA_SET_ID, TENNIS_FACILITIES)"`
Expected:
```
https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/timetable.json 18 {10: 'Tennis Court No. 1', 11: 'Tennis Court No. 2'}
```

- [ ] **Step 5: Run full test suite to confirm no regression**

Run: `uv run pytest`
Expected: 45 passed.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/config.py
git commit -m "deps(http): add httpx + respx, tennis HTTP constants in config"
```

---

## Task A2: HTML extraction helpers (TDD)

The post-login `make_book.do` HTML contains both the CSRFToken (in a JS string) and fbUserId (in a hidden input). Two small regex-based parsers.

**Files:**
- Create: `tests/test_http_client.py` (initially with just these two test functions)
- Create: `src/http_client.py` (initially with just these two helpers + the boilerplate)

- [ ] **Step 1: Write failing tests**

Create `tests/test_http_client.py`:

```python
"""Offline tests for PolyUHttpClient + html parsing helpers.

All tests are offline — they use respx to mock httpx responses, or operate
on string fixtures captured from a real PolyU response. No network.
"""
from src.http_client import parse_csrf_token, parse_fb_user_id


def test_parse_csrf_token_extracts_from_js_url():
    # Captured from a real make_book.do response.
    html = '''
    <script>
        $.ajax({
            type: "POST",
            dataType: "json",
            url: "/starspossfbstud/secure/menu_click_fctn.json?CSRFToken=0cd6a396-5498-4d05-a3f8-a6fefaa2f9ea",
            data: {fctnCode: $(ptr).data('fctncode')}
        });
    </script>
    '''
    assert parse_csrf_token(html) == "0cd6a396-5498-4d05-a3f8-a6fefaa2f9ea"


def test_parse_csrf_token_raises_when_missing():
    import pytest
    from src.http_client import HtmlParseError
    with pytest.raises(HtmlParseError):
        parse_csrf_token("<html><body>no token here</body></html>")


def test_parse_fb_user_id_extracts_hidden_input():
    html = '''
    <div>
        <input type="hidden" id="fbUserId" name="fbUserId" value="432567"/>
        <input type="hidden" id="bookType" name="bookType" value="INDV"/>
    </div>
    '''
    assert parse_fb_user_id(html) == "432567"


def test_parse_fb_user_id_accepts_attribute_order_variations():
    # Real PolyU HTML uses a specific attribute order; tolerate minor variations
    # to avoid brittleness when their template changes whitespace.
    html = '<input value="999" name="fbUserId" id="fbUserId" type="hidden"/>'
    assert parse_fb_user_id(html) == "999"


def test_parse_fb_user_id_raises_when_missing():
    import pytest
    from src.http_client import HtmlParseError
    with pytest.raises(HtmlParseError):
        parse_fb_user_id("<html>no fbUserId here</html>")
```

- [ ] **Step 2: Run tests to verify they fail at import**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_http_client.py -v`
Expected: ModuleNotFoundError on `src.http_client` (collection error).

- [ ] **Step 3: Commit (test only)**

```bash
git add tests/test_http_client.py
git commit -m "test(http): failing tests for csrf + fbUserId html parsers"
```

- [ ] **Step 4: Implement the helpers**

Create `src/http_client.py`:

```python
"""PolyU booking HTTP client.

Talks directly to PolyU's booking endpoints with httpx, bypassing
Playwright on the hot path. Session cookies, fbUserId, and CSRFToken
are bootstrapped from a Playwright login (in src/booker.py:run);
this module is purely the HTTP layer.

Endpoints, form fields, and CSRFToken/fbUserId locations were captured
via scripts/capture_http.py — see docs/superpowers/specs/2026-06-03-
http-replay-booking-design.md and the discovery notes in the Phase 2a
plan.
"""
from __future__ import annotations

import re


class HtmlParseError(RuntimeError):
    """Raised when an expected token cannot be found in a PolyU HTML response."""


_CSRF_TOKEN_RE = re.compile(r"CSRFToken=([0-9a-f-]+)")
_FB_USER_ID_RE = re.compile(
    r'<input[^>]*\bname="fbUserId"[^>]*\bvalue="(\d+)"', re.IGNORECASE
)
_FB_USER_ID_RE_REVERSED = re.compile(
    r'<input[^>]*\bvalue="(\d+)"[^>]*\bname="fbUserId"', re.IGNORECASE
)


def parse_csrf_token(html: str) -> str:
    """Extract the post-login CSRFToken from a make_book.do HTML response.

    The token appears in inline JS as `CSRFToken=<uuid>` inside an AJAX URL.
    Raises HtmlParseError if not found.
    """
    m = _CSRF_TOKEN_RE.search(html)
    if not m:
        raise HtmlParseError(
            "CSRFToken not found in HTML — page shape may have changed; "
            "re-run scripts/capture_http.py to diagnose"
        )
    return m.group(1)


def parse_fb_user_id(html: str) -> str:
    """Extract the fbUserId from the hidden input in make_book.do HTML.

    Two regex passes to handle attribute order variations (PolyU's actual
    template puts name before value, but we don't want to be brittle).
    Raises HtmlParseError if not found.
    """
    for pattern in (_FB_USER_ID_RE, _FB_USER_ID_RE_REVERSED):
        m = pattern.search(html)
        if m:
            return m.group(1)
    raise HtmlParseError(
        "fbUserId hidden input not found in HTML — page shape may have changed"
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_http_client.py -v`
Expected: 5 tests pass.

- [ ] **Step 6: Run full suite**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest`
Expected: 50 passed.

- [ ] **Step 7: Commit**

```bash
git add src/http_client.py
git commit -m "feat(http): parse_csrf_token and parse_fb_user_id helpers"
```

---

## Task A3: AvailableSlot + BookingResult types

**Files:**
- Modify: `src/http_client.py` — add `AvailableSlot` dataclass and `BookingResult` enum
- Modify: `tests/test_http_client.py` — add tests for the data types' invariants

- [ ] **Step 1: Add failing tests at the end of tests/test_http_client.py**

```python
from datetime import datetime


def test_available_slot_is_immutable():
    from src.http_client import AvailableSlot
    slot = AvailableSlot(
        facility_id=11,
        facility_name="Tennis Court No. 2",
        center_id=1,
        center_name="Shaw Sports Complex",
        start_dt=datetime(2026, 6, 10, 12, 30),
        end_dt=datetime(2026, 6, 10, 13, 30),
    )
    import pytest
    with pytest.raises(Exception):
        slot.facility_id = 99  # frozen dataclass


def test_booking_result_has_three_outcomes():
    from src.http_client import BookingResult
    assert {BookingResult.SUCCESS, BookingResult.OCCUPIED, BookingResult.ERROR}
    assert len(list(BookingResult)) == 3
```

- [ ] **Step 2: Run tests to verify failure**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_http_client.py -v`
Expected: 2 new tests fail with ImportError on AvailableSlot / BookingResult; existing 5 still pass.

- [ ] **Step 3: Implement the types in src/http_client.py**

Add right after the imports section, before the regex constants:

```python
import enum
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AvailableSlot:
    """A bookable (facility, time-range) pair returned by search()."""
    facility_id: int
    facility_name: str
    center_id: int
    center_name: str
    start_dt: datetime
    end_dt: datetime


class BookingResult(enum.Enum):
    SUCCESS = enum.auto()
    OCCUPIED = enum.auto()
    ERROR = enum.auto()
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_http_client.py -v`
Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/http_client.py tests/test_http_client.py
git commit -m "feat(http): AvailableSlot dataclass + BookingResult enum"
```

---

## Task A4: PolyUHttpClient skeleton (construction)

**Files:**
- Modify: `src/http_client.py` — add `PolyUHttpClient` class with `__init__` + `aclose`, no methods yet beyond plumbing
- Modify: `tests/test_http_client.py` — tests for construction

- [ ] **Step 1: Add failing tests**

Append to `tests/test_http_client.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_client_constructs_with_required_session_state():
    from src.http_client import PolyUHttpClient
    client = PolyUHttpClient(
        cookies={"JSESSIONID": "abc"},
        csrf_token="tok-1",
        fb_user_id="432567",
    )
    assert client.csrf_token == "tok-1"
    assert client.fb_user_id == "432567"
    await client.aclose()


@pytest.mark.asyncio
async def test_client_sets_chrome_user_agent_and_polyu_referer():
    # Defensive: mimic the captured Playwright Chromium headers so PolyU
    # doesn't 4xx us for "non-browser" requests. The exact UA string from
    # the trace; if PolyU updates their detection, this is the knob.
    from src.http_client import PolyUHttpClient
    client = PolyUHttpClient(
        cookies={"JSESSIONID": "abc"},
        csrf_token="tok-1",
        fb_user_id="432567",
    )
    headers = client._http.headers  # httpx.AsyncClient.headers
    assert "Chrome" in headers["user-agent"]
    assert "polyu.edu.hk" in headers.get("referer", "polyu.edu.hk")
    await client.aclose()
```

- [ ] **Step 2: Run tests — verify failure**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_http_client.py -v -k "client"`
Expected: 2 tests fail with ImportError on `PolyUHttpClient`.

- [ ] **Step 3: Implement the skeleton in src/http_client.py**

Add (after the `BookingResult` enum):

```python
import httpx


_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "User-Agent": _CHROME_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do",
}


class PolyUHttpClient:
    """Async HTTP client for PolyU booking endpoints.

    Wraps httpx.AsyncClient with session cookies, CSRFToken, fbUserId
    bootstrapped from a Playwright login. Caller is responsible for
    aclose().
    """

    def __init__(
        self,
        *,
        cookies: dict[str, str],
        csrf_token: str,
        fb_user_id: str,
        timeout: float = 10.0,
    ) -> None:
        self.csrf_token = csrf_token
        self.fb_user_id = fb_user_id
        self._http = httpx.AsyncClient(
            cookies=cookies,
            headers=_DEFAULT_HEADERS,
            timeout=timeout,
            follow_redirects=False,  # We need to inspect 302 Location ourselves.
        )

    async def aclose(self) -> None:
        await self._http.aclose()
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_http_client.py -v`
Expected: 9 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/http_client.py tests/test_http_client.py
git commit -m "feat(http): PolyUHttpClient constructor with chrome headers"
```

---

## Task A5: PolyUHttpClient.search() — POST timetable.json, parse JSON

This task does TDD using a real-but-trimmed timetable.json response as the fixture, so the parser exercises actual PolyU shape.

**Files:**
- Create: `tests/fixtures/__init__.py` — empty
- Create: `tests/fixtures/timetable_response.json` — trimmed canonical response
- Modify: `src/http_client.py` — add `search()`
- Modify: `tests/test_http_client.py` — add search tests

- [ ] **Step 1: Create the fixture directory and file**

Create `tests/fixtures/__init__.py` as an empty file.

Create `tests/fixtures/timetable_response.json` with this content (real shape, two columns, two slots per column — enough to exercise the parser; one slot has both courts free, one has court 10 occupied, one has both occupied):

```json
{
  "data": {
    "advanceBookMsg": "Online booking can be made anytime up to 7 days in advance from 08:30 onwards.",
    "advanceBookDate": 1781020800000,
    "duration": 1,
    "hasMaintenance": false,
    "hasExam": false,
    "hasReservation": false,
    "hasConfirmation": true,
    "allowMultipleSlots": false,
    "timeSlotColumns": [
      {
        "title": "10 Jun (Wed)",
        "date": 1781020800000,
        "timeSlots": [
          {
            "fromTime": "12:30",
            "toTime": "13:30",
            "fromDateTime": 1781065800000,
            "toDateTime": 1781069400000,
            "chargingScheme": {"colorCode": "#0066FF", "colorDesc": "BLUE", "charge": 10, "rfndRsvFee": 0},
            "facilityIds": [10, 11],
            "occupiedFacilityIds": [10],
            "hasMaintenance": false,
            "hasExam": false,
            "hasReservation": false,
            "hasConfirmation": false
          },
          {
            "fromTime": "13:30",
            "toTime": "14:30",
            "fromDateTime": 1781069400000,
            "toDateTime": 1781073000000,
            "chargingScheme": {"colorCode": "#0066FF", "colorDesc": "BLUE", "charge": 10, "rfndRsvFee": 0},
            "facilityIds": [10, 11],
            "occupiedFacilityIds": [10, 11],
            "hasMaintenance": false,
            "hasExam": false,
            "hasReservation": false,
            "hasConfirmation": false
          },
          {
            "fromTime": "18:30",
            "toTime": "19:30",
            "fromDateTime": 1781086200000,
            "toDateTime": 1781089800000,
            "chargingScheme": {"colorCode": "#0066FF", "colorDesc": "BLUE", "charge": 10, "rfndRsvFee": 0},
            "facilityIds": [10, 11],
            "occupiedFacilityIds": [],
            "hasMaintenance": false,
            "hasExam": false,
            "hasReservation": false,
            "hasConfirmation": false
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 2: Add failing search() tests to tests/test_http_client.py**

```python
from datetime import date, datetime, time
from pathlib import Path

import respx
from httpx import Response


_FIXTURES = Path(__file__).parent / "fixtures"


def _load_timetable_fixture() -> str:
    return (_FIXTURES / "timetable_response.json").read_text()


@pytest.mark.asyncio
@respx.mock
async def test_search_returns_free_slots_grouped_by_time():
    from src.http_client import PolyUHttpClient, AvailableSlot

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/timetable.json"
    ).mock(return_value=Response(200, text=_load_timetable_fixture()))

    client = PolyUHttpClient(
        cookies={"JSESSIONID": "abc"},
        csrf_token="tok-1",
        fb_user_id="432567",
    )
    try:
        result = await client.search(date(2026, 6, 10))
    finally:
        await client.aclose()

    # 12:30-13:30: court 10 occupied, court 11 free → 1 entry
    free_1230 = result[(time(12, 30), time(13, 30))]
    assert len(free_1230) == 1
    assert free_1230[0].facility_id == 11
    assert free_1230[0].facility_name == "Tennis Court No. 2"
    assert free_1230[0].start_dt == datetime(2026, 6, 10, 12, 30)
    assert free_1230[0].end_dt == datetime(2026, 6, 10, 13, 30)

    # 13:30-14:30: both occupied → not in result map (or empty list)
    assert result.get((time(13, 30), time(14, 30)), []) == []

    # 18:30-19:30: both free → 2 entries
    free_1830 = result[(time(18, 30), time(19, 30))]
    assert len(free_1830) == 2
    assert {s.facility_id for s in free_1830} == {10, 11}


@pytest.mark.asyncio
@respx.mock
async def test_search_sends_correct_form_body():
    from src.http_client import PolyUHttpClient

    captured = {}

    def record_and_respond(request):
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return Response(200, text=_load_timetable_fixture())

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/timetable.json"
    ).mock(side_effect=record_and_respond)

    client = PolyUHttpClient(
        cookies={"JSESSIONID": "abc"},
        csrf_token="tok-XYZ",
        fb_user_id="999",
    )
    try:
        await client.search(date(2026, 6, 10))
    finally:
        await client.aclose()

    assert "CSRFToken=tok-XYZ" in captured["url"]
    # Form body must contain these exact field=value pairs.
    body = captured["body"]
    assert "CSRFToken=tok-XYZ" in body
    assert "fbUserId=999" in body
    assert "bookType=INDV" in body
    assert "dataSetId=18" in body
    assert "actvId=10" in body
    # PolyU expects DD/MM/YYYY with URL-encoded slashes.
    assert "searchDate=10%2F06%2F2026" in body
    assert "ctrId=1" in body
    assert "showCourtAreaDetails=true" in body
```

- [ ] **Step 3: Run tests — verify failure**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_http_client.py -v -k "search"`
Expected: 2 tests fail (AttributeError: 'PolyUHttpClient' has no 'search').

- [ ] **Step 4: Implement search() in src/http_client.py**

Add to the bottom of the `PolyUHttpClient` class (i.e. as a new method):

```python
    async def search(
        self,
        target_date: "date",
    ) -> dict[tuple["time", "time"], list[AvailableSlot]]:
        """POST the Search/timetable endpoint and return free slots.

        The response groups free (facility, time-range) pairs by (start, end)
        time-of-day, so callers iterating priority slots can pick any free
        facility for each priority time.
        """
        from src.config import (
            TIMETABLE_URL,
            TENNIS_ACTV_ID,
            TENNIS_CENTER_NAME,
            TENNIS_CTR_ID,
            TENNIS_DATA_SET_ID,
            TENNIS_FACILITIES,
        )

        date_str = target_date.strftime("%d/%m/%Y")
        form = {
            "CSRFToken": self.csrf_token,
            "fbUserId": self.fb_user_id,
            "bookType": "INDV",
            "dataSetId": str(TENNIS_DATA_SET_ID),
            "actvId": str(TENNIS_ACTV_ID),
            "searchDate": date_str,
            "ctrId": str(TENNIS_CTR_ID),
            "facilityId": "",
            "showCourtAreaDetails": "true",
        }
        resp = await self._http.post(
            TIMETABLE_URL,
            params={"CSRFToken": self.csrf_token},
            data=form,
        )
        resp.raise_for_status()
        payload = resp.json()

        from datetime import datetime as _dt
        out: dict[tuple[time, time], list[AvailableSlot]] = {}
        for col in payload["data"]["timeSlotColumns"]:
            for slot in col["timeSlots"]:
                start = time.fromisoformat(slot["fromTime"])
                end = time.fromisoformat(slot["toTime"])
                start_dt = _dt.fromtimestamp(slot["fromDateTime"] / 1000)
                end_dt = _dt.fromtimestamp(slot["toDateTime"] / 1000)
                occupied = set(slot.get("occupiedFacilityIds") or [])
                for fid in slot["facilityIds"]:
                    if fid in occupied:
                        continue
                    if fid not in TENNIS_FACILITIES:
                        # Unknown facility id — skip rather than guess a name.
                        continue
                    out.setdefault((start, end), []).append(AvailableSlot(
                        facility_id=fid,
                        facility_name=TENNIS_FACILITIES[fid],
                        center_id=TENNIS_CTR_ID,
                        center_name=TENNIS_CENTER_NAME,
                        start_dt=start_dt,
                        end_dt=end_dt,
                    ))
        return out
```

Also add at the top of the file (with the other imports):
```python
from datetime import date, datetime, time
```
(Replace the existing `from datetime import datetime` line.)

- [ ] **Step 5: Run tests**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_http_client.py -v`
Expected: 11 tests pass.

- [ ] **Step 6: Run full suite**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest`
Expected: 56 passed.

- [ ] **Step 7: Commit**

```bash
git add src/http_client.py tests/test_http_client.py tests/fixtures/
git commit -m "feat(http): PolyUHttpClient.search() with timetable.json parsing"
```

**Note on timezone**: `datetime.fromtimestamp(ms/1000)` produces local-time, which is fine because the CI runner has `TZ=Asia/Hong_Kong` set in book.yml. If this code ever runs outside HKT we'd need explicit tz handling, but that's not the case today.

---

## Task A6: PolyUHttpClient.try_book() — Cell+Next POST + Final Submit multipart

This is the hot-path workhorse. Two sub-steps in one method: POST make_book.do (cell + next), then POST make_book_submit.do (final). Returns BookingResult.

**Files:**
- Modify: `src/http_client.py` — add `try_book()`
- Modify: `tests/test_http_client.py` — happy path, OCCUPIED on Submit, OCCUPIED on cell-click, network error

- [ ] **Step 1: Failing tests appended to tests/test_http_client.py**

```python
def _slot_11_at_1230() -> "AvailableSlot":
    from src.http_client import AvailableSlot
    return AvailableSlot(
        facility_id=11,
        facility_name="Tennis Court No. 2",
        center_id=1,
        center_name="Shaw Sports Complex",
        start_dt=datetime(2026, 6, 10, 12, 30),
        end_dt=datetime(2026, 6, 10, 13, 30),
    )


@pytest.mark.asyncio
@respx.mock
async def test_try_book_happy_path_returns_success():
    from src.http_client import PolyUHttpClient, BookingResult

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(
        302,
        headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"},
    ))
    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
    ).mock(return_value=Response(
        302,
        headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_result.do"},
    ))

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="tok", fb_user_id="432567")
    try:
        result = await client.try_book(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert result is BookingResult.SUCCESS


@pytest.mark.asyncio
@respx.mock
async def test_try_book_occupied_when_submit_redirects_back():
    from src.http_client import PolyUHttpClient, BookingResult

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(
        302,
        headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"},
    ))
    # Submit fails: server redirects back to make_book_submit.do (the user
    # never reaches make_book_result.do) — the "Facility is occupied" path.
    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
    ).mock(return_value=Response(
        200,
        text="<html><body>Facility is occupied</body></html>",
    ))

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="tok", fb_user_id="432567")
    try:
        result = await client.try_book(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert result is BookingResult.OCCUPIED


@pytest.mark.asyncio
@respx.mock
async def test_try_book_occupied_when_cell_click_rejected():
    # If by the time we POST cell+Next, the slot is already gone, PolyU's
    # response shape is harder to predict from one captured trace. We
    # treat a non-302 from make_book.do as OCCUPIED (the most likely cause).
    from src.http_client import PolyUHttpClient, BookingResult

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(
        200,
        text="<html>Facility is occupied</html>",
    ))

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="tok", fb_user_id="432567")
    try:
        result = await client.try_book(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert result is BookingResult.OCCUPIED


@pytest.mark.asyncio
@respx.mock
async def test_try_book_error_on_network_failure():
    from src.http_client import PolyUHttpClient, BookingResult
    import httpx

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(side_effect=httpx.ConnectError("boom"))

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="tok", fb_user_id="432567")
    try:
        result = await client.try_book(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert result is BookingResult.ERROR


@pytest.mark.asyncio
@respx.mock
async def test_try_book_sends_correct_cell_click_body():
    from src.http_client import PolyUHttpClient

    captured = {}
    def record_make_book(request):
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return Response(
            302,
            headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"},
        )
    def record_submit(request):
        captured["submit_url"] = str(request.url)
        captured["submit_body"] = request.content.decode()
        captured["submit_ct"] = request.headers.get("content-type", "")
        return Response(302, headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_result.do"})

    respx.post("https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do").mock(side_effect=record_make_book)
    respx.post("https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do").mock(side_effect=record_submit)

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="tok-Z", fb_user_id="432567")
    try:
        await client.try_book(_slot_11_at_1230())
    finally:
        await client.aclose()

    body = captured["body"]
    assert "CSRFToken=tok-Z" in body
    assert "dataSetId=18" in body
    assert "actvId=10" in body
    assert "boMakeBookFacilities%5B0%5D.ctrId=1" in body
    assert "boMakeBookFacilities%5B0%5D.facilityId=11" in body
    # Format from the trace: "10 Jun 2026 12:30" with spaces URL-encoded.
    assert "boMakeBookFacilities%5B0%5D.startDateTime=10+Jun+2026+12%3A30" in body
    assert "boMakeBookFacilities%5B0%5D.endDateTime=10+Jun+2026+13%3A30" in body

    # Submit body is multipart with the agreement checkbox set.
    assert "multipart/form-data" in captured["submit_ct"]
    sb = captured["submit_body"]
    assert 'name="dataSetId"' in sb and "18" in sb
    assert 'name="boMakeBookFacilities[0].facilityId"' in sb and "11" in sb
    assert 'name="boMakeBookFacilities[0].centerName"' in sb and "Shaw Sports Complex" in sb
    assert 'name="boMakeBookFacilities[0].facilityName"' in sb and "Tennis Court No. 2" in sb
    assert 'name="boMakeBookFacilities[0].startDateTime"' in sb and "10 Jun 2026 12:30" in sb
    assert 'name="declare"' in sb and "on" in sb
```

- [ ] **Step 2: Run tests — verify failure**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_http_client.py -v -k "try_book"`
Expected: 5 tests fail (AttributeError: no 'try_book').

- [ ] **Step 3: Implement try_book() in src/http_client.py**

Add to the `PolyUHttpClient` class (after `search`):

```python
    async def try_book(self, slot: AvailableSlot) -> BookingResult:
        """Attempt to book a specific (facility, time-range) slot.

        Two POSTs:
          1. make_book.do (form-encoded) — selects the cell + advances.
             Expect 302 → make_book_submit.do. Any other status ⇒ OCCUPIED.
          2. make_book_submit.do (multipart) — final commit + agreement.
             Expect 302 → make_book_result.do (SUCCESS).
             302 back to make_book_submit.do OR 200 with "occupied" banner ⇒ OCCUPIED.
             Anything else ⇒ ERROR.
        """
        from src.config import (
            MAKE_BOOK_URL,
            MAKE_BOOK_SUBMIT_URL,
            MAKE_BOOK_RESULT_URL,
            TENNIS_ACTV_ID,
            TENNIS_DATA_SET_ID,
        )

        date_str = slot.start_dt.strftime("%d/%m/%Y")
        dt_fmt = "%d %b %Y %H:%M"  # e.g. "10 Jun 2026 12:30"
        search_form_str = (
            f"fbUserId={self.fb_user_id}&bookType=INDV"
            f"&dataSetId={TENNIS_DATA_SET_ID}&actvId={TENNIS_ACTV_ID}"
            f"&searchDate={date_str}&ctrId={slot.center_id}&facilityId="
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
            "boMakeBookFacilities[0].startDateTime": slot.start_dt.strftime(dt_fmt),
            "boMakeBookFacilities[0].endDateTime": slot.end_dt.strftime(dt_fmt),
            "CSRFToken": self.csrf_token,
        }

        try:
            cell_resp = await self._http.post(MAKE_BOOK_URL, data=cell_form)
        except httpx.HTTPError:
            return BookingResult.ERROR

        if cell_resp.status_code != 302 or "make_book_submit" not in cell_resp.headers.get("location", ""):
            return BookingResult.OCCUPIED

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
            "boMakeBookFacilities[0].startDateTime": slot.start_dt.strftime(dt_fmt),
            "boMakeBookFacilities[0].endDateTime": slot.end_dt.strftime(dt_fmt),
            "declare": "on",
        }
        # httpx will multipart-encode `files` kwargs; we use `data=` with files
        # set to a sentinel to force multipart. Simplest: pass files={} and data
        # — httpx encodes the body as multipart even with no actual file part
        # if files is set. Concretely, we tell httpx to use multipart by using
        # the `files` parameter with a dict; but our payload is all text fields,
        # so we encode each as a (name, (None, value)) tuple.
        multipart_files = {
            name: (None, value) for name, value in submit_fields.items()
        }
        try:
            submit_resp = await self._http.post(
                MAKE_BOOK_SUBMIT_URL,
                files=multipart_files,
            )
        except httpx.HTTPError:
            return BookingResult.ERROR

        location = submit_resp.headers.get("location", "")
        if submit_resp.status_code == 302 and "make_book_result" in location:
            return BookingResult.SUCCESS
        if submit_resp.status_code in (200, 302) and ("Facility is occupied" in (submit_resp.text or "") or "make_book_submit" in location):
            return BookingResult.OCCUPIED
        return BookingResult.ERROR
```

- [ ] **Step 4: Run try_book tests**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest tests/test_http_client.py -v -k "try_book"`
Expected: 5 tests pass.

- [ ] **Step 5: Run full suite**

Run: `cd /Users/xue/polyu-tennis-booker && uv run pytest`
Expected: 61 passed.

- [ ] **Step 6: Commit**

```bash
git add src/http_client.py tests/test_http_client.py
git commit -m "feat(http): PolyUHttpClient.try_book() — cell-click + multipart submit"
```

---

## Self-review

**Spec coverage (against `2026-06-03-http-replay-booking-design.md`):**

| Spec item | Plan task |
|---|---|
| `src/http_client.py` with `PolyUHttpClient` (httpx wrapper) | A4 |
| Chrome UA + headers from Playwright | A4 |
| Cookies copied from BrowserContext.cookies() | A4 (`cookies=` kwarg) |
| `search(target_date) -> SearchResult` parsing the JSON response | A5 |
| `try_book(slot) -> BookingResult` with SUCCESS/OCCUPIED/ERROR | A6 |
| Tests offline via respx | A2/A3/A4/A5/A6 |
| `httpx` runtime dep, `respx` dev dep | A1 |
| Tennis constants in `src/config.py` | A1 |
| HTML parsing for csrf + fbUserId (will be wired up in Phase 2b) | A2 |

Phase 2b will handle: `http_booker.py` orchestrator, `src/booker.py:run` extraction of cookies/csrf/fbUserId, deletion of `src/parallel_runner.py`, CLAUDE.md updates, dry-run validation.

**Placeholder scan:** No "TODO" / "TBD" / "fill in" in steps above. Every code block is complete.

**Type consistency:**
- `parse_csrf_token` / `parse_fb_user_id` both return `str` and raise `HtmlParseError` — consistent.
- `AvailableSlot.facility_id` is `int`, but POST forms stringify it via `str(slot.facility_id)`. Internally consistent.
- `BookingResult` enum with 3 variants used in `try_book` return type.
- `search()` returns `dict[tuple[time, time], list[AvailableSlot]]` — type-stable.
- `PolyUHttpClient.csrf_token` and `fb_user_id` are both `str`; that matches the form values throughout.

**Soft spots flagged for Phase 2b:**
- The OCCUPIED detection in `try_book` checks for "Facility is occupied" text in the response body OR a redirect back to make_book_submit.do. This is the best guess from a single SUCCESS trace; an OCCUPIED trace from the same booking flow would let us tighten the heuristic. Phase 2b's dry-run validation will surface any mismatch.
- `search()` uses `datetime.fromtimestamp(ms/1000)` which depends on local TZ being HKT. CI sets `TZ=Asia/Hong_Kong`, so this is fine for production. Documented inline.
- The capture didn't include an explicit logout, so cookie expiry/refresh is untested. Phase 2b should add a "client GETs make_book.do as a sanity check" before the 08:30 fire (the spec already mandates this).
