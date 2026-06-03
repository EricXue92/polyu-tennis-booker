# HTTP request capture script — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scripts/capture_http.py` — a one-shot tool that runs the existing Playwright booking flow while recording every PolyU HTTP request/response to `artifacts/http_trace.json`. This trace is the prerequisite for Phase 2 (writing `PolyUHttpClient`).

**Architecture:** A thin wrapper around `src.booker.login` + `prepare_search` + `submit_search` + `click_through` + `submit_and_resolve`. Attaches `page.on("request")` / `page.on("response")` hooks at the BrowserContext level, redacts the password and cookie values, and writes a JSON trace on exit. Independent of the live booker — does not run on CI, does not affect the production cron.

**Tech Stack:** Python 3.12+, Playwright async API (already in deps), stdlib `json` / `argparse` / `pathlib`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-03-http-replay-booking-design.md`

**Out of scope for this plan:** `src/http_client.py`, `src/http_booker.py`, dependency adds (httpx, respx), `parallel_runner.py` removal. Those land in Phase 2, written after the trace is captured.

---

## File structure

- Create: `scripts/capture_http.py` — CLI script: launches Playwright, runs booker phases with network hooks, dumps trace
- Create: `scripts/__init__.py` — empty, makes `scripts` importable so its helpers are unit-testable
- Create: `scripts/_trace_redaction.py` — pure helpers for redacting password / cookies in trace entries
- Create: `tests/test_trace_redaction.py` — offline unit tests for redaction helpers
- Modify: `CLAUDE.md` — add a bullet documenting capture_http.py next to the "Selectors are externalized" bullet

## Manual checkpoint after this plan

Once the script is merged, **the user must run it locally** with valid credentials targeting a known off-peak slot to produce `artifacts/http_trace.json`. That JSON is the input to the Phase 2 plan (which will be written after inspection).

---

## Task 1: Set up the scripts package

**Files:**
- Create: `scripts/__init__.py`

The existing `scripts/discover_selectors.py` is run as a standalone file, not imported. We need to import helpers from `scripts/` in `tests/`, so the directory needs to be a package.

- [ ] **Step 1: Create empty `scripts/__init__.py`**

```python
```

(empty file — just marks the directory as a Python package)

- [ ] **Step 2: Verify scripts directory is importable**

Run: `uv run python -c "import scripts; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add scripts/__init__.py
git commit -m "chore(scripts): make scripts directory a package"
```

---

## Task 2: Pure redaction helper — failing test

**Files:**
- Create: `tests/test_trace_redaction.py`

Two redaction concerns:
1. **Password in form bodies.** The login POST has `password=<actual>` in form-encoded body. Must become `password=***` in the trace.
2. **Cookie values in `Cookie` / `Set-Cookie` headers.** Session cookies are sensitive — they let anyone replay the user's PolyU session. Names stay, values become `***`.

The redaction is a pure function `redact(entry: dict, secret: str) -> dict` that takes one trace entry (request OR response) and returns a redacted copy. Tested before implemented.

- [ ] **Step 1: Write the failing test file**

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_trace_redaction.py -v`
Expected: ImportError or ModuleNotFoundError on `scripts._trace_redaction` — that module doesn't exist yet.

- [ ] **Step 3: Commit (test only, fails)**

```bash
git add tests/test_trace_redaction.py
git commit -m "test(capture): add failing tests for trace redaction"
```

---

## Task 3: Implement redaction helpers

**Files:**
- Create: `scripts/_trace_redaction.py`

- [ ] **Step 1: Write the module**

```python
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
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_trace_redaction.py -v`
Expected: 7 tests pass.

- [ ] **Step 3: Run the full test suite to confirm no regression**

Run: `uv run pytest`
Expected: all green (existing tests untouched).

- [ ] **Step 4: Commit**

```bash
git add scripts/_trace_redaction.py
git commit -m "feat(capture): add trace redaction helpers"
```

---

## Task 4: Capture script — argument parsing and skeleton

**Files:**
- Create: `scripts/capture_http.py`

The script accepts:
- `--target-date YYYY-MM-DD` (default: 7 days ahead, same as production)
- `--slot HH:MM-HH:MM` (e.g., `12:30-13:30`) — slot to try; should be a known off-peak slot the user actually wants to book
- `--no-submit` — stop before final Submit (still records cell-click / Next / checkbox requests; misses the Submit POST itself)
- `--headed` — run Playwright non-headless so the user can watch
- `--output PATH` (default: `artifacts/http_trace.json`)

It reads `POLYU_USERNAME` / `POLYU_PASSWORD` from env (same as `book-tennis`).

- [ ] **Step 1: Write the script with argparse + main shell (no Playwright logic yet)**

