"""Pure helpers to redact secrets in HTTP trace entries.

Used by scripts/capture_http.py to scrub password values and cookie
contents before dumping the trace to disk. Pure functions; no I/O.
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


_COOKIE_PAIR_RE = re.compile(r"(\b[\w.-]+)=([^;]+)")


def _redact_cookie_header(value: str) -> str:
    """Replace every `name=value` pair's value with `***`, preserving
    delimiters (`; Path=/`, `; HttpOnly`, etc.). Cookie attribute
    keys are case-insensitive but we leave them as-is for diffability.
    """
    return _COOKIE_PAIR_RE.sub(
        lambda m: f"{m.group(1)}=***" if m.group(1).lower() not in _COOKIE_ATTRS else m.group(0),
        value,
    )


_COOKIE_ATTRS = {"path", "domain", "expires", "max-age", "samesite"}


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of headers with Cookie and Set-Cookie values redacted."""
    result = dict(headers)
    for key in list(result.keys()):
        if key.lower() in {"cookie", "set-cookie"}:
            result[key] = _redact_cookie_header(result[key])
    return result


def _redact_body(body: Any, secret: str) -> Any:
    """Replace literal occurrences of `secret` in a string body with `***`.

    No-op for falsy secrets or non-strings.
    """
    if not secret or not isinstance(body, str):
        return body
    return body.replace(secret, "***")


def redact_request(entry: dict, *, secret: str) -> dict:
    """Return a redacted copy of a request trace entry.

    Redactions:
    - `headers[Cookie]` cookie values → `***`
    - `post_data` literal `secret` occurrences → `***`
    """
    result = deepcopy(entry)
    if "headers" in result and isinstance(result["headers"], dict):
        result["headers"] = _redact_headers(result["headers"])
    if "post_data" in result:
        result["post_data"] = _redact_body(result["post_data"], secret)
    return result


def redact_response(entry: dict, *, secret: str) -> dict:
    """Return a redacted copy of a response trace entry.

    Redactions:
    - `headers[Set-Cookie]` cookie values → `***`
    - `body` literal `secret` occurrences → `***`
    """
    result = deepcopy(entry)
    if "headers" in result and isinstance(result["headers"], dict):
        result["headers"] = _redact_headers(result["headers"])
    if "body" in result:
        result["body"] = _redact_body(result["body"], secret)
    return result
