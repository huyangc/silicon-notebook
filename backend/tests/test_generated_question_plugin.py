from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
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
        text=f"ordinary evidence for {chunk_id}",
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
        connection_probe=SimpleNamespace(is_connection_held=lambda: False),
        admission_hydrate=fallback,
        max_items=5,
        max_tokens=100,
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


def test_earlier_same_id_contributor_cannot_impersonate_generated_lane():
    declaration = ContributionDeclaration(
        "aaa.earlier_same_id",
        RETRIEVAL_CONTRIBUTOR_POINT,
        ContributionKind.CONTRIBUTOR,
    )

    class _EarlierContributor:
        invocations = frozenset({"chunk_candidates"})

        @staticmethod
        def contribute(_context):
            return ContributorResult((EvidenceCandidate(
                identity="generated",
                notebook_id="notebook",
                source_id="source-independent",
                provenance=EvidenceProvenance("chunk", "generated"),
                value=object(),
                token_cost=0,
            ),), ExtensionResultStatus.AVAILABLE)

    class _EarlierBundle:
        manifest = ExtensionManifest(
            id="aaa.earlier_same_id",
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name="Earlier same-id test",
            trust="builtin",
            contributions=(declaration,),
        )

        @staticmethod
        def register(registrar):
            registrar.add_contributor(ExtensionContribution(
                declaration, _EarlierContributor()
            ))

    semantic = RetrievalSupport("semantic", "chunk", "base", 0.8)
    collision = RetrievalSupport(
        "generated_question", "chunk", "base", 0.9
    )
    baseline_chunk = _chunk("base")
    baseline_chunk.retrieval_supports = (semantic,)
    independent = _chunk("generated")
    independent.source_id = "source-independent"
    independent.retrieval_supports = (
        RetrievalSupport("semantic", "chunk", "generated", 0.7),
    )
    failures = []
    matrix_calls = []

    def evaluate(isolated):
        chunks, _ids, _matrix = isolated
        chunks[0].retrieval_supports = (semantic, collision)
        return [*chunks, _chunk("generated", generated=True)], [
            "base", "generated"
        ], "GQ_MATRIX"

    call = GeneratedQuestionContributionCall(
        "notebook",
        ([baseline_chunk], ["base"], "BASE_MATRIX"),
        mode="on",
        evaluate=evaluate,
        matrix_hydrate=lambda ids: (
            matrix_calls.append(tuple(ids)) or [], list(ids), "REBUILT"
        ),
        failure_event=lambda: failures.append("generated_rejected"),
    )
    runtime = build_extension_runtime(
        (_EarlierBundle(), GENERATED_QUESTION_BUNDLE),
        capability_decisions={
            GENERATED_QUESTION_ACCESS_CAPABILITY: lambda _context: (
                Availability.available()
            ),
        },
        retrieval_admission_policies={
            GENERATED_QUESTION_CONTRIBUTION_ID: "atomic",
        },
    )
    context = _context(
        call,
        lambda _notebook_id, _actor_id, identities: (
            [independent] if identities == ("generated",) else []
        ),
    )
    host_chunks = runtime.retrieval_contributors.run(
        [baseline_chunk],
        invocation="chunk_candidates",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )

    visible, ids, matrix = call.visible_result(host_chunks)

    assert list(host_chunks) == [baseline_chunk, independent]
    assert visible == [baseline_chunk, independent]
    assert visible[1] is independent
    assert baseline_chunk.retrieval_supports == (semantic,)
    assert ids == ["base", "generated"]
    assert matrix == "REBUILT"
    assert matrix_calls == [("base", "generated")]
    assert failures == ["generated_rejected"]


def test_generated_on_commits_collision_support_and_unique_tail_atomically():
    semantic = RetrievalSupport("semantic", "chunk", "base", 0.8)
    generated_collision = RetrievalSupport(
        "generated_question", "chunk", "base", 0.91
    )
    baseline_chunk = _chunk("base")
    baseline_chunk.retrieval_supports = (semantic,)
    baseline_ids = ["base"]
    baseline_matrix = np.asarray([[1.0, 0.0]], dtype=np.float32)
    final_ids = ["base", "generated"]
    final_matrix = np.asarray(
        [[1.0, 0.0], [0.25, 0.75]], dtype=np.float32
    )

    def evaluate(isolated):
        chunks, _ids, _matrix = isolated
        chunks[0].retrieval_supports = (semantic, generated_collision)
        return [*chunks, _chunk("generated", generated=True)], final_ids, final_matrix

    call = GeneratedQuestionContributionCall(
        "notebook",
        ([baseline_chunk], baseline_ids, baseline_matrix),
        mode="on",
        evaluate=evaluate,
        matrix_hydrate=lambda _ids: (_ for _ in ()).throw(
            AssertionError("generated-only path rebuilt its matrix")
        ),
        failure_event=lambda: None,
    )
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
    context = _context(call, lambda *_args: ())
    host_chunks = runtime.retrieval_contributors.run(
        [baseline_chunk],
        invocation="chunk_candidates",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )

    visible, ids, matrix = call.visible_result(host_chunks)

    assert visible[0] is baseline_chunk
    assert [chunk.chunk_id for chunk in visible] == ["base", "generated"]
    assert visible[0].retrieval_supports == (semantic, generated_collision)
    assert ids is final_ids
    assert matrix is final_matrix
    assert matrix.tobytes() == final_matrix.tobytes()


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


