"""The one-day window between "the owner deleted this notebook" and the
physical delete (product decision 2026-10-21, rulings B1-B12 of 2026-10-22;
spec ``docs/superpowers/specs/2026-10-22-notebook-delete-grace-window_zh.md``).

``DELETE /api/notebooks/{id}`` moves the row ``live -> pending_delete`` (T1)
and stamps ``delete_after = now + NOTEBOOK_DELETE_GRACE_SECONDS``. From that
moment the notebook is invisible to everyone: ``pending_delete`` is one of the
values ``access_sql.NOTEBOOK_LIVE_SQL`` lists, so every read, write and share
path treats it as missing. Five dedicated exceptions stay open until the window
closes:

1. the caller exports their own Memory of the notebook;
2. the owner cancels (T2, ``pending_delete -> status_before_delete``);
3. the notice projection (``GET /api/me/notebook-deletions`` and the bell
   entry) for the owner and every reader holding Memory there;
4. ``GET /notebooks/{id}`` answers a reader 404 with an explanation code;
5. the caller moves their own confirmed Memory to another notebook they own.

When ``delete_after`` passes, the delete sweep's driver C moves the row
``pending_delete -> deleting`` and inserts its delete job in the same
transaction (T3); the six-phase delete then runs unchanged. All three
transitions are compare-and-set on the row, so a cancel racing the promotion
lands exactly one of them.

Pure leaf module: constants and instant arithmetic, no I/O.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# The window length. A named constant, deliberately not a deployment setting
# (ruling B2): the interface promises "一天", and every client reads the actual
# deadline from the server's ``delete_after``.
NOTEBOOK_DELETE_GRACE_SECONDS = 86_400

# Machine-readable codes of the window's HTTP answers (``detail.code``).
# A repeated DELETE inside the window, and the reader-facing 404 of
# ``GET /notebooks/{id}``.
NOTEBOOK_PENDING_DELETE_CODE = "notebook_pending_delete"
# A cancel that arrived after the deadline (or after the promotion).
NOTEBOOK_DELETE_DUE_CODE = "notebook_delete_due"
# An export / transfer out of a notebook whose window has closed.
DELETE_WINDOW_CLOSED_CODE = "delete_window_closed"


# The window's status value (``access_sql.NOTEBOOK_PENDING_DELETE_STATUS`` on
# both backends spells the same string; pinned equal by
# ``tests/test_notebook_delete_grace.py``).
PENDING_DELETE_STATUS = "pending_delete"

# Answers of ``classify_delete_window``.
WINDOW_LIVE = "live"
WINDOW_OPEN = "open"
WINDOW_CLOSED = "closed"


def classify_delete_window(row: Any) -> str | None:
    """Where one notebook stands for the two Memory exits (export, transfer)
    of one caller, from a row carrying ``status`` and the three booleans
    ``readable`` (read predicate without the lifecycle term), ``live``
    (``NOTEBOOK_LIVE_SQL``) and ``window_open`` (window predicate at the
    caller's instant):

    * ``None`` -- missing, unreadable, or copying / importing: a plain 404;
    * ``"live"`` -- an ordinary notebook;
    * ``"open"`` -- inside its window: the exits are open;
    * ``"closed"`` -- the deadline passed (promoted or not): 404
      ``delete_window_closed``. Once phase 3 has removed the caller's
      membership the row stops being readable and this degrades to ``None``.
    """
    if row is None or not row["readable"]:
        return None
    if row["live"]:
        return WINDOW_LIVE
    if row["window_open"]:
        return WINDOW_OPEN
    if row["status"] in (PENDING_DELETE_STATUS, "deleting"):
        return WINDOW_CLOSED
    return None


def utc_instant(value: Any) -> datetime:
    """An aware UTC ``datetime`` for a clock reading or a stored instant.

    Accepts what the two backends hand out: a ``datetime`` (PostgreSQL
    ``timestamptz``; a naive one is read as UTC) or an ISO-8601 string (the
    SQLite clock seam writes the host's local offset; a bare string is read as
    UTC, the same rule ``app.core.activity_time`` applies)."""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def delete_after_for(requested_at: Any) -> datetime:
    """The instant a window opened at ``requested_at`` closes."""
    return utc_instant(requested_at) + timedelta(
        seconds=NOTEBOOK_DELETE_GRACE_SECONDS
    )


def instant_text(value: Any) -> str:
    """The wire form of a stored instant: aware UTC ISO-8601 (``+00:00``).
    Both backends answer the same string for the same instant."""
    return utc_instant(value).isoformat()


def stored_instant_text(value: Any) -> str:
    """The SQLite storage and comparison form of a window instant: UTC with
    the microseconds always written (``2026-10-23T08:00:00.000000+00:00``).
    Fixed width and one offset make byte order time order, so the window
    predicate compares ``delete_after`` with a bound instant exactly as
    PostgreSQL compares its ``timestamptz`` -- the same SQL text on both
    backends (``access_sql`` pins the two predicate surfaces equal) -- and
    the partial index serves the range. Every writer and every bound value of
    the window columns goes through this function."""
    return utc_instant(value).isoformat(timespec="microseconds")
