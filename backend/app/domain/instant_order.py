"""Exact read-side instant keys shared by Python cursors and SQLite.

This never rewrites a stored timestamp. Naive values mean UTC, like the
existing cursor contract; invalid/null values have no comparable SQL key.
Integer arithmetic retains the full datetime/PostgreSQL microsecond precision
without Julian-day rounding or floating-point epoch conversion.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECOND = timedelta(microseconds=1)


def utc_microsecond_key(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed.astimezone(timezone.utc) - _EPOCH) // _MICROSECOND
    except (ValueError, TypeError, OverflowError):
        return None