```python
"""Capture PolyU HTTP requests during a booking flow.

Runs the existing Playwright booker phases (login → prepare_search →
submit_search → click_through → optional submit_and_resolve) while
attaching network hooks to record every request and response to
`artifacts/http_trace.json`. The trace is the source of truth for the
Phase 2 PolyUHttpClient implementation.

Usage:
    POLYU_USERNAME=... POLYU_PASSWORD=... \\
        uv run python scripts/capture_http.py \\
            --slot 12:30-13:30 \\
            --headed

Add --no-submit to stop before the final Submit click (recording every
request up to the agreement checkbox tick but not the final POST). To
capture the Submit + success response, omit --no-submit and pick a
genuinely free off-peak slot (you will actually book it; cancel after
via PolyU's UI).
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date, datetime, timedelta
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-date",
        default=(date.today() + timedelta(days=7)).isoformat(),
        help="Target booking date YYYY-MM-DD (default: today + 7 days)",
    )
    parser.add_argument(
        "--slot",
        required=True,
        help="Slot to target, format HH:MM-HH:MM (e.g. 12:30-13:30). "
        "Must actually be free at run time.",
    )
    parser.add_argument(
        "--no-submit",
        action="store_true",
        help="Stop before final Submit. Records up to checkbox tick.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Playwright in headed mode (visible browser).",
    )
    parser.add_argument(
        "--output",
        default="artifacts/http_trace.json",
        help="Output path for the trace JSON (default: artifacts/http_trace.json)",
    )
    return parser.parse_args(argv)


def parse_slot(value: str) -> tuple:
    """Parse 'HH:MM-HH:MM' into (start_time, end_time)."""
    from datetime import time
    start_str, end_str = value.split("-")
    start = time.fromisoformat(start_str.strip())
    end = time.fromisoformat(end_str.strip())
    return start, end


async def main_async(args: argparse.Namespace) -> int:
    # Filled in by Task 5 + Task 6.
    raise NotImplementedError("Playwright wiring lands in Task 5")


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify argparse works**

Run: `uv run python scripts/capture_http.py --help`
Expected: help text prints, exit code 0.

Run: `uv run python scripts/capture_http.py --slot 12:30-13:30 --no-submit`
Expected: NotImplementedError (Playwright wiring deferred).

- [ ] **Step 3: Commit**

```bash
git add scripts/capture_http.py
git commit -m "feat(capture): add capture_http.py skeleton and CLI"
```

---

## Task 5: Wire Playwright + network hooks

**Files:**
- Modify: `scripts/capture_http.py`

We need to:
1. Launch Playwright with `headless` controlled by `--headed`.
2. Attach `context.on("request", ...)` and `context.on("response", ...)` so hooks see EVERY request in EVERY page in the context (login redirects, AJAX, etc).
3. Filter to the PolyU domain only (`www40.polyu.edu.hk`) — drop favicon, analytics, etc.
4. For each request, capture: URL, method, headers, post_data, timestamp. Assign a sequential `seq` so we can match request→response.
5. For each response, capture: URL, status, headers, body (full for non-image content types, omitted for binary), and the matching request `seq` (look up by `request.url + method` — Playwright also exposes `response.request` directly).
6. Redact via `redact_request` / `redact_response` before adding to the trace list.

- [ ] **Step 1: Replace `main_async` with the real implementation**

Replace the `raise NotImplementedError` line with:

```python
async def main_async(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    from scripts._trace_redaction import redact_request, redact_response
    from src.booker import (
        click_through,
        login,
        prepare_search,
        slot_has_availability,
        submit_and_resolve,
        submit_search,
    )
    from src.log import build_logger

    username = os.environ["POLYU_USERNAME"]
    password = os.environ["POLYU_PASSWORD"]
    log = build_logger("capture", secret=password)

    target_date = date.fromisoformat(args.target_date)
    start, end = parse_slot(args.slot)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    trace: list[dict] = []
    seq = 0

    def on_request(request) -> None:
        nonlocal seq
        if "www40.polyu.edu.hk" not in request.url:
            return
        seq += 1
        entry = {
            "kind": "request",
            "seq": seq,
            "ts": datetime.utcnow().isoformat() + "Z",
            "url": request.url,
            "method": request.method,
            "headers": dict(request.headers),
            "post_data": request.post_data,
        }
        trace.append(redact_request(entry, secret=password))

    async def on_response(response) -> None:
        if "www40.polyu.edu.hk" not in response.url:
            return
        ctype = response.headers.get("content-type", "")
        body: str | None
        if any(
            ctype.startswith(p)
            for p in ("text/", "application/json", "application/x-www-form-urlencoded")
        ):
            try:
                body = await response.text()
            except Exception as e:
                body = f"<could not read body: {e}>"
        else:
            body = f"<binary {ctype}, {response.headers.get('content-length', '?')} bytes>"
        entry = {
            "kind": "response",
            "seq": seq,
            "ts": datetime.utcnow().isoformat() + "Z",
            "url": response.url,
            "status": response.status,
            "headers": dict(response.headers),
            "body": body,
        }
        trace.append(redact_response(entry, secret=password))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        try:
            context = await browser.new_context()
            context.on("request", on_request)
            context.on("response", lambda r: asyncio.create_task(on_response(r)))
            page = await context.new_page()
            page.set_default_timeout(20_000)

            await login(page, username, password, log)
            await prepare_search(page, target_date, log)
            await submit_search(page, log)

            if not await slot_has_availability(page, target_date, start, end):
                log.error("slot %s-%s not available on %s — cannot capture",
                          start, end, target_date)
                # Still dump the partial trace — Search request/response was captured.
                _write_trace(trace, output_path, log)
                return 1

            await click_through(
                page, target_date, start, end,
                session_id="capture", log=log,
            )

            if args.no_submit:
                log.info("--no-submit set; skipping final Submit")
            else:
                result = await submit_and_resolve(
                    page, session_id="capture", log=log,
                )
                log.info("submit_and_resolve returned %s", result)
        finally:
            await browser.close()

    _write_trace(trace, output_path, log)
    return 0


def _write_trace(trace: list[dict], path: Path, log) -> None:
    import json
    path.write_text(json.dumps(trace, indent=2, ensure_ascii=False))
    log.info("wrote %d trace entries to %s", len(trace), path)
```

Add at the top of the file alongside the existing imports:

```python
from datetime import datetime, timedelta
```

(already there from Task 4 — no change needed).

- [ ] **Step 2: Verify the script still imports**

Run: `uv run python -c "import scripts.capture_http; print('ok')"`
Expected: `ok`

Run: `uv run python scripts/capture_http.py --help`
Expected: help text prints.

- [ ] **Step 3: Verify the script's static structure**

Run: `uv run python -c "from scripts.capture_http import main_async, parse_slot; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add scripts/capture_http.py
git commit -m "feat(capture): wire Playwright network hooks for HTTP trace"
```

---

## Task 6: Document the capture script in CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` — add a new bullet under "Architecture" near the "Selectors are externalized" bullet

The new bullet explains: what the script is for, when to run it, where output goes, prerequisites (off-peak slot, valid creds).

- [ ] **Step 1: Read the relevant CLAUDE.md section**

Run: `grep -n "Selectors are externalized" CLAUDE.md`
Expected: returns the line number — confirm it exists.

- [ ] **Step 2: Add the new bullet immediately after the "Selectors are externalized" paragraph**

Locate the bullet that starts with `- **Selectors are externalized.**` and ends with `is left as the PENDING_DISCOVERY sentinel.` Add the following bullet right after it (one blank line between):

```markdown
- **HTTP request shapes are externalized too.** `scripts/capture_http.py`
  is the HTTP analogue of `discover_selectors.py`: it runs the booking
  flow under Playwright with `context.on("request"/"response")` hooks
  and dumps `artifacts/http_trace.json` (passwords and cookie values
  redacted). Used by `src/http_client.py` to build/maintain the raw
  HTTP request templates that replace Playwright on the hot path.
  Re-run when PolyU changes its booking endpoints or form fields
  (symptom: `PolyUHttpClient` 4xxs or returns unexpected response
  shape). Requires a known free off-peak slot — see the comment block
  at the top of the script for usage.
```

Use the Edit tool to make the change atomically. The exact `old_string` to anchor on:

```
  left as the `PENDING_DISCOVERY` sentinel.
```

The exact `new_string`:

```
  left as the `PENDING_DISCOVERY` sentinel.

- **HTTP request shapes are externalized too.** `scripts/capture_http.py`
  is the HTTP analogue of `discover_selectors.py`: it runs the booking
  flow under Playwright with `context.on("request"/"response")` hooks
  and dumps `artifacts/http_trace.json` (passwords and cookie values
  redacted). Used by `src/http_client.py` to build/maintain the raw
  HTTP request templates that replace Playwright on the hot path.
  Re-run when PolyU changes its booking endpoints or form fields
  (symptom: `PolyUHttpClient` 4xxs or returns unexpected response
  shape). Requires a known free off-peak slot — see the comment block
  at the top of the script for usage.
```

- [ ] **Step 3: Verify the edit landed**

Run: `grep -n "HTTP request shapes are externalized" CLAUDE.md`
Expected: returns a line number — confirms the addition.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): document capture_http.py alongside selector discovery"
```

---

## Task 7: End-to-end smoke (NO actual Playwright launch)

**Files:**
- Create: `tests/test_capture_http_smoke.py`

We can't unit-test the Playwright bits offline. But we CAN verify the script's pure parts (argparse, slot parsing) and the redaction integration import chain — to catch any "module accidentally renamed" regression.

- [ ] **Step 1: Write the smoke test**

```python
"""Smoke tests for capture_http.py — verifies the script wires up
without launching Playwright. Live capture is exercised manually.
"""
from datetime import time

