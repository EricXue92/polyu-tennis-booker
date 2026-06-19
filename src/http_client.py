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

import enum
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from urllib.parse import quote

import httpx

_LOG = logging.getLogger("booker")


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
    # 5xx, network, or timeout — advance to the next candidate (session likely fine).
    ERROR_TRANSIENT = enum.auto()
    # Auth lost, 4xx, or unrecognised response shape — abort the run; remaining
    # candidates would hit the same wall.
    ERROR_FATAL = enum.auto()


class CellOutcome(enum.Enum):
    """Result of the cell-click POST (first stage of the two-phase booking)."""
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


def _classify_http_error(status: int) -> "BookingResult":
    if 500 <= status < 600:
        return BookingResult.ERROR_TRANSIENT
    return BookingResult.ERROR_FATAL


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


_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)
# Headers sent on every request. Per-request headers override Accept and
# add Origin / X-Requested-With / Referer overrides as needed (see search,
# cell_click, and submit). All values verified against artifacts/http_trace.json.
_DEFAULT_HEADERS = {
    "User-Agent": _CHROME_UA,
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="147", "Not.A/Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}

_REFERER_MAKE_BOOK = (
    "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
)
_REFERER_MAKE_BOOK_SUBMIT = (
    "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"
)
_ORIGIN = "https://www40.polyu.edu.hk"


_MONTH_ABBR = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _fmt_polyu_dt(dt: datetime) -> str:
    """Format datetime as 'DD MMM YYYY HH:MM' with English month abbreviation.

    Locale-independent — strftime('%b') depends on LC_TIME, which can
    produce 'juin' on a French system and break PolyU's parser.
    """
    return f"{dt.day:02d} {_MONTH_ABBR[dt.month]} {dt.year} {dt.hour:02d}:{dt.minute:02d}"


class PolyUHttpClient:
    """Async HTTP client for PolyU booking endpoints.

    Wraps httpx.AsyncClient with session cookies, CSRFToken, fbUserId
    bootstrapped from a Playwright login. Caller is responsible for
    aclose().
    """

    def __init__(
        self,
        *,
        cookies,
        csrf_token: str,
        fb_user_id: str,
        timeout: float = 6.0,
    ) -> None:
        # 6s upper bound on any single request. The slowest observed legitimate
        # SUCCESS submit was 5.3s (2026-06-16, rank 0); 6s gives small margin
        # over that while bounding ReadTimeout disasters like 2026-06-18
        # (10s wait poisoned the whole strict-priority chain). cell_click
        # latencies on warm pool are 150-300ms so this doesn't squeeze them.
        self.csrf_token = csrf_token
        self.fb_user_id = fb_user_id
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

    async def aclose(self) -> None:
        await self._http.aclose()

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
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": _REFERER_MAKE_BOOK,
            },
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

    async def cell_click(self, slot: AvailableSlot) -> CellClickResult:
        """POST make_book.do for one (facility, time) candidate.

        Returns CellClickResult with one of:
          ACCEPTED        - 302 -> make_book_submit.do; slot is server-side held,
                            caller can call submit() to commit.
          OCCUPIED        - 200/302 with "occupied" body OR Location bouncing back
                            to make_book*; slot was taken between our search and
                            this POST.
          ERROR_TRANSIENT - 5xx, network/timeout; safe to advance to the next
                            candidate, our session is probably still alive.
          ERROR_FATAL     - 4xx or unrecognised shape; this candidate is unbookable
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

    async def submit(self, slot: AvailableSlot) -> BookingResult:
        """POST make_book_submit.do to commit a slot whose cell_click was ACCEPTED.

        Returns:
          SUCCESS         - 302 -> make_book_result.do. Booking confirmed.
          OCCUPIED        - 302 back to make_book* OR body contains 'occupied'.
                            Slot was grabbed between our cell_click and this POST,
                            or PolyU's quota check rejected us.
          ERROR_TRANSIENT - 5xx, network/timeout.
          ERROR_FATAL     - 4xx or unrecognised shape; orchestrator should stop
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

