"""The chunk recall lane's per-task context — a dependency-free leaf.

Two modules drive the same chunk producers and therefore need the same two
context variables and the same routing probe: ``retrieval_candidates`` (the
producers themselves, plus the single-library ``_retrieve_chunks_multi``
fan-out) and ``chunk_federation`` (the cross-library fan-out).  Keeping these
three symbols in either of those modules would force the other to import it,
and since ``retrieval_candidates`` calls ``chunk_federation`` for the federated
lane while ``chunk_federation`` drives ``retrieval_candidates``' producers,
that is a genuine import cycle rather than a layering accident.

So they live here instead: this module imports nothing from the service layer
and must stay that way.  ``retrieval_candidates`` re-exports all three under
their historical names, so every existing reader (``rc._CHUNK_PEEK_ONLY`` in
the tests included) keeps working unchanged.
"""
from __future__ import annotations

import contextvars
from typing import Optional


def _lexical_gate_drift_probe(retrieval_state, notebook_id: str) -> bool:
    """Once-per-arm-entry wrapper around ``_unsafe_source_scope_restricted``
    for the lexical-gate ROUTING callers (``_retrieve_chunks_multi``,
    ``_retrieve_chunks_baseline``, ``_keyword_chunk_candidates``, and the
    federated task table) — codex #640 R2 P2.  A module-level function, not a
    method, and doubly ``getattr``-guarded (missing attribute AND non-callable):
    production ``retrieval_state`` always has the real probe, but a couple of
    existing tests invoke one of those methods unbound against a bare
    ``SimpleNamespace``/adapter double standing in for ``self`` that has no
    ``_RetrievalState`` surface at all (see
    ``test_multi_query_native_cancellation_is_not_swallowed`` — a method lookup
    like ``self._lexical_gate_drift_probe`` would itself raise
    ``AttributeError`` on that double, which is why this lives at module scope
    instead). Such a double answers "no drift", the same as the pre-#640-R2
    baseline for every caller that never reaches this branch. This does NOT
    memoise the probe itself — every call here still re-reads it fresh; it
    only centralizes the fallback for callers that may not have it at all.

    codex #640 R3 P2: fail-open on ANY exception the probe itself raises, not
    just a missing/non-callable attribute.  Two of this wrapper's call
    sites (``_retrieve_chunks_multi``, ``_keyword_chunk_candidates``) invoke it
    OUTSIDE their own fail-open ``try/except`` block — it runs once, before
    the multi-query fan-out or before the FTS ``try`` further down — so an
    unguarded probe exception there would propagate past this ROUTING-only
    verdict and take the whole chunk/keyword arm (and therefore the ask) down
    with it, exactly the failure mode every other lexical helper in
    ``retrieval_candidates`` (``_lexical_corpus_langs``,
    ``_lexical_object_hits``) already refuses to allow. "No drift" is the safe
    answer on failure for the same reason the missing-probe branch above
    already answers it that way: it routes to the SAME lane an unprobed/absent
    probe already takes (the pre-#640-R2 baseline), never disables the
    enforcement predicate itself (that is pushed down unconditionally
    regardless of this verdict — see ``_lexical_gate_source_scoped``), and can
    only ever pick the wrong lexical TERM SET for this one call, never let an
    out-of-scope row through.  The diagnostic event is best-effort: a double
    with no usable ``event_log`` (the same ``SimpleNamespace`` this function
    already tolerates above) must not turn a swallowed probe failure into a
    new, unswallowed emit failure.
    """
    probe = getattr(retrieval_state, "_unsafe_source_scope_restricted", None)
    if not callable(probe):
        return False
    try:
        return bool(probe(notebook_id))
    except Exception as exc:  # noqa: BLE001 — a routing-only probe must never break retrieval
        emit = getattr(getattr(retrieval_state, "event_log", None), "emit", None)
        if callable(emit):
            try:
                emit({
                    "kind": "lexical_gate_probe_failed",
                    "notebook_id": notebook_id,
                    "error_type": type(exc).__name__,
                })
            except Exception:  # noqa: BLE001 — diagnostics must never break retrieval
                pass
        return False


# codex #640 R2 P2: ``_retrieve_chunks_multi`` fans a single chunk-arm entry
# out to one ``_retrieve_chunks`` call per sub-query (a ThreadPoolExecutor, one
# COPIED context per task).  Threading the once-per-arm drift verdict down as
# an ordinary keyword argument on ``_retrieve_chunks`` would change that
# method's call-time signature for every one of its many existing test
# doubles (several suites replace ``_retrieve_chunks`` wholesale with a
# narrower fake and would raise ``TypeError`` on an unexpected kwarg). A
# contextvar sidesteps that: ``_retrieve_chunks_multi`` sets it once, each
# sub-query's copied ``Context`` snapshots that one value, and only the real,
# never-mocked ``_retrieve_chunks_baseline`` reads it back — a full-method
# fake never even looks at it.  Scope is exactly one ``_retrieve_chunks_multi``
# (or one federated task table) call: set immediately before the fan-out, reset
# in a ``finally`` right after building the per-task context copies. This is
# NOT the run/request-level memoisation codex #634 R1 rejected, and it is never
# read for a scope ENFORCEMENT decision — see ``_lexical_gate_source_scoped``'s
# docstring for why a routing-only use tolerates this while enforcement never
# may.
_CHUNK_ARM_DRIFTED: contextvars.ContextVar[Optional[bool]] = contextvars.ContextVar(
    "_chunk_arm_drifted", default=None
)

# Federated chunk recall (``chunk_federation``) searches libraries the user is
# not "in". For a LARGE one of those, cold-loading its scale index would evict
# the warm indexes the single-notebook path depends on, so that one task may
# only BORROW an index that is already resident. A contextvar for the same
# reason as ``_CHUNK_ARM_DRIFTED`` above: ``_retrieve_chunks`` is replaced
# wholesale by narrower test doubles, while ``_retrieve_chunks_baseline`` --
# the only reader -- never is. Scope is exactly one federated task's copied
# ``Context``; it is never set for the active notebook.
_CHUNK_PEEK_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_chunk_peek_only", default=False
)


__all__ = ["_CHUNK_ARM_DRIFTED", "_CHUNK_PEEK_ONLY", "_lexical_gate_drift_probe"]
