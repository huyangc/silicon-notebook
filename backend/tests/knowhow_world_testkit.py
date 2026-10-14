"""Helpers shared by the knowhow test files that copy a seeded world per test.

Not a test module (pytest only collects ``test_*.py``). The worlds themselves
come from ``world_templates``; this holds the two knowhow-specific pieces every
builder and fixture needs.
"""
from __future__ import annotations

import threading
import time


def quiesce_knowhow_world(timeout: float = 10.0) -> None:
    """Wait until nothing is still projecting. A world is copied as bytes, so
    a projection run, a debounce timer or the orphan-asset sweep job still in
    flight would land in the template AFTER it was frozen (or in a closed
    database). Same condition the autouse teardown in conftest enforces."""
    from app.services import background_jobs
    from app.services.knowhow import api as knowhow_api

    deadline = time.monotonic() + timeout
    while True:
        busy = False
        for scheduler in list(knowhow_api._SCHEDULERS.values()):
            with scheduler._lock:
                busy = busy or bool(
                    scheduler._running or scheduler._timers or scheduler._rerun
                )
        if not busy:
            break
        assert time.monotonic() < deadline, "knowhow projection never went idle"
        time.sleep(0.01)
    background_jobs._drain_maintenance_executors_for_tests(timeout=timeout)
    # SQLite connections are per thread, and ``repository.close()`` only closes
    # the calling thread's: the pool's worker threads still hold theirs inside
    # the world. Stop the pools and wait for their threads to exit (the same
    # reset conftest does after every test) so the world can be proven closed.
    background_jobs._reset_maintenance_gate_for_tests()
    deadline = time.monotonic() + timeout
    while any(t.name.startswith("background-") for t in threading.enumerate()):
        assert time.monotonic() < deadline, "background workers never exited"
        time.sleep(0.01)


def world_payload(client, fixture_name: str):
    """The payload ``client`` was opened with. ``client`` decides whether to
    copy a world from the test's static fixture closure (``request.fixturenames``),
    so a world fixture fetched dynamically (``request.getfixturevalue``) would
    find a fresh empty database instead; say so rather than failing obscurely."""
    payload = getattr(client, "_world_payload", None)
    assert payload is not None, (
        f"`{fixture_name}` must be requested statically (a test parameter or "
        "usefixtures), not via request.getfixturevalue: `client` opens the "
        "world copy only when it sees the fixture in the test's declared closure"
    )
    return payload