import pytest

from scripts.capture_http import parse_args, parse_slot


def test_parse_args_requires_slot():
    with pytest.raises(SystemExit):
        parse_args([])


def test_parse_args_defaults_target_date_to_today_plus_7():
    args = parse_args(["--slot", "12:30-13:30"])
    # Just check format and that it's parseable as a date.
    from datetime import date
    parsed = date.fromisoformat(args.target_date)
    assert parsed.year >= 2026  # sanity


def test_parse_args_accepts_full_flag_set():
    args = parse_args([
        "--slot", "17:30-18:30",
        "--target-date", "2026-06-10",
        "--no-submit",
        "--headed",
        "--output", "out/trace.json",
    ])
    assert args.slot == "17:30-18:30"
    assert args.target_date == "2026-06-10"
    assert args.no_submit is True
    assert args.headed is True
    assert args.output == "out/trace.json"


def test_parse_slot_basic():
    start, end = parse_slot("12:30-13:30")
    assert start == time(12, 30)
    assert end == time(13, 30)


def test_parse_slot_tolerates_whitespace():
    start, end = parse_slot("12:30 - 13:30")
    assert start == time(12, 30)
    assert end == time(13, 30)


def test_imports_resolve_without_playwright_launch():
    # Just importing main_async should not start a browser. This catches
    # accidental top-level Playwright launches if main_async is refactored.
    from scripts.capture_http import main_async
    assert callable(main_async)
