from typing import List, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr, field_validator


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
    # Server-computed semantic state.  ``include`` is also used to freeze the
    # browser's current all-selected snapshot, so its shape alone cannot tell
    # whether the user actually excluded anything.  None keeps direct/legacy
    # service callers backward compatible; API entry points always overwrite
    # this value from the current visible-source universe.
    narrowed: Optional[bool] = None
    # Server-only physical participants captured for an all-selected snapshot.
    # They keep the historical Memory/Knowhow projection behavior without
    # exposing hidden source ids in API/report payloads.  API validation always
    # overwrites this field; persisted report scopes recompute it at each gate.
    #
    # The two kinds inside it are scoped differently: Knowhow projections are
    # notebook-wide and reach every member's ceiling, while a Memory projection
    # is private to its creator and only ever enters that user's ceiling. The
    # split is enforced in ``scope_source_ids``' SQL, so this list never
    # carries another member's Memory source id.
    _hidden_source_ids: List[str] = PrivateAttr(default_factory=list)
    # The identity the hidden half above was read for.  Carried (never
    # serialized) so the retrieval drift probe can re-read that partition in
    # the SAME frame the freeze was taken in: an owner-scoped freeze compared
    # against an unfiltered live read would report drift forever in any shared
    # notebook holding another member's Memory, permanently disabling the
    # whole-graph/PPR/relation/exact channels.  Empty for direct service-layer
    # construction, which never reaches the drift comparison.
    _scope_owner_id: str = PrivateAttr(default="")

    @property
    def hidden_source_ids(self) -> List[str]:
        return list(self._hidden_source_ids)

    @property
    def scope_owner_id(self) -> str:
        return self._scope_owner_id

    @field_validator("source_ids")
    @classmethod
    def normalize_source_ids(cls, values: List[str]) -> List[str]:
        normalized = [str(value).strip() for value in values if str(value).strip()]
        return list(dict.fromkeys(normalized))


class BaseNotebookScope(BaseModel):
    """User-selected mounted-reference-library scope for one retrieval run.

    Mirrors ``SourceScope``'s shape and validation philosophy, but selects
    whole mounted base notebooks rather than individual sources inside the
    active notebook -- a different granularity with a different lifecycle, so
    library ids never live on ``SourceScope`` itself.

    ``include`` means only the listed mounted base notebooks participate;
    ``exclude`` means every mounted base notebook except the listed ones
    participates. Omitting this model from a request preserves the historical
    behavior of every mounted base notebook participating unconditionally.

    ``narrowed`` is the same server-computed fact ``SourceScope`` carries and
    for the same reason: the API boundary freezes ``exclude`` into an explicit
    ``include`` snapshot (so a library mounted after the run started cannot
    widen it), which makes ``mode`` permanently ``include`` and shape-based
    judgment report every browser request as narrowed. Clients may send the
    field; the boundary always recomputes and overwrites it.

    ``max_length`` is a soft ceiling against pathological requests, not a
    supported mount-scale limit -- federated retrieval issues one query per
    participating library and stops being usable long before 10,000 mounts.
    It exists because ``exclude`` + [] is frozen into an explicit include list
    naming every currently-mounted library, so the bound must comfortably
    exceed any mount count the product actually expects.
    """

    mode: Literal["include", "exclude"] = "exclude"
    notebook_ids: List[str] = Field(default_factory=list, max_length=10_000)
    narrowed: Optional[bool] = None

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
    demoted, and a reader that re-derived names from the notebook's *current*
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
    # Must not be lower than BaseNotebookScope.notebook_ids's cap -- a run
    # scoped over more mounted libraries than this could fit would otherwise
    # have its receipt silently truncated, understating how many libraries
    # actually participated or were excluded. The receipt is a disclosure, and
    # a truncated list presented as exhaustive makes the reader compute a wrong
    # denominator.
    bases: List[RetrievalScopeBaseReceipt] = Field(
        default_factory=list, max_length=10_000
    )
