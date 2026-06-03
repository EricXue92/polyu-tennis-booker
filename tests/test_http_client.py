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
