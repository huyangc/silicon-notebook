"""Per-notebook scale-build lock: handle contract, sentinel and key hashing.

The lock exists so an **offline build process can run beside a live service**
against the same database and artifact tree.  Only the PostgreSQL adapter can
provide it (session advisory locks); SQLite deployments are single-process by
construction and get the explicit UNSUPPORTED sentinel below rather than a
silent no-op — a no-op would read as "lock granted" to the offline CLI and let
two writers race on the same ``{scale_dir}.tmp``.

Ownership crosses threads (an admitting thread acquires, the worker it spawns
releases), so this is deliberately NOT a context manager: the handle is entered
and exited by hand and every exit path must either hand the handle to the
worker or release it.
"""
from __future__ import annotations

import zlib
from typing import Protocol, runtime_checkable


class ScaleBuildLockLost(RuntimeError):
    """Re-verification before a destructive swap found the lock gone.

    Raised from the artifact store *before* the first rename, so the live
    artifact is untouched and the staged ``.tmp`` is left on disk for the
    operator to inspect or delete.
    """


class ScaleBuildBusy(RuntimeError):
    """Another process (or thread) already holds this notebook's build lock."""


class ScaleBuildAlreadyBuilding(ScaleBuildBusy):
    """This process already has an in-flight build/fold for the notebook."""


@runtime_checkable
class ScaleBuildLock(Protocol):
    """An acquired per-notebook build claim."""

    supported: bool

    def verify_held(self) -> bool:
        """Whether the claim is still provably held, re-read from the source.

        Never raises: an unusable lock session is reported as *not held* so the
        caller fails closed on the destructive step.
        """
        ...

    def release(self) -> None:
        """Release the claim. Idempotent; never raises."""
        ...


class UnsupportedScaleBuildLock:
    """Sentinel for backends without a cross-process lock (SQLite).

    ``supported`` is the discriminator: the offline CLI refuses to run against
    such a deployment, while the serving process falls back to its in-process
    ``building`` claim, which is the whole mutex it ever had there.
    """

    supported = False

    def verify_held(self) -> bool:
        return True

    def release(self) -> None:
        return None


UNSUPPORTED_SCALE_BUILD_LOCK = UnsupportedScaleBuildLock()


def advisory_lock_key(value: str) -> int:
    """Hash an identifier into PostgreSQL's signed int32 advisory-key range.

    Same normalization the projection lock uses.  The two-argument advisory
    form keeps namespace and key in separate ``pg_locks`` columns, so an
    external prober reads them directly instead of re-splitting a 64-bit key
    whose high bit makes the value negative (roughly half of all identifiers).
    """
    key = zlib.crc32(value.encode("utf-8"))
    if key >= 2**31:
        key -= 2**32
    return key


def advisory_lock_oid(key: int) -> int:
    """Render a signed int32 advisory key the way ``pg_locks`` stores it.

    ``pg_locks.classid`` / ``objid`` are ``oid`` (unsigned), so a negative key
    appears there as its two's-complement value.  Self-verification compares
    against this rendering, never against the signed key.
    """
    return key & 0xFFFFFFFF
