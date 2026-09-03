"""Offline tests for bootstrap_http_client.

Uses a fake Playwright page (a small async stub) — no real browser launch.
The fixture HTML is a minimal real-shape excerpt of make_book.do.
"""
from pathlib import Path

import pytest

_FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "make_book_post_login.html").read_text()


class _FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return self._cookies


class _FakePage:
    def __init__(self, html, cookies):
        self._html = html
        self.context = _FakeContext(cookies)

    async def content(self):
        return self._html


@pytest.mark.asyncio
async def test_bootstrap_extracts_session_state_from_post_login_page():
    from src.booker import bootstrap_http_client

    page = _FakePage(
        html=_FIXTURE_HTML,
        cookies=[
            {"name": "JSESSIONID", "value": "abc123", "domain": "www40.polyu.edu.hk", "path": "/starspossfbns"},
            {"name": "AWSALB", "value": "lb-token", "domain": "www40.polyu.edu.hk", "path": "/"},
            # A cookie from an unrelated domain — must be filtered out.
            {"name": "ga", "value": "tracking", "domain": "google-analytics.com", "path": "/"},
        ],
    )
    import logging
    log = logging.getLogger("test")
    client = await bootstrap_http_client(page, log=log)
    try:
        assert client.csrf_token == "0cd6a396-5498-4d05-a3f8-a6fefaa2f9ea"
        assert client.fb_user_id == "432567"
        # Cookies from polyu.edu.hk only; the google-analytics one is filtered.
        assert client._http.cookies.get("JSESSIONID") == "abc123"
        assert client._http.cookies.get("AWSALB") == "lb-token"
        assert client._http.cookies.get("ga") is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_bootstrap_raises_when_html_is_unexpected():
    # If PolyU redirects us somewhere that's not make_book.do (e.g. a
    # password-expired prompt), the parsers raise HtmlParseError — surface
    # it so the watchdog email subject is meaningful.
    from src.booker import bootstrap_http_client
    from src.http_client import HtmlParseError

    page = _FakePage(
        html="<html><body>Your password has expired.</body></html>",
        cookies=[{"name": "JSESSIONID", "value": "x", "domain": "www40.polyu.edu.hk"}],
    )
    import logging
    log = logging.getLogger("test")
    with pytest.raises(HtmlParseError):
        await bootstrap_http_client(page, log=log)


@pytest.mark.asyncio
async def test_bootstrap_preserves_path_scoped_duplicates():
    """PolyU has two JSESSIONIDs (Path=/possns anonymous + Path=/starspossfbns
    authenticated). The bootstrap helper must keep BOTH so httpx sends the
    right one to /starspossfbns/* endpoints — collapsing by name causes 403."""
    from src.booker import bootstrap_http_client

    page = _FakePage(
        html=_FIXTURE_HTML,
        cookies=[
            {"name": "JSESSIONID", "value": "ANON",
             "domain": "www40.polyu.edu.hk", "path": "/possns"},
            {"name": "JSESSIONID", "value": "AUTH",
             "domain": "www40.polyu.edu.hk", "path": "/starspossfbns"},
            {"name": "LtpaToken2", "value": "sso",
             "domain": "www40.polyu.edu.hk", "path": "/"},
        ],
    )
    import logging
    log = logging.getLogger("test")
    client = await bootstrap_http_client(page, log=log)
    try:
        # The httpx cookie jar should hold both JSESSIONIDs.
        jar = client._http.cookies
        # Use path-scoped get to verify both survive.
        auth = jar.get("JSESSIONID", path="/starspossfbns")
        anon = jar.get("JSESSIONID", path="/possns")
        assert auth == "AUTH"
        assert anon == "ANON"
    finally:
        await client.aclose()
