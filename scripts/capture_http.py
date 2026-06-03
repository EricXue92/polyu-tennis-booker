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


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
