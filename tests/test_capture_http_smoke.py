"""Smoke tests for capture_http.py — verifies the script wires up
without launching Playwright. Live capture is exercised manually.
"""
from datetime import time

import pytest

from scripts.capture_http import parse_args, parse_slot


def test_parse_args_works_without_slot():
    args = parse_args([])  # no flags
    assert args.slot is None


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
