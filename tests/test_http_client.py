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
async def test_client_sets_chrome_user_agent_and_sec_ch_ua():
    # Defensive: mimic the captured Playwright Chromium headers so PolyU
    # doesn't 4xx us for "non-browser" requests. Per-request Referer /
    # Accept / Origin / X-Requested-With are set inside search() and
    # try_book(); only the truly common headers live on the client.
    from src.http_client import PolyUHttpClient
    client = PolyUHttpClient(
        cookies={"JSESSIONID": "abc"},
        csrf_token="tok-1",
        fb_user_id="432567",
    )
    headers = client._http.headers
    assert "Chrome" in headers["user-agent"]
    assert "Chromium" in headers.get("sec-ch-ua", "")
    assert headers.get("sec-ch-ua-mobile") == "?0"
    assert headers.get("sec-ch-ua-platform") == '"macOS"'
    await client.aclose()


from datetime import date, datetime, time
from pathlib import Path

import respx
from httpx import Response


@pytest.mark.asyncio
@respx.mock
async def test_warmup_sends_get_to_make_book_and_returns_status():
    from src.http_client import PolyUHttpClient

    route = respx.get(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(200, text="<html>book</html>"))

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="tok", fb_user_id="432567")
    try:
        status = await client.warmup()
    finally:
        await client.aclose()
    assert status == 200
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_warmup_returns_negative_one_on_transport_error():
    # A failed warmup must not raise — booking would still proceed.
    import httpx
    from src.http_client import PolyUHttpClient

    respx.get(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(side_effect=httpx.ConnectError("boom"))

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="tok", fb_user_id="432567")
    try:
        status = await client.warmup()
    finally:
        await client.aclose()
    assert status == -1


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
    # Inner searchFormString must have pre-encoded slashes (becomes %252F after outer encode)
    assert "searchFormString=" in body
    # After outer decode, the inner value should have %2F (pre-encoded).
    # In the wire body, the slashes appear as %252F.
    assert "%252F06%252F2026" in body

    # Submit body is multipart with the agreement checkbox set.
    assert "multipart/form-data" in captured["submit_ct"]
    sb = captured["submit_body"]
    assert 'name="dataSetId"' in sb and "18" in sb
    assert 'name="boMakeBookFacilities[0].facilityId"' in sb and "11" in sb
    assert 'name="boMakeBookFacilities[0].centerName"' in sb and "Shaw Sports Complex" in sb
    assert 'name="boMakeBookFacilities[0].facilityName"' in sb and "Tennis Court No. 2" in sb
    assert 'name="boMakeBookFacilities[0].startDateTime"' in sb and "10 Jun 2026 12:30" in sb
    assert 'name="declare"' in sb and "on" in sb
    assert 'name="CSRFToken"' in sb and "tok-Z" in sb


@pytest.mark.asyncio
@respx.mock
async def test_try_book_error_on_unexpected_cell_click_status():
    # If cell-click returns 500 (server error) or 401 (auth lost), we must
    # surface ERROR — not OCCUPIED — so the orchestrator can stop instead
    # of burning through all priorities.
    from src.http_client import PolyUHttpClient, BookingResult

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do"
    ).mock(return_value=Response(500, text="Internal Server Error"))

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="tok", fb_user_id="432567")
    try:
        result = await client.try_book(_slot_11_at_1230())
    finally:
        await client.aclose()
    assert result is BookingResult.ERROR


def test_fmt_polyu_dt_is_locale_independent():
    from src.http_client import _fmt_polyu_dt
    from datetime import datetime
    # Even if LC_TIME is set to a non-English locale, the formatter must
    # produce English month abbreviations.
    import locale
    try:
        original = locale.setlocale(locale.LC_TIME)
        # Try a few non-English locales; skip the test if none are available.
        for loc in ("fr_FR.UTF-8", "de_DE.UTF-8", "ja_JP.UTF-8", "C"):
            try:
                locale.setlocale(locale.LC_TIME, loc)
                break
            except locale.Error:
                continue
        result = _fmt_polyu_dt(datetime(2026, 6, 10, 12, 30))
        assert result == "10 Jun 2026 12:30"
    finally:
        try:
            locale.setlocale(locale.LC_TIME, original)
        except locale.Error:
            pass


@pytest.mark.asyncio
@respx.mock
async def test_search_sends_ajax_headers():
    """The 403-Forbidden post-mortem: Spring rejects timetable.json without
    X-Requested-With: XMLHttpRequest. Lock this in."""
    from src.http_client import PolyUHttpClient

    captured = {}
    def record(request):
        captured["headers"] = dict(request.headers)
        return Response(200, text=_load_timetable_fixture())

    respx.post(
        "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/timetable.json"
    ).mock(side_effect=record)

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        await client.search(date(2026, 6, 10))
    finally:
        await client.aclose()

    assert captured["headers"].get("x-requested-with") == "XMLHttpRequest"
    assert "application/json" in captured["headers"].get("accept", "")
    assert "make_book.do" in captured["headers"].get("referer", "")


@pytest.mark.asyncio
@respx.mock
async def test_try_book_sends_origin_and_correct_referers():
    """The 403-Forbidden post-mortem: cell-click and Submit need Origin +
    Upgrade-Insecure-Requests; final Submit has a DIFFERENT Referer
    (make_book_submit.do, not make_book.do)."""
    from src.http_client import PolyUHttpClient

    captured = {}
    def record_cell(request):
        captured["cell_headers"] = dict(request.headers)
        return Response(302, headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do"})
    def record_submit(request):
        captured["submit_headers"] = dict(request.headers)
        return Response(302, headers={"location": "https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_result.do"})

    respx.post("https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do").mock(side_effect=record_cell)
    respx.post("https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book_submit.do").mock(side_effect=record_submit)

    client = PolyUHttpClient(cookies={"JSESSIONID": "x"}, csrf_token="t", fb_user_id="1")
    try:
        await client.try_book(_slot_11_at_1230())
    finally:
        await client.aclose()

    # Cell-click: Origin + make_book.do referer + UIR.
    assert captured["cell_headers"].get("origin") == "https://www40.polyu.edu.hk"
    assert "make_book.do" in captured["cell_headers"].get("referer", "")
    assert "make_book_submit" not in captured["cell_headers"].get("referer", "")
    assert captured["cell_headers"].get("upgrade-insecure-requests") == "1"

    # Final Submit: Origin + make_book_submit.do referer + UIR.
    assert captured["submit_headers"].get("origin") == "https://www40.polyu.edu.hk"
    assert "make_book_submit.do" in captured["submit_headers"].get("referer", "")
    assert captured["submit_headers"].get("upgrade-insecure-requests") == "1"
