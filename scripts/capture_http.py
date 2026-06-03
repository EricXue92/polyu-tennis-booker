"""Capture PolyU HTTP requests during login and make_book.do navigation.

Logs in via Playwright and navigates to make_book.do with network hooks
attached, dumping every request/response to `artifacts/http_trace.json`.
Used to re-discover the post-login CSRFToken/fbUserId shape if PolyU
updates its HTML — the full booking-flow HTTP shapes (timetable.json,
make_book.do POST, make_book_submit.do POST) are already baked into
`src/http_client.py` from the original Phase 1 capture.

Usage:
    POLYU_USERNAME=... POLYU_PASSWORD=... \\
        uv run python scripts/capture_http.py \\
            --headed

NOTE (Phase 2b): The --slot flag is retained for argparse backwards
compatibility but is unused. If you need to re-capture the full booking
flow (e.g. PolyU changed the make_book.do POST shape), do it manually
via Chrome DevTools → Network → Save All As HAR rather than extending
this script.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date, datetime, timedelta, timezone
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
    from playwright.async_api import async_playwright

    from scripts._trace_redaction import redact_request, redact_response
    from src.booker import login
    from src.config import MAKE_BOOK_URL
    from src.log import build_logger

    username = os.environ["POLYU_USERNAME"]
    password = os.environ["POLYU_PASSWORD"]
    log = build_logger("capture", secret=password)

    target_date = date.fromisoformat(args.target_date)
    # Parse the --slot arg even though we don't use it — validates the format
    # so users get a clear error early.
    _start, _end = parse_slot(args.slot)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    trace: list[dict] = []
    seq = 0
    seq_by_request: dict[int, int] = {}

    def on_request(request) -> None:
        nonlocal seq
        if "www40.polyu.edu.hk" not in request.url:
            return
        seq += 1
        seq_by_request[id(request)] = seq
        entry = {
            "kind": "request",
            "seq": seq,
            "ts": datetime.now(timezone.utc).isoformat(),
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
            "seq": seq_by_request.get(id(response.request), -1),
            "ts": datetime.now(timezone.utc).isoformat(),
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
            # Navigate to make_book.do so the trace captures its HTML (which
            # contains CSRFToken + fbUserId for any future HTTP re-discovery).
            log.info("navigating to make_book.do to capture its HTML")
            await page.goto(MAKE_BOOK_URL, wait_until="domcontentloaded", timeout=20_000)
            if args.no_submit:
                log.info("--no-submit set; nothing further to capture")
            else:
                log.warning(
                    "Phase 2b: capture_http no longer drives the full booking flow. "
                    "If you need a fresh booking trace (e.g. PolyU shape changed), capture "
                    "manually via Chrome DevTools → Network → Save All As HAR."
                )
        finally:
            await browser.close()

    _write_trace(trace, output_path, log)
    return 0


def _write_trace(trace: list[dict], path: Path, log) -> None:
    import json
    path.write_text(json.dumps(trace, indent=2, ensure_ascii=False))
    log.info("wrote %d trace entries to %s", len(trace), path)


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
