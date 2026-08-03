from typing import List, Literal

from pydantic import BaseModel, Field, field_validator


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

    @field_validator("source_ids")
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
    """

    mode: Literal["include", "exclude"] = "exclude"
    notebook_ids: List[str] = Field(default_factory=list, max_length=1_000)

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
    bases: List[RetrievalScopeBaseReceipt] = Field(
        default_factory=list, max_length=1_000
    )

