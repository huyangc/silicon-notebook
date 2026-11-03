"""Bounded process-local invalidation for a freshly authenticated mode hint.

The database remains authoritative. This is one epoch and one active-write
count, not a user cache. A mode transaction invalidates old hints BEFORE any
I/O and advances again after commit or rollback. The mutex is never held over
database work. Overlapping writes have no stable epoch until all settle.

The server's existing single-worker/single-repository lifecycle is required.
Every new store invalidates the epoch too, so a copied request context from an
old database/runtime cannot certify a mode in its successor. Separate store
instances conservatively invalidate each other; no per-database/user map grows.
Offline merge/migration writers retain their documented stopped-service rule.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_lock = threading.Lock()
_epoch = 0
_writers = 0


def stable_mode_epoch() -> int | None:
    with _lock:
        return None if _writers else _epoch


def invalidate_mode_epoch() -> None:
    global _epoch
    with _lock:
        _epoch += 1


@contextmanager
def memory_mode_write() -> Iterator[None]:
    global _epoch, _writers
    with _lock:
        _epoch += 1
        _writers += 1
    try:
        yield
    finally:
        with _lock:
            _epoch += 1
            _writers -= 1
