"""Core-facing indexing-pipeline ports and product identity contracts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, Sequence


BUILTIN_INDEXING_PIPELINE_VERSION = "builtin.chunk.v1"


class IndexingPipelineUnavailableError(RuntimeError):
    """The selected deployment pipeline is absent or unavailable for writes."""

    def __init__(self, pipeline_id: str = "") -> None:
        super().__init__("selected indexing pipeline is unavailable")
        self.pipeline_id = pipeline_id


class IndexingPipelineStalePlanError(RuntimeError):
    """A notebook/source generation changed while a whole-notebook plan ran."""


class IndexingPipelineRebuildActiveError(RuntimeError):
    """A rebuild worker is active; desired intent must not be re-minted.

    Raised by ``begin()`` BEFORE any desired-columns write: advancing the
    generation while a worker is mid-rebuild would doom that worker's publish
    CAS after it has already spent the whole notebook's model/embedding cost,
    while the second submitter reads an innocent-looking 409.
    """


class IndexingPipelineRebuildFailedError(RuntimeError):
    """A bounded desired generation remains pending after rebuild rejection."""


class IndexingPipelineKgExtractionFailedError(RuntimeError):
    """At least one source rejected the selected pipeline's KG strategy."""


@dataclass(frozen=True, slots=True)
class IndexingPipelineOption:
    pipeline_id: str
    label: str
    description: str
    version: str
    overrides_chunking: bool
    overrides_kg_extraction: bool
    available: bool


@dataclass(frozen=True, slots=True)
class IndexingPipelineDescriptor:
    pipeline_id: str
    label: str
    description: str
    version: str
    overrides_chunking: bool
    overrides_kg_extraction: bool


@dataclass(frozen=True, slots=True)
class IndexingSourceElement:
    """One read-only parsed element; no repository or connection is exposed."""

    element_id: str
    element_type: str
    text: str
    caption: str = ""
    description: str = ""
    section_path: str = ""


@dataclass(frozen=True, slots=True)
class IndexingChunkContext:
    """Content-free settings projection for one pure chunking invocation."""

    target_chars: int
    overlap_chars: int


@dataclass(frozen=True, slots=True)
class IndexingChunkProposal:
    text: str
    element_ids: tuple[str, ...]
    section_path: str = ""


@dataclass(frozen=True, slots=True)
class IndexingKgElement:
    """One window-local evidence handle plus prompt text.

    ``handle`` is minted by core for this one extraction window.  Plugins must
    return the handle, never a durable source/element id, when mapping model
    output back to evidence.
    """

    handle: str
    text: str
    element_type: str
    location_label: str = ""
    section_path: str = ""


@dataclass(frozen=True, slots=True)
class IndexingKgEdgeType:
    edge_type: str
    allowed_pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class IndexingKgPromptContext:
    doc_type: str
    section_path: str
    object_types: tuple[str, ...]
    edge_types: tuple[IndexingKgEdgeType, ...]


@dataclass(frozen=True, slots=True)
class IndexingKgMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class IndexingKgPrompt:
    messages: tuple[IndexingKgMessage, ...]
    response_schema_hint: str = ""


@dataclass(frozen=True, slots=True)
class IndexingKgStepProposal:
    name: str
    evidence_handles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IndexingKgObjectProposal:
    local_id: str
    object_type: str
    name: str
    evidence_handles: tuple[str, ...]
    section_path: str = ""
    validity_scope: object | None = None
    steps: tuple[IndexingKgStepProposal, ...] = ()


@dataclass(frozen=True, slots=True)
class IndexingKgEdgeProposal:
    edge_type: str
    source_local_id: str
    target_local_id: str
    evidence_handles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IndexingKgFragment:
    objects: tuple[IndexingKgObjectProposal, ...]
    edges: tuple[IndexingKgEdgeProposal, ...] = ()


class IndexingPipelineProvider(Protocol):
    descriptor: IndexingPipelineDescriptor

    def build_chunks(
        self,
        elements: tuple[IndexingSourceElement, ...],
        context: IndexingChunkContext,
    ) -> tuple[IndexingChunkProposal, ...]: ...

    def build_kg_prompt(
        self,
        elements: tuple[IndexingKgElement, ...],
        context: IndexingKgPromptContext,
    ) -> IndexingKgPrompt: ...

    def map_kg_response(
        self,
        response: str,
        elements: tuple[IndexingKgElement, ...],
        context: IndexingKgPromptContext,
    ) -> IndexingKgFragment: ...


@dataclass(frozen=True, slots=True)
class IndexingPipelineAvailabilityContext:
    contribution_id: str
    pipeline_id: str


@dataclass(frozen=True, slots=True)
class IndexingPipelineChunkResult:
    proposals: tuple[object, ...] | None
    warning_code: str = ""


@dataclass(frozen=True, slots=True)
class IndexingPipelineKgPromptResult:
    prompt: object | None
    warning_code: str = ""


@dataclass(frozen=True, slots=True)
class IndexingPipelineKgMapResult:
    fragment: object | None
    warning_code: str = ""


@dataclass(frozen=True, slots=True)
class IndexingPipelineKgLimits:
    """Validated core-owned rails for one plugin KG window.

    The host deliberately does not own deployment settings.  The extraction
    service projects the validated values into this immutable value and keeps
    model scheduling, cancellation and admission on the core side.
    """

    max_messages: int
    max_prompt_chars: int
    max_schema_hint_chars: int
    max_objects: int
    max_edges: int
    max_evidence_handles: int
    max_steps_per_object: int
    max_name_chars: int


class IndexingPipelineHostPort(Protocol):
    def options(self) -> tuple[IndexingPipelineOption, ...]: ...

    def option(self, pipeline_id: str) -> IndexingPipelineOption | None: ...

    def build_chunks(
        self,
        pipeline_id: str,
        elements: Sequence[object],
        *,
        target_chars: int,
        overlap_chars: int,
    ) -> IndexingPipelineChunkResult: ...

    def build_kg_prompt(
        self,
        pipeline_id: str,
        elements: Sequence[object],
        *,
        doc_type: str,
        section_path: str,
        object_types: Sequence[str] = (),
    ) -> IndexingPipelineKgPromptResult: ...

    def map_kg_response(
        self,
        pipeline_id: str,
        response: str,
        elements: Sequence[object],
        *,
        doc_type: str,
        section_path: str,
        object_types: Sequence[str] = (),
    ) -> IndexingPipelineKgMapResult: ...
