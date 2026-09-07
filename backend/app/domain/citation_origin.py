"""The one definition of the cross-notebook citation-origin rule.

``Citation.notebook_id`` / ``AnswerAnchor.notebook_id`` are **non-empty only on
cross-notebook evidence**: a value equal to the notebook this ask/report runs
against must be normalised to ``""``.  The frontend resolves a non-empty id
through a library-name map that *includes the active notebook*, so echoing the
active id back labels a citation from the user's own notes with a redundant
「来自「当前笔记本自己的名字」」 badge.

This module is deliberately a dependency-free leaf in ``app.domain`` so every
producer can share the rule: the ``app.services`` composers (evidence_context,
ask_service) and the import-light ``app.services.kg`` renderers (follow_chain,
graph_reason) alike.  The rule was re-implemented inline at least six times
during the multi-domain base rollout and was missed three times in review (see
``docs/superpowers/specs/2026-07-19-multi-domain-bases-followups.md`` §A1);
``backend/tests/test_citation_notebook_id_guard.py`` statically pins the two
shapes a seventh copy would take — a ``notebook_id=`` keyword on a
``Citation``/``AnswerAnchor`` construction (including a ``**`` splat, and under
whatever alias the model is imported as) and a ``"notebook_id"`` key written by
one of the registered id_map builders — to this function.  Values assembled any
other way (``dict(...)``/``update()``, a write in an unregistered function,
laundering through an attribute) are outside the guard's reach and rest on the
behavioural tests instead; that module's docstring is the authority on scope.
"""
from __future__ import annotations


def foreign_notebook_id(origin: object, active_notebook_id: object) -> str:
    """Return ``origin`` only when it is genuinely a *foreign* notebook id.

    ``""`` is returned for both "no origin recorded" and "origin is the active
    notebook".  Callers hand in raw values straight off a retrieval hit or a
    stored row (federated retrieval stamps *every* hit — including the active
    notebook's own — with its owning id, which is exactly why the comparison
    cannot be skipped), so ``None``/non-``str`` inputs are coerced rather than
    rejected: this is a display-normalisation rule, never a validation gate.

    The function is idempotent — feeding an already-normalised value back in
    returns it unchanged — so a defensive second call is always safe.
    """
    origin_id = str(origin or "")
    if not origin_id:
        return ""
    return "" if origin_id == str(active_notebook_id or "") else origin_id
