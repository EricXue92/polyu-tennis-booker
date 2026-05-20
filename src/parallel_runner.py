"""Orchestrates N parallel booking sessions racing for the same target date.

Each session has three phases — prepare, click_through, submit. The first two
run concurrently across sessions. submit is serialized in priority rank order
by a single coordinator, so at most one session ever clicks Submit at a time.
The first SUCCESS sets a shared win event and stops the rest.
"""
from __future__ import annotations

import enum
from pathlib import Path

ARTIFACTS = Path("artifacts")


class BookingResult(enum.Enum):
    SUCCESS = enum.auto()
    OCCUPIED = enum.auto()
    ERROR = enum.auto()


def artifact_path(kind: str, session_id: str) -> Path:
    """Per-session screenshot path, e.g. artifact_path('pre_submit', 's0')."""
    return ARTIFACTS / f"{kind}_{session_id}.png"
