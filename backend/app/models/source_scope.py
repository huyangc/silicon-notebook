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
    _hidden_source_ids: List[str] = PrivateAttr(default_factory=list)

    @property
    def hidden_source_ids(self) -> List[str]:
        return list(self._hidden_source_ids)

    @field_validator("source_ids")
    @classmethod
    def normalize_source_ids(cls, values: List[str]) -> List[str]:
        normalized = [str(value).strip() for value in values if str(value).strip()]
        return list(dict.fromkeys(normalized))
