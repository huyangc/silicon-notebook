"""Core-owned application contracts for knowledge candidate projection."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


KNOWLEDGE_EVIDENCE_QUOTE_MAX_CHARS = 400


@dataclass(frozen=True, slots=True)
class KnowledgeProjectionElement:
    ordinal: int
    element_id: str
    element_type: str
    location_label: str
    text: str


@dataclass(frozen=True, slots=True)
class KnowledgeProjectionSchema:
    object_type: str
    fields: tuple[str, ...]
    primary: str
    list_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeProjectionEvidencePatch:
    ordinal: int
    quote: str


@dataclass(frozen=True, slots=True)
class KnowledgeProjectionObjectPatch:
    candidate_key: str
    object_type: str
    payload: Mapping[str, Any]
    evidence: tuple[KnowledgeProjectionEvidencePatch, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeProjectionRelationPatch:
    source_key: str
    target_key: str
    edge_type: str
    evidence: tuple[KnowledgeProjectionEvidencePatch, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeProjectionPatch:
    plugin_id: str
    plugin_version: str
    contribution_id: str
    objects: tuple[KnowledgeProjectionObjectPatch, ...]
    relations: tuple[KnowledgeProjectionRelationPatch, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeProjectionCallContext:
    elements: tuple[KnowledgeProjectionElement, ...]
    schemas: tuple[KnowledgeProjectionSchema, ...]
    cancellation: Any
    connection_probe: Any
    max_objects: int
    max_relations: int
    max_candidate_bytes: int
    deadline_monotonic: float


class KnowledgeCandidateProjectorHostPort(Protocol):
    @property
    def has_contributors(self) -> bool: ...

    def project_application(
        self,
        call_context: KnowledgeProjectionCallContext,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[KnowledgeProjectionPatch, ...]: ...
