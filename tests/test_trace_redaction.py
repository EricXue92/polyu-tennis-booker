"""Unit tests for trace redaction helpers."""
from scripts._trace_redaction import redact_request, redact_response


def test_redact_request_strips_password_from_form_body():
    entry = {
        "url": "https://www40.polyu.edu.hk/.../login",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "post_data": "username=23012345d&password=SuperSecret123",
    }
    result = redact_request(entry, secret="SuperSecret123")
    assert result["post_data"] == "username=23012345d&password=***"


def test_redact_request_passes_through_unrelated_fields():
    entry = {
        "url": "https://www40.polyu.edu.hk/x",
        "method": "GET",
        "headers": {"Accept": "*/*"},
        "post_data": None,
    }
    result = redact_request(entry, secret="anything")
    assert result == entry


def test_redact_request_redacts_cookie_header_values():
    entry = {
        "url": "https://www40.polyu.edu.hk/x",
        "method": "GET",
        "headers": {
            "Cookie": "JSESSIONID=abc123def; XSRF-TOKEN=zzz",
            "Accept": "*/*",
        },
        "post_data": None,
    }
    result = redact_request(entry, secret="unrelated")
    assert result["headers"]["Cookie"] == "JSESSIONID=***; XSRF-TOKEN=***"
    assert result["headers"]["Accept"] == "*/*"


def test_redact_request_handles_missing_headers():
    entry = {"url": "x", "method": "GET", "headers": {}, "post_data": None}
    result = redact_request(entry, secret="s")
    assert result == entry


def test_redact_response_redacts_set_cookie_values():
    entry = {
        "url": "https://www40.polyu.edu.hk/login",
        "status": 200,
        "headers": {
            "Set-Cookie": "JSESSIONID=newvalue; Path=/; HttpOnly",
            "Content-Type": "text/html",
        },
        "body": "<html>ok</html>",
    }
    result = redact_response(entry, secret="anything")
    assert result["headers"]["Set-Cookie"] == "JSESSIONID=***; Path=/; HttpOnly"
    assert result["headers"]["Content-Type"] == "text/html"


def test_redact_response_replaces_secret_anywhere_in_body():
    # Defense in depth: if PolyU echoes the password back in an error page,
    # don't let it leak into the trace.
    entry = {
        "url": "x",
        "status": 200,
        "headers": {},
        "body": "Sorry, the password 'Hunter2' is wrong.",
    }
    result = redact_response(entry, secret="Hunter2")
    assert "Hunter2" not in result["body"]
    assert "***" in result["body"]


def test_redact_response_handles_empty_secret_safely():
    # Empty secret must not turn the body into '***...***' (str.replace('', x)
    # explodes a string into '<x><c><x><h><x>...').
    entry = {"url": "x", "status": 200, "headers": {}, "body": "anything"}
    result = redact_response(entry, secret="")
    assert result["body"] == "anything"
