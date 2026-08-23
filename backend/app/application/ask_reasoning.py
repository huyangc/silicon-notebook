"""Immutable data contracts for the Ask reasoning application pipeline.

Concrete orchestration still lives in the established services. These values
make the real prepare -> retrieve -> synthesize -> persist ownership transfers
explicit without exposing a repository, service locator, transaction, worker,
or plugin seat.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from app.core.ask_retrieval_policy import AskRetrievalLimits
from app.domain.cancellation import CancelEvent
from app.models.ask import AskResponse, QueryIntentContract


class StageBoundaryError(RuntimeError):
    """A core orchestration invariant failed; never an optional lane failure."""


class ReasoningStageExecutor(Protocol):
    """The sole behavior admitted through the application retrieval seam."""

    def run_stage(
        self,
        stage: "ReasoningRunInput",
        runtime: "ReasoningRetrievalRuntime",
    ) -> "ReasoningEvidenceSnapshot": ...


class ResponseDraftStage(Protocol):
    """The sole behavior admitted through the application synthesis seam.

    ``retrieval evidence -> response draft`` is the second injectable Ask
    reasoning stage.  The shipped implementation is the previously inline
    synthesis/binding segment; any implementation receives the same immutable
    input and the same point-specific runtime, and owes the core exactly one
    ``ReasoningResponseDraft``.  Persistence stays behind the separate commit
    boundary, so no stage implementation can reach the atomic save, the job
    terminal state, or the answer row.

    ``mode`` (always ``"reasoning"``) and ``model_errors`` are produced by the
    stage as part of the drafted ``AskResponse`` -- core re-verifies ``mode``
    at the commit boundary (``_commit_reasoning_draft``) so a stage cannot
    silently steer a reasoning turn's persisted answer into another mode's
    shape.
    """

    def draft_response(
        self,
        stage: "ResponseDraftInput",
        runtime: "ReasoningRetrievalRuntime",
    ) -> "ReasoningResponseDraft": ...


class ConnectionHoldProbe(Protocol):
    """I/O-free view of the current adapter connection boundary."""

    def is_connection_held(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReasoningIntentProjection:
    """Closed projection shared by the intent trace and retrieval experience."""

    resolved_question: str
    result_scope: str
    completeness_required: bool
    retrieval_effort: str
    entities: tuple[str, ...]
    constraints: tuple[str, ...]
    excluded_topics: tuple[str, ...]
    assumptions: tuple[str, ...]
    expected_output: str
    mandatory_topics: tuple[str, ...]

    def as_mapping(self) -> Mapping[str, object]:
        return MappingProxyType({
            "resolved_question": self.resolved_question,
            "result_scope": self.result_scope,
            "completeness_required": self.completeness_required,
            "retrieval_effort": self.retrieval_effort,
            "entities": self.entities,
            "constraints": self.constraints,
            "excluded_topics": self.excluded_topics,
            "assumptions": self.assumptions,
            "expected_output": self.expected_output,
            "mandatory_topics": self.mandatory_topics,
        })

    def as_json_mapping(self) -> dict[str, object]:
        """Fresh legacy-shaped projection for callbacks and service adapters."""
        return {
            "resolved_question": self.resolved_question,
            "result_scope": self.result_scope,
            "completeness_required": self.completeness_required,
            "retrieval_effort": self.retrieval_effort,
            "entities": list(self.entities),
            "constraints": list(self.constraints),
            "excluded_topics": list(self.excluded_topics),
            "assumptions": list(self.assumptions),
            "expected_output": self.expected_output,
            "mandatory_topics": list(self.mandatory_topics),
        }


@dataclass(frozen=True, slots=True)
class ReasoningRetrievalRuntime:
    """Point-specific request authorities, separate from serializable data."""

    scope: object | None
    retrieval_run: object | None
    cancellation: CancelEvent
    trace_sink: Callable[[Any], None] | None
    connection_probe: ConnectionHoldProbe | None


@dataclass(frozen=True, slots=True)
class PreparedReasoningAsk:
    notebook_id: str
    question: str
    conversation_id: str
    history: str
    style_block: str
    intent_json: str
    research_question: str
    intent_queries: tuple[str, ...]
    limits: AskRetrievalLimits
    intent_projection: ReasoningIntentProjection
    intent_trace_duration_ms: int | None
    user_id: str
    job_id: str
    asked_at: str
    retrieval_effort: str


@dataclass(frozen=True, slots=True)
class ReasoningRunInput:
    notebook_id: str
    question: str
    history: str
    top_n: int | None
    max_steps: int | None
    intent_queries: tuple[str, ...]
    limits: AskRetrievalLimits | None
    intent: ReasoningIntentProjection | None


@dataclass(frozen=True, slots=True)
class ReasoningEvidenceSnapshot:
    """Immutable ownership transfer from retrieval to answer assembly.

    Tuple snapshots prevent answer assembly from mutating retriever containers.
    Contained domain evidence keeps its historical object identity because
    citation binding and baseline attestation rely on that identity.
    """

    top_hits: tuple[object, ...]
    elements: tuple[object, ...]
    trace: tuple[object, ...]
    chunks: tuple[object, ...]
    chains: tuple[object, ...]
    attempted: tuple[Mapping[str, object], ...]
    enumerations: tuple[object, ...]
    collection_map_text: str
    outline: tuple[object, ...]
    outline_evidence: tuple[object, ...]
    baseline_manifest: object | None

    @classmethod
    def from_result(cls, result: object) -> "ReasoningEvidenceSnapshot":
        return cls(
            top_hits=tuple(getattr(result, "top_hits", ())),
            elements=tuple(getattr(result, "elements", ())),
            trace=tuple(getattr(result, "trace", ())),
            chunks=tuple(getattr(result, "chunks", ())),
            chains=tuple(getattr(result, "chains", ())),
            attempted=tuple(
                MappingProxyType(dict(item))
                for item in getattr(result, "attempted", ())
            ),
            enumerations=tuple(getattr(result, "enumerations", ())),
            collection_map_text=str(
                getattr(result, "collection_map_text", "") or ""
            ),
            outline=tuple(getattr(result, "outline", ())),
            outline_evidence=tuple(getattr(result, "outline_evidence", ())),
            baseline_manifest=getattr(result, "baseline_manifest", None),
        )


@dataclass(frozen=True, slots=True)
class RetrievedReasoningAsk:
    prepared: PreparedReasoningAsk
    evidence: ReasoningEvidenceSnapshot


@dataclass(frozen=True, slots=True)
class ResponseDraftInput:
    """Immutable ownership transfer from retrieval into answer assembly.

    Deliberately *not* a ``RetrievedReasoningAsk``: that envelope carries the
    retriever's pristine snapshot, while this one carries what answer assembly
    actually consumes -- the evidence after selected-source-graph activation
    and after the retriever's own fail-open degradation, which can legitimately
    be empty where the snapshot was not.  Collapsing the two would let a
    degraded turn read a snapshot the run never actually used.

    The pre-retrieval facts (memory hits, the structured Knowhow batch, the two
    disclosure flags) ride along because the drafted answer discloses them; they
    are decided before retrieval and are read-only here.  Tuple snapshots keep
    assembly from mutating the orchestrator's containers, while the contained
    domain evidence keeps its object identity because citation binding and
    baseline attestation rely on it.
    """

    prepared: PreparedReasoningAsk
    intent_contract: QueryIntentContract
    top_hits: tuple[object, ...]
    elements: tuple[object, ...]
    trace: tuple[object, ...]
    chunks: tuple[object, ...]
    chains: tuple[object, ...]
    enumerations: tuple[object, ...]
    collection_map_text: str
    outline: tuple[object, ...]
    outline_evidence: tuple[object, ...]
    historical_chunks: tuple[object, ...]
    memory_hits: tuple[object, ...]
    structured_batch: object | None
    completeness_unavailable: bool
    kg_required: bool
    candidate_manifest: object | None


@dataclass(frozen=True, slots=True)
class ReasoningResponseDraft:
    """Exclusive core-owned response graph before the only atomic save.

    The frozen envelope transfers ownership without serializing or copying the
    graph: collection result cards and the citation list intentionally share
    Citation instances for image-admission accounting.
    """

    notebook_id: str
    question: str
    response: AskResponse
    conversation_id: str
    user_id: str
    job_id: str
    asked_at: str
    baseline_manifest: object | None = None


@dataclass(frozen=True, slots=True)
class CommittedReasoningAnswer:
    """Answer after the atomic answer/job-terminal persistence boundary."""

    response: AskResponse
    baseline_manifest: object | None = None


def execute_reasoning_retrieval_stage(
    retriever: ReasoningStageExecutor,
    stage: ReasoningRunInput,
    runtime: ReasoningRetrievalRuntime,
) -> ReasoningEvidenceSnapshot:
    """Enter the one typed production retrieval seam."""

    runner = getattr(retriever, "run_stage", None)
    if callable(runner):
        result = runner(stage, runtime)
        if type(result) is not ReasoningEvidenceSnapshot:
            raise StageBoundaryError("invalid reasoning retrieval stage output")
        return result
    raise StageBoundaryError("reasoning retriever has no stage runner")


def execute_response_draft_stage(
    stage_impl: ResponseDraftStage,
    stage: ResponseDraftInput,
    runtime: ReasoningRetrievalRuntime,
) -> ReasoningResponseDraft:
    """Enter the one typed production synthesis seam.

    Mirrors ``execute_reasoning_retrieval_stage``: the exact output type is a
    core invariant, so a stage returning a look-alike (or a bare
    ``AskResponse``) fails the boundary instead of reaching the atomic save.
    """

    runner = getattr(stage_impl, "draft_response", None)
    if callable(runner):
        result = runner(stage, runtime)
        if type(result) is not ReasoningResponseDraft:
            raise StageBoundaryError("invalid reasoning response draft output")
        return result
    raise StageBoundaryError("reasoning response draft stage has no runner")
