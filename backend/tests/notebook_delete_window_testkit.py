"""Test helper: run a notebook deletion past its one-day window.

``DELETE /api/notebooks/{id}`` opens the deletion window
(``app.domain.notebook_delete_grace``); the delete job only starts once the
delete sweep's driver C promotes the due window. Tests that delete through
the HTTP route and then assert what the finished delete leaves call
``close_delete_windows`` to run one sweep tick with a clock past every window
opened now -- the production promotion path, not a shortcut around it -- and
then drain the background jobs as before.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.domain.notebook_delete_grace import NOTEBOOK_DELETE_GRACE_SECONDS


def after_the_window() -> datetime:
    """An instant past any deletion window opened up to now."""
    return datetime.now(timezone.utc) + timedelta(
        seconds=NOTEBOOK_DELETE_GRACE_SECONDS + 60
    )


def close_delete_windows(runtime: Any) -> int:
    """One sweep tick past every open window: each due window is promoted and
    its delete job submitted. Returns how many jobs were submitted."""
    return runtime.notebook_delete.sweep_once(now=after_the_window())
