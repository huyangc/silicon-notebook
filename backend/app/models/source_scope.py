from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Server-computed truth carried on both scope models: did this selection
# actually narrow anything, or is it the browser's default "everything is
# checked"?
#
# It cannot be derived from ``mode``/the id list at consumption time. The API
# boundary freezes every submitted scope into an explicit ``include`` snapshot
# (so a source uploaded, or a library mounted, after the run started cannot
# widen it -- see ``_validate_source_scope`` / ``_validate_base_scope``), which
# makes ``mode`` **permanently** ``include`` and the historical
# "``exclude`` + [] means all" branch unreachable over HTTP. Judging by shape
# alone therefore reports every default request as narrowed, which silently
# disables private Memory, whole conversation history, PPR, community reports
# and the corpus profile for users who never narrowed anything (PR#426).
#
# ``None`` means "not computed" and makes the consumer fall back to the old
# value-driven判据 -- direct service-layer callers construct scopes without a
# repository and must keep behaving exactly as before.
#
# Clients may send this field; the API boundary ALWAYS recomputes and
# overwrites it, so a forged value cannot widen or narrow a run.
_NARROWED_FIELD = Field(
    default=None,
    description="Server-computed: whether this scope actually narrows the "
                "notebook's full universe. Client-supplied values are ignored.",
)


class SourceScope(BaseModel):
    """User-selected imported-source scope for one retrieval run.

    ``include`` means only the listed active-notebook sources participate;
    ``exclude`` means every visible active-notebook source except the listed
    ones participates. Mounted base notebooks remain independent participants.
    Omitting this model from a request preserves the historical whole-scope
    behavior for compatibility clients.
    """

    mode: Literal["include", "exclude"] = "exclude"
    source_ids: List[str] = Field(default_factory=list, max_length=10_000)
    narrowed: Optional[bool] = _NARROWED_FIELD
    # The hidden Memory/Knowhow projection sources this run's frozen ceiling
    # ALSO admits. A separate list, never folded into ``source_ids``, for three
    # reasons that each break if they are merged:
    #
    #   * ``source_ids`` is a USER SELECTION -- every id in it is a checkbox the
    #     user could tick. Hidden projection sources are infrastructure: they
    #     carry no row in the source list and cannot be unchecked. The receipt
    #     (``_scope_receipt``) counts ``source_ids`` against the notebook's
    #     visible source count, so folding them in would print "53 of 5 来源".
    #   * ``_validate_source_scope`` rejects (422) any submitted id outside the
    #     visible universe. A report's frozen scope is re-validated at confirm
    #     and at generate, so a merged list would fail its own re-validation --
    #     and widening that membership check to accept hidden ids would let a
    #     hand-crafted request name another user's private Memory projection.
    #   * ``narrowed`` is measured against the visible universe only; a merged
    #     list would make ``len(selected) < len(all_ids)`` incommensurable.
    #
    # Populated by the API boundary ONLY when the local dimension was not
    # narrowed (see ``_validate_source_scope``), which is what keeps the
    # existing "收窄来源维度时隐藏 Memory/Knowhow 投影证据不参与" contract true
    # by construction rather than by a second consumption-side condition.
    #
    # Clients may send this field; the API boundary ALWAYS recomputes and
    # overwrites it, exactly as it does for ``narrowed``.
    #
    # ``max_length`` deliberately matches ``source_ids``'s: both end up in the
    # same frozen include list that every candidate producer pushes below its
    # LIMIT, so they share one order of magnitude. It IS a new exposure at that
    # scale (a notebook with more than 10,000 Knowhow tables + confirmed Memory
    # items now fails the freeze where it previously ran unscoped), and that is
    # the deliberate choice: truncating instead would silently drop some of the
    # notebook's Knowhow/Memory evidence with no way for the user to see it,
    # which is the exact failure class this field exists to remove.
    hidden_source_ids: List[str] = Field(
        default_factory=list,
        max_length=10_000,
        description="Server-computed: hidden Memory/Knowhow projection sources "
                    "inside this run's frozen ceiling. Client-supplied values "
                    "are ignored.",
    )

    @field_validator("source_ids", "hidden_source_ids")
    @classmethod
    def normalize_source_ids(cls, values: List[str]) -> List[str]:
        normalized = [str(value).strip() for value in values if str(value).strip()]
        return list(dict.fromkeys(normalized))


