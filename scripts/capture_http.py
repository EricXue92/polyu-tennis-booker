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
