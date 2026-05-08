"""Logger with built-in secret redaction.

The booker passes the user's password into log messages indirectly (Playwright
errors can quote field values). We install a Filter that substitutes the
password with `***` before any handler sees the record.
"""
from __future__ import annotations

import logging
import sys


def redact(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "***")


class _RedactFilter(logging.Filter):
    def __init__(self, secret: str) -> None:
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secret:
            return True
        if isinstance(record.msg, str):
            record.msg = redact(record.msg, self._secret)
        if record.args:
            record.args = tuple(
                redact(a, self._secret) if isinstance(a, str) else a
                for a in record.args
            )
        return True


def build_logger(name: str, *, secret: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(_RedactFilter(secret))
    logger.addHandler(handler)
    logger.propagate = True  # let caplog capture in tests
    return logger
