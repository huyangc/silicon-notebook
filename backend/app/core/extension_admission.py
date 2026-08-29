"""Process-wide snapshot of the deployment plugins an admin switched off.

This module is the whole read side of the runtime toggle. It holds one
frozenset and nothing else: no database handle, no lock, no thread, no
import outside the standard library. That austerity is the point — the
registry consults it on *every* contribution and capability evaluation, so a
read has to cost a module-global load, and the extension layer has to be able
to reach it without ``app.extensions`` gaining an edge into ``app.services``
(``scripts/check_architecture_boundaries.py`` keeps that edge at zero).
``app.core`` is the one package both the extension composition root and the
API layer already import, which is why the holder lives here rather than next
to the store that fills it.

Who fills it: the publishers are in ``app.services`` and ``app.bootstrap`` —
the composition root primes this snapshot once the repository is built, the
admin write path republishes immediately in its own process, and a low
frequency refresher converges the others. None of that is visible from here,
deliberately: this module cannot tell a primed process from a never-primed
one, and does not need to.

**Empty means everything is admitted.** A process that never publishes — a
test, a CLI that skips the composition root, a deployment with no toggle rows
— keeps the empty default forever and behaves exactly as it did before this
gate existed. "No row = enabled" in the table and "empty set = all admitted"
here are the same rule spelled twice, and both fail toward *enabled*: a
refresh that breaks leaves the last good snapshot in place rather than
disabling a plugin nobody asked to disable.
"""
from __future__ import annotations

from collections.abc import Set


# Rebound as a whole, never mutated in place. A reader's ``return _disabled``
# and a publisher's ``_disabled = ...`` are each a single bytecode op on the
# module dict, so a reader sees either the entire previous snapshot or the
# entire next one — never a half-applied one — with no lock on either side.
# That is why the value is a frozenset and not a set: an in-place ``add`` on a
# shared mutable set would give a concurrent reader no such guarantee, and
# would also let a caller keep mutating a snapshot it had already handed over.
_disabled: frozenset[str] = frozenset()


def disabled_plugin_ids() -> frozenset[str]:
    """Plugin ids an administrator has disabled, as of this instant.

    Zero I/O and zero locking by contract — this is called from inside
    availability evaluation, which the extension layer keeps I/O-free.
    """

    return _disabled


def publish_disabled_plugin_ids(ids: Set[str]) -> None:
    """Install ``ids`` as the snapshot every later read returns.

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

    global _disabled
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
    _disabled = frozenset(ids)


def reset_for_tests() -> None:
    """Restore the empty default — i.e. every loaded plugin is admitted."""

    global _disabled
    _disabled = frozenset()
