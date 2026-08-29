"""Process-wide snapshot of the deployment plugins an admin switched off.

This module is the whole read side of the runtime toggle. It holds one
frozenset and a publish counter: no database handle, no thread, no import
outside the standard library, and — on the read path — no lock. That
austerity is the point: the registry consults it on *every* contribution and
capability evaluation, so a read has to cost a module-global load, and the
extension layer has to be able to reach it without ``app.extensions`` gaining
an edge into ``app.services``
(``scripts/check_architecture_boundaries.py`` keeps that edge at zero).
``app.core`` is the one package both the extension composition root and the
API layer already import, which is why the holder lives here rather than next
to the store that fills it.

Who fills it: the publishers are in ``app.services`` and ``app.bootstrap`` —
the composition root primes this snapshot once the repository is built, the
admin write path republishes immediately in its own process, and a low
frequency refresher converges the others. None of that is visible from here,
deliberately: this module cannot tell a primed process from a never-primed
one, and does not need to. What it *does* know is that those publishers race
each other, which is what the publish token below is for.

**Empty means everything is admitted.** A process that never publishes — a
test, a CLI that skips the composition root, a deployment with no toggle rows
— keeps the empty default forever and behaves exactly as it did before this
gate existed. "No row = enabled" in the table and "empty set = all admitted"
here are the same rule spelled twice, and both fail toward *enabled*: a
refresh that breaks leaves the last good snapshot in place rather than
disabling a plugin nobody asked to disable.
"""
from __future__ import annotations

import threading
from collections.abc import Set


# Rebound as a whole, never mutated in place. A reader's ``return _disabled``
# and a publisher's ``_disabled = ...`` are each a single bytecode op on the
# module dict, so a reader sees either the entire previous snapshot or the
# entire next one — never a half-applied one — with no lock on either side.
# That is why the value is a frozenset and not a set: an in-place ``add`` on a
# shared mutable set would give a concurrent reader no such guarantee, and
# would also let a caller keep mutating a snapshot it had already handed over.
_disabled: frozenset[str] = frozenset()

# Publish ordering. The read side stays lock-free — this lock is taken only by
# publishers, a few times a minute — and it exists because "who published
# last" is NOT the same question as "whose data is newest".
#
# Every publisher's work is read-then-publish, and the read is the slow part.
# Two orderings therefore come apart:
#
#   tick   ─── issues token ── SELECT (slow) ─────────────────── publish
#   admin        UPDATE ─ commit ─ issues token ─ SELECT ─ publish
#
# The tick's SELECT started before the admin's row was committed, so it cannot
# see the change; but it finishes *after* the admin's publish, and a naive
# last-writer-wins holder would let it undo the admin's switch for a whole
# interval. The same shape appears between two concurrent admin writes.
#
# A token issued BEFORE the read fixes the ordering, because token order is
# read-start order: if token(A) < token(B) then A's read began before B's was
# even issued, so B's data is at least as fresh. Applying only strictly
# increasing tokens therefore drops exactly the stale publishes and nothing
# else. A rejected publish is a normal race, not a fault: the newer snapshot
# it lost to is already in place, so there is nothing to retry or repair.
_publish_lock = threading.Lock()
_issued_token: int = 0
_applied_token: int = 0


def disabled_plugin_ids() -> frozenset[str]:
    """Plugin ids an administrator has disabled, as of this instant.

    Zero I/O and zero locking by contract — this is called from inside
    availability evaluation, which the extension layer keeps I/O-free.
    """

    return _disabled


def begin_publish() -> int:
    """Claim a publish slot. Call this BEFORE reading the toggle rows.

    The token's whole meaning is "my read started here". Taking it after the
    read — or not taking one at all — reintroduces exactly the race it exists
    to close, so a caller that reads from the database must call this first
    and pass the result to :func:`publish_disabled_plugin_ids`.
    """

    global _issued_token
    with _publish_lock:
        _issued_token += 1
        return _issued_token


def publish_disabled_plugin_ids(
    ids: Set[str], token: int | None = None
) -> bool:
    """Install ``ids`` as the snapshot every later read returns.

    Returns whether it was installed. ``False`` means a publisher whose data
    is newer already won; see the ordering note at the top of this module.

    ``token`` is optional, and the two forms mean different things:

    - **with a token** (from :func:`begin_publish`, taken before the read):
      "install this if nothing fresher has landed since I started reading".
      Every publisher that reads from the database uses this form.
    - **without one**: "this value is authoritative now" — a token is issued
      here, so it is by construction the newest and cannot be rejected. This
      is for callers holding a value that did not come from a racing read:
      tests arranging a snapshot, and any future caller synthesising one.

    The convenience form is the default rather than the exception because it
    is the only one that is safe to use without understanding the ordering
    rule; a caller who does not pass a token cannot get the ordering subtly
    wrong, only trivially right.

    Shape is checked *here*, on the cold path a publisher walks a few times a
    minute, rather than on the read path the registry walks per evaluation.
    The check is also the only place it can be loud: the gate's read side must
    fail open (a malformed snapshot cannot be allowed to disable plugins at
    random, nor to raise inside a request), so a wrong shape that got past
    this point would degrade *silently* to "nothing is disabled" — an admin's
    switch quietly doing nothing. Rejecting it at the publisher instead turns
    that into a stack trace pointing at the caller that has the bug.

    ``str`` is the accident this is really for: it is the one wrong argument
    that is iterable, so ``frozenset("corp.plugin")`` would build a set of
    single characters and disable nothing while looking like it worked. It is
    not a ``Set``, so it is rejected here. So are ``dict``, ``list``, and a
    generator (which would additionally be consumed by the check itself).

    ``frozenset(ids)`` then costs nothing when the argument already is one
    (CPython hands back the same object) and is what keeps a caller that
    passed a mutable ``set`` from mutating this module's state afterwards.
    """

    global _disabled, _issued_token, _applied_token
    if not isinstance(ids, Set):
        raise TypeError(
            "disabled plugin ids must be a set of str, not "
            f"{type(ids).__name__}"
        )
    # Values are excluded from the message on purpose: this runs in the
    # composition root and its log, and a plugin id is the operator's data,
    # not this module's to render.
    if any(type(plugin_id) is not str for plugin_id in ids):
        raise TypeError("disabled plugin ids must all be str")
    # Validation stays OUTSIDE the lock: it is pure, it can be slow-ish on a
    # large set, and a bad shape must raise whether or not this publish would
    # have won the ordering — a caller with the bug should hear about it even
    # on the tick where its value was going to be dropped anyway.
    snapshot = frozenset(ids)
    with _publish_lock:
        if token is None:
            _issued_token += 1
            token = _issued_token
        if token <= _applied_token:
            return False
        _disabled = snapshot
        _applied_token = token
        return True


def reset_for_tests() -> None:
    """Restore the empty default — i.e. every loaded plugin is admitted.

    Resets the publish ordering too, so tokens do not carry across the
    boundary a test suite draws between one test and the next. The one thing
    this cannot undo is a token already handed to a thread that is still
    running: it was issued above the reset floor and would still be applied.
    That is not a hazard worth designing around here — a test holding a live
    publisher across a reset has a bigger isolation problem than the token —
    but it is why the suite stops refresher threads before it resets.
    """

    global _disabled, _issued_token, _applied_token
    with _publish_lock:
        _disabled = frozenset()
        _issued_token = 0
        _applied_token = 0
