"""Request-local REPLACEMENT of the retrieval participant set.

⛔ This is a **replacement**, not a narrowing.  ``source_scope.py`` owns every
narrowing question -- which sources inside a library, which mounted libraries
stay checked -- and its whole module docstring is built on the rule that a
scope may only shrink what the mount table already returned.  An override here
says something that rule cannot express: "this run searches THESE libraries",
including libraries that are not mounted into the nominal active notebook at
all.  Global Ask needs exactly that (one user, eight of their own notebooks,
no mounts between them) and nothing else in the product does.  Folding the two
into one module would put a widening primitive behind a narrowing module's
docstring, which is the specific way a later reader gets it wrong.

An override does NOT bypass narrowing: the seat that consumes it still runs
``notebook_in_scope`` over the result, so the library checkboxes can shrink an
override but never widen it.

Why an override cannot leak into authorization
----------------------------------------------
``resolve_participants``/``mount_sql.py`` is the predicate that BOTH retrieval
and permission checks read (``source_scope.scoped_participants`` says so
verbatim: cross-library source proxying, citation resolution, asset reads).  A
per-request retrieval knob that could reach that predicate would be a
privilege escalation, so three independent layers keep it out -- each one is
sufficient on its own, and all three are pinned by
``backend/tests/test_participant_override_guard.py``:

1. **Single-reader whitelist.**  Only the retrieval consumption boundary may
   import this module (``retrieval_candidates`` / ``graph_retrieval`` /
   ``collection_catalog`` / ``collection_enumeration`` / ``communities``), and
   only ``global_ask`` may install an override.  The guard asserts the set of
   production importers is a subset of that whitelist, so a new reader is a
   deliberate, reviewed edit of the whitelist rather than an import someone
   added while wiring an unrelated feature.
2. **Authorization sites keep the real mount predicate.**  The guard names the
   files that answer "may this user read that library" (``source_routes``,
   ``mcp_tools/citations``, ``knowledge_query``, ``knowledge_lifecycle``,
   ``plugin_ask_engine``, ``repository_facade``, both backends'
   ``notebook_store``/``mount_sql``) and asserts twice over: they do not import
   this module, AND they still call the live predicate.  The second half is
   what stops the interesting failure -- rerouting one of them wholesale so it
   no longer asks the mount table at all, which a "does not import the
   override" assertion alone would happily accept.
3. **The override carries its own identity.**  ``attested_actor_id`` is the
   user whose ``can_read_many`` already passed.  ``resolve_retrieval_
   participants`` re-checks it against the ambient retrieval run's
   ``actor_id`` and RAISES on a mismatch.  ContextVars are copied into worker
   threads and detached workers outlive their request, so "another user's
   context leaked in" is a real shape; it must explode at first use rather
   than silently fall back to the mount table, because a silent fallback still
   returns a plausible answer and nobody ever finds out.

Fail-closed without a run
-------------------------
An override is only meaningful inside a retrieval run that carries an actor,
because the run is where the identity it must be checked against lives.  With
no ambient run there is nothing to attest against, so
``resolve_retrieval_participants`` raises instead of either trusting the
override unchecked or quietly returning the mount table.  Both alternatives
are worse: the first accepts an unverifiable claim, and the second turns a
wiring bug (an override installed outside its run, or a worker thread that
lost the run but kept the override) into a scope that merely looks smaller
than intended.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Iterator, Mapping, Optional, Sequence

from app.services.retrieval_run import current_retrieval_run


_DEFAULT_TIER = "personal"


class ParticipantOverrideError(RuntimeError):
    """An override was installed or used outside the contract above.

    Every message is content-free on purpose: these raise on identity
    mismatches, so the operands are a user id and a notebook id.  Naming them
    in an exception puts them in tracebacks, logs and error responses, which is
    the one place a scope-isolation failure must not start leaking the very
    identifiers it was protecting.  The failing call site is in the traceback
    already; the values are recoverable from the debugger, not from the log.
    """


@dataclass(frozen=True)
class ParticipantOverride:
    """The libraries one retrieval run searches, plus the identity that earned them.

    ``notebook_ids`` is deterministic and ``notebook_ids[0]`` is the NOMINAL
    active notebook -- nominal because with an override in place it holds no
    retrieval privilege whatsoever, it is just the id every downstream seat is
    keyed by (``retrieval_run``, ``source_scope_context``, trace).

    Validation happens at construction, not at use.  An override is built once
    by the writer and then read by every leg of a fan-out, so a malformed one
    would otherwise surface as N confusing failures deep inside worker threads
    instead of one at the point that built it.
    """

    notebook_ids: tuple[str, ...]
    tiers: Mapping[str, str]
    attested_actor_id: str

    def __post_init__(self) -> None:
        notebook_ids = tuple(str(value) for value in self.notebook_ids)
        if not notebook_ids:
            raise ParticipantOverrideError("participant override is empty")
        if len(set(notebook_ids)) != len(notebook_ids):
            # A duplicate would make one library pay for two fan-out legs and
            # double its weight in every "one row per participant" merge.
            raise ParticipantOverrideError(
                "participant override repeats a notebook id"
            )
        if not str(self.attested_actor_id):
            # An empty actor id would compare equal to the default `actor_id`
            # of a run that never set one, turning the attestation in
            # `resolve_retrieval_participants` into a no-op exactly when it
            # matters most.
            raise ParticipantOverrideError(
                "participant override has no attested actor"
            )
        # Copy before freezing: a frozen dataclass that stores the caller's
        # live dict is not frozen.  The caller keeps a reference and could
        # rewrite the tier map after attestation, which is a mutation of an
        # already-authorized object.  `MappingProxyType` over our own copy
        # closes both directions.
        object.__setattr__(self, "notebook_ids", notebook_ids)
        object.__setattr__(
            self,
            "tiers",
            MappingProxyType({str(k): str(v) for k, v in dict(self.tiers).items()}),
        )
        object.__setattr__(self, "attested_actor_id", str(self.attested_actor_id))

    @property
    def nominal_active_id(self) -> str:
        return self.notebook_ids[0]

    def pairs(self) -> tuple[tuple[str, str], ...]:
        """``(notebook_id, tier)`` in the declared order, missing tier -> personal.

        ``personal`` rather than ``base`` is the conservative default: ``base``
        is the tier a shared reference library carries, and guessing it for a
        notebook whose tier the writer did not state would describe a private
        notebook as a published one.
        """
        return tuple(
            (notebook_id, self.tiers.get(notebook_id, _DEFAULT_TIER))
            for notebook_id in self.notebook_ids
        )


_OVERRIDE: "ContextVar[ParticipantOverride | None]" = ContextVar(
    "retrieval_participant_override", default=None,
)


@contextmanager
def participant_override(override: ParticipantOverride) -> Iterator[None]:
    """Install one override for the duration of the block.

    Nesting is refused outright -- including a nested override that compares
    equal to the one already installed.  The reason to refuse the equal case
    too is that equality is a value test, not an identity test: two overrides
    live on the stack at once means two writers each believe they own this
    run's participant set, and whichever one exits first would restore a scope
    the other is still using.  There is exactly one writer by contract
    (``global_ask``), so nesting is a wiring bug in every form and is worth a
    loud failure rather than a silent shadow.

    The reset is in ``finally`` so an exception raised by the body -- the
    cancellation path above all -- cannot leave an override installed on a
    thread that goes back into a pool.
    """
    current = _OVERRIDE.get()
    if current is not None:
        raise ParticipantOverrideError(
            "participant override is already installed for this context"
        )
    token = _OVERRIDE.set(override)
    try:
        yield
    finally:
        _OVERRIDE.reset(token)


def current_participant_override() -> Optional[ParticipantOverride]:
    """The override installed for this context, or None."""
    return _OVERRIDE.get()


def federated_ask_active() -> bool:
    """Whether this run is in "participant-set mode" (= an override is in place).

    THE single predicate every AskService step asks when it needs to know
    whether it is answering for one notebook or for a set.  Deliberately not a
    separate flag: a flag and an override can disagree, and a step that reads
    the flag while another reads the override is how half a run ends up in
    each mode.
    """
    return _OVERRIDE.get() is not None


def resolve_retrieval_participants(
    active_notebook_id: str,
    fallback: Callable[[], Sequence[tuple[str, str]]],
) -> tuple[tuple[str, str], ...]:
    """The ``(notebook_id, tier)`` pairs this run may search.

    With no override this is ``fallback()`` -- the real mount predicate, byte
    for byte.  With one, it is the override's own set, after two checks that
    both raise rather than fall back (see the module docstring: a silent
    fallback returns a plausible answer and hides the bug forever).

    The actor check runs FIRST.  A run can fail both checks at once, and of
    the two, "this override belongs to a different user" is the one that must
    be the reported cause; reporting the notebook mismatch instead would
    describe a cross-user leak as a routing mistake.
    """
    override = _OVERRIDE.get()
    if override is None:
        return tuple(
            (str(notebook_id), str(tier)) for notebook_id, tier in fallback()
        )

    run = current_retrieval_run()
    if run is None:
        raise ParticipantOverrideError(
            "participant override used outside a retrieval run"
        )
    if str(run.actor_id) != override.attested_actor_id:
        raise ParticipantOverrideError(
            "participant override was attested for a different actor"
        )
    if str(active_notebook_id) != override.nominal_active_id:
        # An override authorizes one named set for one named nominal active.
        # Answering it for some other active id is how a second notebook's
        # retrieval -- a report leaf, a plugin engine, anything that resolves
        # participants for an id of its own -- would silently inherit this
        # run's set.
        raise ParticipantOverrideError(
            "participant override does not cover the requested active notebook"
        )
    return override.pairs()


def override_fingerprint(override: ParticipantOverride) -> str:
    """A stable, content-free digest of WHICH libraries an override names.

    Process-level caches (the federated relation graph, the PPR graph, the
    combined scale graph) key on the active notebook id alone.  With an
    override, one active id maps to several different participant sets, so
    that key would hand one request the graph built for another request's
    scope.  Appending this digest separates them.

    Sorted, so two overrides listing the same libraries in different orders
    share a cache entry -- the graphs keyed by it depend on membership, not on
    fan-out order.  BLAKE2s rather than ``hash()`` because the key has to be
    the same in every worker process (``hash()`` of a str is salted per
    process), and 8 bytes because this disambiguates ≤8-library combinations
    within one already-namespaced key, not a security boundary.
    """
    payload = "|".join(sorted(override.notebook_ids)).encode("utf-8")
    return hashlib.blake2s(payload, digest_size=8).hexdigest()


__all__ = [
    "ParticipantOverride",
    "ParticipantOverrideError",
    "current_participant_override",
    "federated_ask_active",
    "override_fingerprint",
    "participant_override",
    "resolve_retrieval_participants",
]