```

- [ ] **Step 2: Run smoke tests**

Run: `uv run pytest tests/test_capture_http_smoke.py -v`
Expected: 6 tests pass.

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_capture_http_smoke.py
git commit -m "test(capture): smoke tests for capture_http.py CLI surface"
```

---

## Manual checkpoint — run the capture

After the above is merged, the user runs the script locally to produce the trace JSON. The Phase 2 plan is written based on what the trace shows.

Recommended capture procedure for the user (paste into chat when ready):

1. `gh workflow run "Daily Tennis Booking" -f dry_run=true -f skip_sleep=true` — confirm the existing booker is healthy on main.
2. On a weekday morning AFTER 08:30 HKT (slots for date+7 already released), inspect PolyU manually for a TRULY FREE off-peak slot (e.g., weekday 12:30-13:30 or 09:30-10:30 — confirm it shows as bookable in the UI and is NOT 17:30-20:30).
3. Run:
   ```bash
   POLYU_USERNAME=... POLYU_PASSWORD=... \
     uv run python scripts/capture_http.py \
       --slot 12:30-13:30 \
       --target-date 2026-06-10 \
       --headed
   ```
4. Watch the browser; confirm Submit lands successfully (PolyU returns the booking confirmation page).
5. Cancel the booking via PolyU's UI immediately (no court actually used).
6. Attach `artifacts/http_trace.json` to the next chat session.

## Self-review

**Spec coverage (against `2026-06-03-http-replay-booking-design.md`):**

| Spec item | Plan task |
|---|---|
| `scripts/capture_http.py` (new, one-shot tool) | Tasks 4 + 5 |
| Hooks on `page.on("request"/"response")` filtered to PolyU domain | Task 5 |
| Trace dump to `artifacts/http_trace.json` with redaction | Tasks 2 + 3 + 5 |
| Password redaction via the same approach as `log.py` filter | Tasks 2 + 3 |
| Cookie value redaction | Tasks 2 + 3 |
| Body truncation for binary, kept for text/JSON | Task 5 (Content-Type-based) |
| Documented in CLAUDE.md alongside `discover_selectors.py` | Task 6 |
| Phase 2 (`http_client.py`, `http_booker.py`, integration, `parallel_runner.py` removal) | **Deferred** — out of scope, requires trace inspection first |

The spec's Phase 2-5 are intentionally deferred; this plan implements only Phase 1 (Capture). A follow-up plan written after the trace is captured will cover the rest.

**Placeholder scan:** No "TODO" / "TBD" / "fill in" in the steps above. Every code block is complete.

**Type consistency:** `redact_request` and `redact_response` signatures match between Task 2 (test) and Task 3 (implementation). `parse_slot` returns `(time, time)` tuple, consumed correctly in Task 5. `main_async` signature matches Task 4 (skeleton) and Task 5 (implementation).

**One known soft spot:** the response-body truncation policy ("text/JSON keep whole, binary describe size") differs slightly from the spec wording ("HTML body truncated to ~10KB for inspection"). I made the trace keep full bodies — it's a local-only artifact, never uploaded by CI, and the user's local disk is fine with a few MB. If trace files balloon in practice, add truncation in the Phase 2 plan.
