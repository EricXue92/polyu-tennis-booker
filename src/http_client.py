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
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from urllib.parse import quote

import httpx


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
# add Origin / X-Requested-With / Referer overrides as needed (see search
# and try_book). All values verified against artifacts/http_trace.json.
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

    async def warmup(self) -> int:
        """GET make_book.do to establish a hot TLS connection in the pool.

        The booking flow sleeps ~50s between bootstrap and the 08:30 trigger.
        Servers typically drop idle keepalive connections in 15-30s, so the
        first POST at 08:30:00.000 pays a full TCP+TLS handshake (~5s observed
        on 2026-06-05). Calling this ~1-2s before the trigger primes the
        connection so the real cell-click POST goes out on a warm socket.

        Best-effort: returns the HTTP status code, or -1 on transport error.
        Never raises — a failed warmup must not prevent the real booking.
        """
        from src.config import MAKE_BOOK_URL
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
            TENNIS_ACTV_ID,
            TENNIS_DATA_SET_ID,
        )

        date_str = slot.start_dt.strftime("%d/%m/%Y")
        # Pre-encode the date in the inner searchFormString. The outer form-encoding
        # will encode the resulting %2F again to %252F, matching the captured trace.
        inner_date = quote(date_str, safe="")  # "10%2F06%2F2026"
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

        try:
            cell_resp = await self._http.post(
                MAKE_BOOK_URL,
                data=cell_form,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Origin": _ORIGIN,
                    "Referer": _REFERER_MAKE_BOOK,
                    "Upgrade-Insecure-Requests": "1",
                },
            )
        except httpx.HTTPError:
            return BookingResult.ERROR

        if cell_resp.status_code == 302 and "make_book_submit" in cell_resp.headers.get("location", ""):
            pass  # cell click accepted — fall through to Submit
        elif cell_resp.status_code in (200, 302) and (
            "Facility is occupied" in (cell_resp.text or "")
            or "make_book" in cell_resp.headers.get("location", "")
        ):
            return BookingResult.OCCUPIED
        else:
            return BookingResult.ERROR

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
        # httpx multipart encoding: pass `files={}` to force multipart even
        # for text-only fields. Each value becomes (None, value) — None means
        # no filename, which httpx renders as a plain text form part.
        multipart_files = {
            name: (None, value) for name, value in submit_fields.items()
        }
        try:
            submit_resp = await self._http.post(
                MAKE_BOOK_SUBMIT_URL,
                files=multipart_files,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Origin": _ORIGIN,
                    "Referer": _REFERER_MAKE_BOOK_SUBMIT,
                    "Upgrade-Insecure-Requests": "1",
                },
            )
        except httpx.HTTPError:
            return BookingResult.ERROR

        location = submit_resp.headers.get("location", "")
        if submit_resp.status_code == 302 and "make_book_result" in location:
            return BookingResult.SUCCESS
        if submit_resp.status_code in (200, 302) and ("Facility is occupied" in (submit_resp.text or "") or "make_book_submit" in location):
            return BookingResult.OCCUPIED
        return BookingResult.ERROR
