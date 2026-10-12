"""Clock helpers shared by tests that would otherwise be time bombs.

Two kinds of test go red purely because the calendar moved on:

* a literal instant fed to a store whose readers filter against the REAL clock
  with no seam (retained user activity expires ``user_activity_retention_days``
  after the injected deletion instant, and SQLite ``julianday('now')`` /
  PostgreSQL ``CURRENT_TIMESTAMP`` decide what is still visible).  Such tests
  take their instants from :func:`recent_datetime` / :func:`recent_instant`,
  relative to the real clock, so they mean the same thing on every day;
* "today" computed in the test while the code under test reads its own
  ``datetime.now()`` (per-day log files): a run that crosses midnight looks for
  the wrong day.  Such tests replace the module's ``datetime`` with
  :class:`FrozenDatetime`, so writer and reader agree on :data:`FROZEN_DAY`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: The day :class:`FrozenDatetime` reports, deliberately far from any real run
#: date so that code which stopped reading the patched clock goes red.
FROZEN_DAY = "2026-01-15"


class FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` is always noon on :data:`FROZEN_DAY`."""

    @classmethod
    def now(cls, tz=None):
        return datetime.fromisoformat(f"{FROZEN_DAY}T12:00:00").replace(tzinfo=tz)


def recent_datetime(*, days: int = 0, hours: int = 0) -> datetime:
    """The real UTC clock (whole seconds) minus ``days`` and ``hours``."""
    instant = datetime.now(timezone.utc).replace(microsecond=0)
    return instant - timedelta(days=days, hours=hours)


def recent_instant(*, days: int = 0, hours: int = 0) -> str:
    """:func:`recent_datetime` as an ISO-8601 string with a ``+00:00`` offset."""
    return recent_datetime(days=days, hours=hours).isoformat()
