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

A probe has **three** outcomes, not two (``ScaleBuildLockAttempt`` below):

* a handle — the claim is held by this caller;
* ``None`` — the claim is provably held by SOMEBODY ELSE (another thread, a
  service replica, the offline CLI). "Somebody is building it" is a true
  statement about the notebook, so a caller may report it as such and leave
  whatever queue entry it found alone;
* ``SCALE_BUILD_LOCK_UNAVAILABLE`` — the claim could not be EVALUATED: this
  process has spent its budget of dedicated lock sessions, or the probe itself
  failed. Nothing is known about who owns the notebook, so a caller must
  neither claim "already building" nor drop the work; it parks it and retries.

Collapsing the last two is what made a lock-session budget exhaustion report
itself as ``already_building`` while queueing nothing — an admission that both
lied and lost the request (codex W-CLI R1 P1-1).
"""
from __future__ import annotations

import secrets
import zlib
from typing import Optional, Protocol, Union, runtime_checkable


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
    # A random-hex identifier minted once, at acquisition, and stable for the
    # handle's whole lifetime. It makes ``{live}.tmp-{claim_token}`` unique
    # per claim (codex PR#643 R1 P1): a fixed shared staging path let a
    # lock-session-lost builder and the process that took over its claim
    # both reset and write the same directory. Every implementation carries
    # one, including ``UnsupportedScaleBuildLock`` — a caller with no real
    # lock still needs a token that cannot collide with a concurrent one.
    claim_token: str

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

    def __init__(self) -> None:
        # No cross-process claim exists to derive a session identity from
        # (and the offline CLI refuses SQLite outright, so this token never
        # protects a real race there); still random, not a fixed literal, so
        # nothing downstream can be tempted to treat it as a shared constant.
        self.claim_token = secrets.token_hex(8)

    def verify_held(self) -> bool:
        return True

    def release(self) -> None:
        return None


UNSUPPORTED_SCALE_BUILD_LOCK = UnsupportedScaleBuildLock()


class ScaleBuildLockUnavailable:
    """Sentinel: the claim could not be evaluated (see the module docstring).

    Deliberately NOT a :class:`ScaleBuildLock` — it has no ``verify_held`` and
    no ``release``, so a caller that forgets to discriminate fails loudly at the
    first use instead of silently building with a claim it never held.
    """

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "SCALE_BUILD_LOCK_UNAVAILABLE"


SCALE_BUILD_LOCK_UNAVAILABLE = ScaleBuildLockUnavailable()

# What every ``try_scale_build_lock`` implementation returns. The two failure
# values are distinguished by identity (``attempt is
# SCALE_BUILD_LOCK_UNAVAILABLE``), never by truthiness.
ScaleBuildLockAttempt = Optional[Union[ScaleBuildLock, ScaleBuildLockUnavailable]]


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
