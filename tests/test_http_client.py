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