class BaseNotebookScope(BaseModel):
    """User-selected mounted-reference-library scope for one retrieval run.

    Mirrors ``SourceScope``'s shape and validation philosophy, but selects
    whole mounted base notebooks rather than individual sources within the
    active notebook -- a different granularity and a different lifecycle, so
    library ids never live on ``SourceScope`` itself.

    ``include`` means only the listed mounted base notebooks participate;
    ``exclude`` means every mounted base notebook except the listed ones
    participates. Omitting this model from a request preserves the
    historical behavior of every mounted base notebook participating
    unconditionally.

    codex #431 R4: ``max_length`` is a soft ceiling against pathological
    requests, not a supported mount-scale limit -- ``exclude`` + an empty
    list is frozen (at the API boundary, see ``_validate_base_scope``) into
    an explicit ``include`` list naming every currently-mounted library, so
    this bound must exceed any mount count the product actually expects.
    Federated retrieval issues one query per participating library and stops
    being usable long before 10,000 mounts, so this is not the real ceiling
    on how many libraries a notebook may mount.
    """

    mode: Literal["include", "exclude"] = "exclude"
    notebook_ids: List[str] = Field(default_factory=list, max_length=10_000)
    narrowed: Optional[bool] = _NARROWED_FIELD

    @field_validator("notebook_ids")
    @classmethod
    def normalize_notebook_ids(cls, values: List[str]) -> List[str]:
        normalized = [str(value).strip() for value in values if str(value).strip()]
        return list(dict.fromkeys(normalized))


class RetrievalScopeLocalReceipt(BaseModel):
    """How much of the active notebook this run was allowed to search."""

    selected: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)


class RetrievalScopeBaseReceipt(BaseModel):
    """One mounted reference library's participation in a finished run.

    ``name`` is a SNAPSHOT taken while the run was authorized, not a live
    lookup key. An answer outlives its mounts: by the time the turn is
    reopened the library may have been unmounted, handed to another owner or
    demoted, and a reader that re-derives names from the notebook's *current*
    mounts would silently drop exactly the row that explains the answer.
    """

    notebook_id: str = Field(default="", max_length=500)
    name: str = Field(default="", max_length=500)
    included: bool = True


class RetrievalScopeReceipt(BaseModel):
    """Display-only record of the scope a finished run actually ran under.

    Persisted with the answer and **never read back by retrieval**: the
    authoritative ceiling is the request's ``SourceScope``/``BaseNotebookScope``
    frozen at the API entry point, and this model exists only so the answer can
    say what that ceiling was. Nothing here may become an input to a gate --
    see ``app.services.source_scope`` for where it is carried.

    Deliberately carries no file path, no error text and no source identity:
    library names and counts are already visible to anyone who can mount them,
    which keeps the disclosure surface narrower than the source-detail proxy.
    """

    local: RetrievalScopeLocalReceipt = Field(
        default_factory=RetrievalScopeLocalReceipt
    )
    # codex #431 R4 (P2): must not be lower than BaseNotebookScope.notebook_ids's
    # cap -- a run scoped over more mounted libraries than this could fit would
    # otherwise have its receipt silently truncated, understating how many
    # libraries actually participated or were excluded. Same soft-ceiling
    # rationale as that field: not a supported mount-scale limit.
    bases: List[RetrievalScopeBaseReceipt] = Field(
        default_factory=list, max_length=10_000
    )

