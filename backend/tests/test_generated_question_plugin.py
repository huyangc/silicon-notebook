from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    GENERATED_QUESTION_ACCESS_CAPABILITY,
    RETRIEVAL_CONTRIBUTOR_POINT,
    Availability,
    ContributionDeclaration,
    ContributionKind,
    ContributorResult,
    EvidenceCandidate,
    EvidenceProvenance,
    ExtensionContribution,
    ExtensionManifest,
    ExtensionResultStatus,
)
from app.extensions import build_extension_runtime
from app.extensions.builtin import (
    GENERATED_QUESTION_BUNDLE,
    GENERATED_QUESTION_CONTRIBUTION_ID,
)
from app.services.cancellation import AskCancelled
from app.services.generated_question_contribution import (
    GeneratedQuestionContributionCall,
    generated_question_call_context,
)
from app.services.retrieval import RetrievalSupport, RetrievedChunk


def _chunk(chunk_id: str, *, generated: bool = False) -> RetrievedChunk:
    supports = (
        RetrievalSupport("generated_question", "chunk", chunk_id, 0.9),
    ) if generated else ()
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_id=f"source-{chunk_id}",
        source_title=chunk_id,
        section_path="",
        text="",
        notebook_id="notebook",
        retrieval_supports=supports,
    )


class _Cancellation:
    def is_set(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


class _IndependentContributor:
    invocations = frozenset({"chunk_candidates"})

    def contribute(self, _context):
        return ContributorResult(
            (
                EvidenceCandidate(
                    identity="independent",
                    notebook_id="notebook",
                    source_id="source-independent",
                    provenance=EvidenceProvenance("chunk", "independent"),
                    value=object(),
                    token_cost=0,
                ),
            ),
            ExtensionResultStatus.AVAILABLE,
        )


@dataclass(frozen=True)
class _IndependentBundle:
    declaration = ContributionDeclaration(
        "builtin.independent_test",
        RETRIEVAL_CONTRIBUTOR_POINT,
        ContributionKind.CONTRIBUTOR,
    )
    manifest = ExtensionManifest(
        id="builtin.independent_test",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Independent test contributor",
        trust="builtin",
        contributions=(declaration,),
    )

    @staticmethod
    def register(registrar) -> None:
        registrar.add_contributor(
            ExtensionContribution(
                _IndependentBundle.declaration,
                _IndependentContributor(),
            )
        )


def _runtime():
    return build_extension_runtime(
        (GENERATED_QUESTION_BUNDLE, _IndependentBundle()),
        capability_decisions={
            GENERATED_QUESTION_ACCESS_CAPABILITY: lambda _context: (
                Availability.available()
            ),
        },
        retrieval_admission_policies={
            GENERATED_QUESTION_CONTRIBUTION_ID: "atomic",
        },
    )


def test_disabled_generated_capability_short_circuits_before_context():
    runtime = build_extension_runtime(
        (GENERATED_QUESTION_BUNDLE,),
        capability_decisions={
            GENERATED_QUESTION_ACCESS_CAPABILITY: lambda _context: (
                Availability.available()
            ),
        },
        retrieval_admission_policies={
            GENERATED_QUESTION_CONTRIBUTION_ID: "atomic",
        },
    )
    baseline = [_chunk("base")]

    result = runtime.retrieval_contributors.run(
        baseline,
        invocation="chunk_candidates",
        context_factory=lambda: (_ for _ in ()).throw(
            AssertionError("disabled lane built a context")
        ),
        disabled_capabilities=frozenset({
            GENERATED_QUESTION_ACCESS_CAPABILITY
        }),
    )

    assert result is baseline


def _context(call, fallback):
    return generated_question_call_context(
        call,
        actor_id="actor",
        cancel_event=None,
        connection_probe=SimpleNamespace(is_connection_held=lambda: False),
        admission_hydrate=fallback,
        max_results=5,
    )


def test_independent_chunk_candidate_survives_empty_generated_lane():
    baseline = [_chunk("base")]
    matrix_calls = []
    fallback_calls = []
    final_matrix = object()
    call = GeneratedQuestionContributionCall(
        "notebook",
        (baseline, ["base"], object()),
        mode="on",
        evaluate=lambda isolated: isolated,
        matrix_hydrate=lambda ids: (
            matrix_calls.append(tuple(ids)) or [], list(ids), final_matrix
        ),
        failure_event=lambda: None,
    )

    def fallback(notebook_id, actor_id, identities):
        fallback_calls.append((notebook_id, actor_id, identities))
        return [_chunk("independent")]

    context = _context(call, fallback)
    host_chunks = _runtime().retrieval_contributors.run(
        baseline,
        invocation="chunk_candidates",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )
    visible, ids, matrix = call.visible_result(host_chunks)

    assert [chunk.chunk_id for chunk in visible] == ["base", "independent"]
    assert fallback_calls == [("notebook", "actor", ("independent",))]
    assert matrix_calls == [("base", "independent")]
    assert ids == ["base", "independent"]
    assert matrix is final_matrix


def test_generated_atomic_rejection_does_not_drop_independent_tail():
    baseline = [_chunk("base")]
    failures = []
    call = GeneratedQuestionContributionCall(
        "notebook",
        (baseline, ["base"], object()),
        mode="on",
        evaluate=lambda isolated: (
            [*isolated[0], _chunk("generated", generated=True)],
            ["base", "generated"],
            object(),
        ),
        matrix_hydrate=lambda ids: ([], list(ids), "rebuilt"),
        failure_event=lambda: failures.append("failed_open"),
    )
    real_read = call.read

    def fallback(_notebook_id, _actor_id, identities):
        return [_chunk("independent")] if identities == ("independent",) else []

    context = _context(call, fallback)
    # Reject only generated authority; the independent contributor still uses
    # the generic scope-bound fallback in its own host admission.
    call.read = lambda identities: () if identities == ("generated",) else real_read(identities)
    host_chunks = _runtime().retrieval_contributors.run(
        baseline,
        invocation="chunk_candidates",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )
    visible, ids, matrix = call.visible_result(host_chunks)

    assert [chunk.chunk_id for chunk in visible] == ["base", "independent"]
    assert ids == ["base", "independent"]
    assert matrix == "rebuilt"
    assert failures == ["failed_open"]


def test_multi_query_native_cancellation_is_not_swallowed():
    from app.services.retrieval_candidates import CandidateRetrievalService

    candidates = SimpleNamespace(
        _retrieve_chunks=lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(AskCancelled())
    )

    with pytest.raises(AskCancelled):
        CandidateRetrievalService._retrieve_chunks_multi(
            candidates, "notebook", ["question"]
        )