def test_independent_tail_matrix_read_rechecks_native_cancellation():
    import threading

    baseline = [_chunk("base")]
    cancelled = threading.Event()

    def matrix_hydrate(ids):
        cancelled.set()
        return [], list(ids), object()

    call = GeneratedQuestionContributionCall(
        "notebook",
        (baseline, ["base"], object()),
        mode="on",
        evaluate=lambda isolated: isolated,
        matrix_hydrate=matrix_hydrate,
        failure_event=lambda: None,
        cancel_event=cancelled,
    )
    context = _context(
        call,
        lambda _notebook_id, _actor_id, _identities: [
            _chunk("independent")
        ],
    )
    host_chunks = _runtime().retrieval_contributors.run(
        baseline,
        invocation="chunk_candidates",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )

    with pytest.raises(AskCancelled):
        call.visible_result(host_chunks)


def test_late_malformed_cancellation_state_returns_exact_baseline():
    class _StatefulEvent:
        state = False

        def is_set(self):
            return self.state

    event = _StatefulEvent()
    baseline = [_chunk("base")]
    baseline_tuple = (baseline, ["base"], object())

    def matrix_hydrate(ids):
        event.state = object()
        return [], list(ids), object()

    call = GeneratedQuestionContributionCall(
        "notebook",
        baseline_tuple,
        mode="on",
        evaluate=lambda isolated: isolated,
        matrix_hydrate=matrix_hydrate,
        failure_event=lambda: None,
        cancel_event=event,
    )
    context = _context(
        call,
        lambda _notebook_id, _actor_id, _identities: [
            _chunk("independent")
        ],
    )
    host_chunks = _runtime().retrieval_contributors.run(
        baseline,
        invocation="chunk_candidates",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )

    assert call.visible_result(host_chunks) is baseline_tuple


def test_authoritative_true_cancellation_is_cached_across_host_double_check():
    class _TrueThenMalformedEvent:
        calls = 0

        def is_set(self):
            self.calls += 1
            return True if self.calls == 1 else object()

    event = _TrueThenMalformedEvent()
    baseline = [_chunk("base")]
    call = GeneratedQuestionContributionCall(
        "notebook",
        (baseline, ["base"], object()),
        mode="on",
        evaluate=lambda isolated: isolated,
        matrix_hydrate=lambda _ids: ([], [], None),
        failure_event=lambda: None,
        cancel_event=event,
    )
    context = _context(call, lambda *_args: ())

    with pytest.raises(AskCancelled):
        _runtime().retrieval_contributors.run(
            baseline,
            invocation="chunk_candidates",
            call_context=context,
            baseline_identity=lambda chunk: chunk.chunk_id,
            cancellation=context.cancellation,
        )

    assert event.calls == 1


def test_independent_tail_survives_matrix_rebuild_failure():
    baseline = [_chunk("base")]
    original_ids = ["base"]
    original_matrix = object()
    receipts = []
    call = GeneratedQuestionContributionCall(
        "notebook",
        (baseline, original_ids, original_matrix),
        mode="on",
        evaluate=lambda isolated: isolated,
        matrix_hydrate=lambda _ids: (_ for _ in ()).throw(
            RuntimeError("matrix unavailable")
        ),
        failure_event=lambda: None,
        matrix_failure_event=lambda: receipts.append("matrix_failed_open"),
    )
    context = _context(
        call,
        lambda _notebook_id, _actor_id, _identities: [
            _chunk("independent")
        ],
    )
    host_chunks = _runtime().retrieval_contributors.run(
        baseline,
        invocation="chunk_candidates",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )

    visible, ids, matrix = call.visible_result(host_chunks)

    assert [chunk.chunk_id for chunk in visible] == ["base", "independent"]
    assert ids is original_ids
    assert matrix is original_matrix
    assert receipts == ["matrix_failed_open"]
