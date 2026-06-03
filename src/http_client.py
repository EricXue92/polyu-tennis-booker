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
