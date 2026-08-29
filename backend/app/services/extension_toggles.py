"""Write side of the extension admission snapshot: prime it, then converge it.

``app.core.extension_admission`` holds the frozenset the registry gate reads on
every contribution/capability evaluation; it deliberately knows nothing about
where the value comes from. This module is that "where": one function that
reads the toggle store and publishes what it found, and one daemon thread that
calls that function on a low-frequency tick.

Two very different failure postures live here, and the difference is the whole
design:

- **Priming is loud.** ``refresh_extension_admission`` lets a store failure
  propagate. Its caller is the composition root, which has just built and
  migrated the database; if that read fails, something is wrong with the
  repository itself and a process that starts anyway would serve a snapshot it
  never actually read — an admin's "disabled" silently ignored until the next
  restart. Failing composition is the honest outcome.
- **Converging is quiet.** The refresher swallows everything and keeps the last
  good snapshot. A transient database hiccup a few seconds after startup must
  not take a serving process down, and must not flip plugins on or off at
  random either: "keep what we had" is both the safe direction (the snapshot is
  already correct as of the last successful read) and the one an operator can
  reason about.

Convergence is per-process and one-directional: this thread pulls, nothing
pushes. The process that *writes* a toggle republishes immediately in its own
memory (see the admin write path), so the operator who flipped the switch sees
it take effect at once; every other process notices within one tick. Offline
CLIs and batch jobs get the prime and nothing more — they are short-lived, and
a background thread reaching into a database an operator believes is quiesced
would be worse than a snapshot that is a few minutes old.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from app.core.extension_admission import (
    disabled_plugin_ids,
    publish_disabled_plugin_ids,
)
from app.repositories.ports import ExtensionToggleStorePort

logger = logging.getLogger("silicon_notebook.extension_admission")

# How long a stop may wait for an in-flight refresh to finish before giving up
# and leaving the (daemon, and by then already stop-flagged) thread to exit on
# its own. Bounded because the caller is a shutdown path that is about to close
# the connection pool this thread borrows from: waiting a moment avoids a
# spurious "query on a closing pool" error in the log, waiting forever would
# hang shutdown behind a wedged database.
_STOP_JOIN_TIMEOUT_SECONDS = 5.0


def _publish(disabled) -> frozenset[str]:
    """Install ``disabled`` and report the change once, if it is one."""

    previous = disabled_plugin_ids()
    publish_disabled_plugin_ids(disabled)
    published = disabled_plugin_ids()
    if published != previous:
        # Only on change, and count-only: an unchanged tick every few seconds
        # would drown the log, and plugin ids are the operator's data — the
        # admin audit row records who disabled what, this line only records
        # that this process has converged.
        logger.info(
            "extension admission snapshot updated: %d plugin(s) disabled",
            len(published),
        )
    return published


def refresh_extension_admission(
    store: ExtensionToggleStorePort,
) -> frozenset[str]:
    """Read the toggle rows and install them as the process-wide snapshot.

    Returns the set that is now live, so a caller that needs it (the admin
    write path wants to answer with what it just published) does not have to
    read the holder back.

    Store failures propagate on purpose — see the module docstring. Callers
    that must survive a failed read (only the refresher below) catch it there,
    where "keep the previous snapshot" is a decision rather than an accident.

    The refresher does NOT reuse this function: it needs to re-check its stop
    flag between the read and the publish (see ``_Refresher._tick``), which is
    a step this one-shot form has no reason to carry.
    """

    return _publish(store.extension_runtime_disabled_ids())


class _Refresher:
    """One daemon thread re-reading the toggle store until stopped."""

    def __init__(
        self, store: ExtensionToggleStorePort, interval_seconds: float
    ) -> None:
        self._store = store
        self._interval_seconds = interval_seconds
        self._stop_requested = threading.Event()
        self._started = False
        self._thread = threading.Thread(
            target=self._loop,
            name="extension-admission-refresh",
            daemon=True,
        )
        # Failure-log de-bouncing state, touched only by the refresher thread.
        # A database that is down stays down for many ticks; logging every one
        # of them at warning turns a single fault into thousands of lines and
        # buries whatever else the operator needs to read. So: warn on the
        # good→bad transition (with the traceback, which is the part that
        # actually diagnoses it), debug for every repeat, and one info line on
        # the bad→good transition saying how many ticks were lost. The pair of
        # transitions is what an operator greps for; the middle is noise.
        # Invalid-snapshot TypeErrors do NOT flow through here — see ``_loop``.
        self._consecutive_failures = 0

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        """Signal the thread and wait briefly. Idempotent, and safe to call on
        a refresher whose thread never started (a failed ``Thread.start``)."""

        self._stop_requested.set()
        if not self._started:
            return
        if self._thread is threading.current_thread():
            return  # never join self: a refresh cannot shut its own thread down
        self._thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            logger.warning(
                "extension admission refresher did not stop within %.1fs; "
                "it is a daemon thread and will exit on its own",
                _STOP_JOIN_TIMEOUT_SECONDS,
            )

    def _loop(self) -> None:
        # Wait FIRST: the composition root already primed the snapshot moments
        # ago, so an immediate refresh would be a redundant query on the
        # busiest part of startup. ``Event.wait`` returns True only when the
        # stop flag is set, which makes stop interrupt the sleep instead of
        # having to outlast it.
        while not self._stop_requested.wait(self._interval_seconds):
            try:
                self._tick()
            except TypeError:
                # Split out of the de-bounced channel below on purpose. That
                # channel exists for *environment* faults — a database that is
                # down floods the log with identical warnings and tells the
                # operator nothing new after the first. A TypeError here comes
                # from the holder's shape check, i.e. a store handing back
                # something that is not a set of str: a programming error in
                # our own code, in a snapshot nobody will ever converge on
                # until it is fixed. De-bouncing that to debug would hide it
                # completely after the first tick, which is exactly backwards
                # — so every occurrence is an error with its traceback. The
                # thread stays alive because the fault is repairable from
                # outside (a corrected store) and a retired refresher would
                # then leave the process frozen on a stale snapshot forever.
                self._note_programming_error()
            except Exception:
                self._note_failure()
            else:
                self._note_success()

    def _tick(self) -> None:
        """One read, then one publish — unless this refresher retired between.

        The re-check is the whole reason the loop does not just call
        ``refresh_extension_admission``. ``stop`` waits a bounded time; a read
        stuck on a slow query can outlast that wait and return afterwards, by
        which point a *successor* refresher (a retried lifecycle, a second
        lifespan) may already have published a newer snapshot. Publishing then
        would roll the process back to a value up to one interval old, with no
        further tick from this dead thread to correct it. Losing the read is
        the cheap outcome; overwriting a live snapshot is not.
        """

        disabled = self._store.extension_runtime_disabled_ids()
        if self._stop_requested.is_set():
            logger.debug(
                "extension admission refresh discarded: refresher retired "
                "while its read was in flight"
            )
            return
        _publish(disabled)

    def _note_programming_error(self) -> None:
        # Deliberately does not touch ``_consecutive_failures``: this is not
        # the outage the de-bounce counts, and it is not a recovery from one
        # either. An outage that is still running keeps its count so the
        # eventual recovery line stays truthful.
        logger.error(
            "extension admission refresh produced an invalid snapshot; "
            "keeping the last one (%d plugin(s) disabled)",
            len(disabled_plugin_ids()),
            exc_info=True,
        )

    def _note_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures == 1:
            logger.warning(
                "extension admission refresh failed; keeping the last "
                "snapshot (%d plugin(s) disabled)",
                len(disabled_plugin_ids()),
                exc_info=True,
            )
        else:
            logger.debug(
                "extension admission refresh still failing (%d consecutive)",
                self._consecutive_failures,
            )

    def _note_success(self) -> None:
        if self._consecutive_failures:
            logger.info(
                "extension admission refresh recovered after %d consecutive "
                "failure(s)",
                self._consecutive_failures,
            )
            self._consecutive_failures = 0


# The process owns at most one refresher. It is module state rather than
# something the caller holds because the shutdown paths that must stop it
# (``startup_warmup.close_repository``, and every defensive early return in
# ``run_startup``) do not all have the handle ``start`` returned.
_lock = threading.Lock()
_active: _Refresher | None = None


def start_extension_admission_refresher(
    store: ExtensionToggleStorePort, interval_seconds: float
) -> Callable[[], None]:
    """Start the convergence thread and return its (idempotent) stop callback.

    A second start **replaces** the first rather than being rejected: the only
    way to reach it is a new repository lifecycle in this process (a retried
    startup, a test driving two lifespans), and in that situation the previous
    refresher holds a store belonging to a repository whose pool is being — or
    has been — closed. Refusing the new start would leave that dead refresher
    as the process's only one; erroring would turn a recoverable retry into a
    hard failure. Stopping the old one first is the only option that leaves a
    live refresher pointing at the live repository.

    "Stopped" is not the same as "gone": the join is bounded, so a predecessor
    wedged on a slow read can still be running when this returns. It cannot
    affect anything — ``_tick`` re-checks the stop flag before it publishes,
    so a retired thread's late read is discarded rather than overwriting this
    refresher's snapshot — but a thread dump during an outage may legitimately
    show two.

    The returned callback stops *this* refresher only. Calling a stale one
    after a replacement is a no-op, so a caller cannot accidentally shut down
    a successor it does not know about.
    """

    if not interval_seconds > 0:
        # A zero or negative interval is a spin loop against the database, not
        # a fast refresher. Settings pins the deployment-facing floor at 1s;
        # this guard covers direct callers.
        raise ValueError(
            f"refresh interval must be positive, got {interval_seconds!r}"
        )
    global _active
    refresher = _Refresher(store, float(interval_seconds))
    with _lock:
        previous, _active = _active, None
        if previous is not None:
            previous.stop()
        # Installed only after the thread is actually running, so a failed
        # ``Thread.start`` leaves the process with no refresher rather than
        # with a handle to one that will never tick.
        refresher.start()
        _active = refresher

    def stop() -> None:
        global _active
        with _lock:
            if _active is refresher:
                _active = None
        refresher.stop()

    return stop


def stop_extension_admission_refresher() -> None:
    """Stop whatever refresher this process owns. Idempotent; no-op if none."""

    global _active
    with _lock:
        refresher, _active = _active, None
    if refresher is not None:
        refresher.stop()
