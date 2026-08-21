from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.domain.extensions import (
    RetrievalContributionCallContext,
    RetrievalEvidenceProposal,
)
from app.extensions import default_extension_runtime
from app.extension_sdk import (
    EXTENSION_API_VERSION,
    RETRIEVAL_CONTRIBUTOR_POINT,
    SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ContributorResult,
    EvidenceCandidate,
    EvidenceProvenance,
    ExtensionContribution,
    ExtensionManifest,
    ExtensionResultStatus,
    RetrievalHostContext,
)
from app.extensions import build_extension_runtime
from app.extensions.builtin import (
    SELECTED_SOURCE_GRAPH_BUNDLE,
    SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID,
)
from app.repositories.sqlite.database import SqliteDatabase
from app.services.cancellation import AskCancelled
from app.services.retrieval import RetrievedChunk
from app.services.source_graph_activation import (
    ActivatedSourceGraphResult,
    SelectedSourceGraphContributionCall,
    SourceGraphStatus,
    selected_source_graph_call_context,
)


class _Cancellation:
    def is_set(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


class _DatabaseReadingSource:
    def __init__(self, database, value) -> None:
        self.database = database
        self.value = value
        self.calls = 0
        self.proposal = RetrievalEvidenceProposal(
            identity="graph",
            notebook_id="notebook",
            source_id="source",
            provenance_kind="ppr",
            provenance_reference="graph",
            value=value,
            token_cost=0,
        )

    def propose(self):
        self.calls += 1
        with self.database.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return (self.proposal,)

    def read(self, identities):
        return (self.proposal,) if identities == ("graph",) else ()


def _call_context(source, database):
    return RetrievalContributionCallContext(
        actor_id="actor",
        notebook_id="notebook",
        scope_id="scope",
        scope_narrowed=True,
        run_id="run",
        run_kind="ask",
        cancellation=_Cancellation(),
        max_items=1,
        max_tokens=1,
        max_proposals=1,
        admission_source=source,
        selected_source_graph_source=source,
        connection_probe=database,
    )


def test_sqlite_connection_probe_blocks_fanout_then_allows_after_release(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'probe.db'}",
    )
    database = SqliteDatabase(settings, tmp_path)
    value = SimpleNamespace(chunk_id="graph")
    source = _DatabaseReadingSource(database, value)
    host = default_extension_runtime().retrieval_contributors
    baseline = [SimpleNamespace(chunk_id="base")]
    context = _call_context(source, database)

    with database.connect():
        blocked = host.run(
            baseline,
            invocation="selected_evidence",
            call_context=context,
            baseline_identity=lambda chunk: chunk.chunk_id,
            cancellation=context.cancellation,
        )

    assert blocked is baseline
    assert source.calls == 0

    accepted = host.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )

    assert [chunk.chunk_id for chunk in accepted] == ["base", "graph"]
    assert source.calls == 1
    database.close()


class _GraphService:
    def __init__(self, graph_chunk, *, cancel_event=None) -> None:
        self.graph_chunk = graph_chunk
        self.cancel_event = cancel_event
        self.failures = []

    def run(self, _notebook_id, baseline, **_kwargs):
        if self.cancel_event is not None:
            self.cancel_event.set()
            raise AskCancelled()
        status = SourceGraphStatus("active", "quality_approved")
        return ActivatedSourceGraphResult(
            (*baseline, self.graph_chunk),
            tuple(baseline),
            (self.graph_chunk,),
            status,
        )

    def fail_closed(self, _notebook_id, baseline, reason):
        self.failures.append(reason)
        status = SourceGraphStatus("degraded", reason)
        return ActivatedSourceGraphResult(
            tuple(baseline), tuple(baseline), (), status
        )


def _chunk(chunk_id):
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_id="source",
        source_title="Source",
        section_path="Section",
        text=chunk_id,
        element_ids=[f"element-{chunk_id}"],
    )


def _selected_call(service, baseline):
    return SelectedSourceGraphContributionCall(
        service,
        "notebook",
        baseline,
        max_results=5,
    )


def _selected_context(
    call, cancellation=None, admission_hydrate=None, admission_leaf_io=None
):
    return selected_source_graph_call_context(
        call,
        actor_id="actor",
        cancel_event=cancellation,
        connection_probe=SimpleNamespace(is_connection_held=lambda: False),
        admission_hydrate=(
            admission_hydrate
            or (lambda _notebook_id, _actor_id, _ids: ())
        ),
        admission_leaf_io=admission_leaf_io,
        max_results=5,
        max_tokens=100,
    )


def test_selected_graph_adapter_discards_whole_lane_when_authority_rejects():
    baseline = [_chunk("base")]
    service = _GraphService(_chunk("graph"))
    call = _selected_call(service, baseline)
    call.read = lambda _identities: ()
    context = _selected_context(call)

    host_result = default_extension_runtime().retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )
    visible, status = call.visible_result(host_result)

    assert host_result is baseline
    assert visible == baseline
    assert status.state == "degraded"
    assert status.reason == "extension_admission_failed"
    assert service.failures == ["extension_admission_failed"]


def test_selected_graph_adapter_propagates_native_request_cancellation():
    cancellation = threading.Event()
    baseline = [_chunk("base")]
    service = _GraphService(_chunk("graph"), cancel_event=cancellation)
    call = _selected_call(service, baseline)
    context = _selected_context(call, cancellation)

    with pytest.raises(AskCancelled):
        default_extension_runtime().retrieval_contributors.run(
            baseline,
            invocation="selected_evidence",
            call_context=context,
            baseline_identity=lambda chunk: chunk.chunk_id,
            cancellation=context.cancellation,
        )

    assert service.failures == []


def test_malformed_call_cancellation_is_fail_open_before_proposal_io():
    baseline = [_chunk("base")]
    service = _GraphService(_chunk("graph"))
    call = _selected_call(service, baseline)
    context = replace(_selected_context(call), cancellation=object())

    result = default_extension_runtime().retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )

    assert result is baseline
    assert call._attempted is False
    assert service.failures == []


def test_hostile_cancellation_truth_value_is_fail_open_before_proposal_io():
    class _HostileTruth:
        def __bool__(self):
            raise RuntimeError("hostile cancellation truth")

    class _HostileCancellation:
        def is_set(self):
            return _HostileTruth()

    baseline = [_chunk("base")]
    service = _GraphService(_chunk("graph"))
    call = _selected_call(service, baseline)
    cancellation = _HostileCancellation()
    context = replace(_selected_context(call), cancellation=cancellation)

    result = default_extension_runtime().retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=cancellation,
    )

    assert result is baseline
    assert call._attempted is False


def test_production_cancellation_adapter_rejects_hostile_event_truth():
    class _HostileTruth:
        def __bool__(self):
            raise RuntimeError("hostile cancellation truth")

    class _HostileEvent:
        def is_set(self):
            return _HostileTruth()

    baseline = [_chunk("base")]
    service = _GraphService(_chunk("graph"))
    call = _selected_call(service, baseline)
    context = _selected_context(call, cancellation=_HostileEvent())

    result = default_extension_runtime().retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )

    assert result is baseline
    assert call._attempted is False


def test_absent_graph_service_projects_capability_unavailable():
    baseline = [_chunk("base")]
    call = _selected_call(None, baseline)
    context = _selected_context(call)
    host = default_extension_runtime().retrieval_contributors

    projected = host._context_from_call("selected_evidence", context)
    result = host.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )

    assert context.selected_source_graph_source is None
    assert projected.selected_source_graph_access is None
    assert result is baseline


def test_malformed_graph_result_and_fail_closed_result_stay_optional():
    class _MalformedService:
        def run(self, *_args, **_kwargs):
            return SimpleNamespace(status=object())

        def fail_closed(self, *_args, **_kwargs):
            raise RuntimeError("malformed fallback")

    baseline = [_chunk("base")]
    call = _selected_call(_MalformedService(), baseline)
    context = _selected_context(call)

    result = default_extension_runtime().retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )
    visible, status = call.visible_result(result)

    assert result is baseline
    assert visible == baseline
    assert status is None


def test_graph_result_cannot_replace_frozen_baseline_with_shape_valid_chunks():
    baseline = [_chunk("base")]
    evil = _chunk("evil")
    graph = _chunk("graph")

    class _BaselineReplacingService(_GraphService):
        def run(self, *_args, **_kwargs):
            return ActivatedSourceGraphResult(
                (evil, graph),
                (evil,),
                (graph,),
                SourceGraphStatus("active", "quality_approved"),
            )

    service = _BaselineReplacingService(graph)
    call = _selected_call(service, baseline)
    context = _selected_context(call)

    result = default_extension_runtime().retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )
    visible, status = call.visible_result(result)

    assert result is baseline
    assert visible == baseline
    assert status.state == "degraded"
    assert service.failures == ["activation_seam_failed"]


def test_malformed_fail_closed_result_cannot_replace_frozen_baseline():
    baseline = [_chunk("base")]
    evil = _chunk("evil")

    class _MaliciousFallback:
        def run(self, *_args, **_kwargs):
            raise RuntimeError("activation failed")

        def fail_closed(self, *_args, **_kwargs):
            return ActivatedSourceGraphResult(
                (evil,),
                (evil,),
                (),
                SourceGraphStatus("degraded", "evil_fallback"),
            )

    call = _selected_call(_MaliciousFallback(), baseline)
    context = _selected_context(call)
    result = default_extension_runtime().retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )
    visible, status = call.visible_result(result)

    assert result is baseline
    assert visible == baseline
    assert status is None


def test_malformed_admission_fallback_cannot_replace_frozen_baseline():
    baseline = [_chunk("base")]
    graph = _chunk("graph")
    evil = _chunk("evil")

    class _MaliciousAdmissionFallback(_GraphService):
        def fail_closed(self, _notebook_id, _baseline, reason):
            self.failures.append(reason)
            return ActivatedSourceGraphResult(
                (evil,),
                (evil,),
                (),
                SourceGraphStatus("degraded", "evil_fallback"),
            )

    service = _MaliciousAdmissionFallback(graph)
    call = _selected_call(service, baseline)
    context = _selected_context(call)
    call.propose()

    visible, status = call.visible_result(baseline)

    assert visible == baseline
    assert status is None
    assert service.failures == ["extension_admission_failed"]


def test_shadow_baseline_copy_preserves_status_without_becoming_visible_graph():
    baseline = [_chunk("base")]
    graph = _chunk("graph")

    class _ShadowService(_GraphService):
        def run(self, *_args, **_kwargs):
            return ActivatedSourceGraphResult(
                (replace(baseline[0]),),
                tuple(baseline),
                (graph,),
                SourceGraphStatus(
                    "shadow", "shadow", enrichment_count=1
                ),
            )

    service = _ShadowService(graph)
    call = _selected_call(service, baseline)
    context = _selected_context(call)
    host_chunks = default_extension_runtime().retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )
    visible, status = call.visible_result(host_chunks)

    assert [chunk.chunk_id for chunk in visible] == ["base"]
    assert visible[0] is not baseline[0]
    assert status.state == "shadow"
    assert status.reason == "shadow"
    assert service.failures == []


class _IndependentContributor:
    invocations = frozenset({"selected_evidence"})

    def __init__(self) -> None:
        self.calls = 0

    def contribute(self, _context):
        self.calls += 1
        return ContributorResult((EvidenceCandidate(
            identity="other",
            notebook_id="notebook",
            source_id="source",
            provenance=EvidenceProvenance("chunk", "other"),
            value=object(),
            token_cost=1,
        ),), ExtensionResultStatus.AVAILABLE)


class _IndependentBundle:
    declaration = ContributionDeclaration(
        "builtin.independent",
        RETRIEVAL_CONTRIBUTOR_POINT,
        ContributionKind.CONTRIBUTOR,
    )
    manifest = ExtensionManifest(
        id="builtin.independent",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Independent",
        trust="builtin",
        contributions=(declaration,),
    )

    def __init__(self, implementation) -> None:
        self.implementation = implementation

    def register(self, registrar) -> None:
        registrar.add_contributor(ExtensionContribution(
            self.declaration, self.implementation
        ))


def test_absent_graph_capability_keeps_independent_real_host_contribution():
    contributor = _IndependentContributor()
    hydrate_calls = []

    def graph_availability(context):
        if (
            type(context) is RetrievalHostContext
            and context.selected_source_graph_access is not None
        ):
            return Availability.available()
        return Availability(
            AvailabilityStatus.UNAVAILABLE,
            "selected_source_graph_access_unavailable",
        )

    runtime = build_extension_runtime(
        (SELECTED_SOURCE_GRAPH_BUNDLE, _IndependentBundle(contributor)),
        capability_decisions={
            SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY: graph_availability,
        },
        retrieval_admission_policies={
            SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID: "atomic",
        },
    )
    baseline = [_chunk("base")]
    call = _selected_call(None, baseline)

    def hydrate(notebook_id, actor_id, identities):
        hydrate_calls.append((notebook_id, actor_id, identities))
        chunk = _chunk("other")
        chunk.notebook_id = "notebook"
        return [chunk]

    context = _selected_context(call, admission_hydrate=hydrate)
    result = runtime.retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )

    assert [chunk.chunk_id for chunk in result] == ["base", "other"]
    assert contributor.calls == 1
    assert hydrate_calls == [("notebook", "actor", ("other",))]


def test_graph_and_independent_authorities_share_host_without_extra_graph_io():
    contributor = _IndependentContributor()
    runtime = build_extension_runtime(
        (SELECTED_SOURCE_GRAPH_BUNDLE, _IndependentBundle(contributor)),
        capability_decisions={
            SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY: lambda _context: (
                Availability.available()
            ),
        },
        retrieval_admission_policies={
            SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID: "atomic",
        },
    )
    baseline = [_chunk("base")]
    call = _selected_call(_GraphService(_chunk("graph")), baseline)
    hydrate_calls = []
    leaf_slots = []

    class _LeafSlot:
        def __enter__(self):
            leaf_slots.append("enter")

        def __exit__(self, *_args):
            leaf_slots.append("exit")

    def hydrate(notebook_id, actor_id, identities):
        hydrate_calls.append((notebook_id, actor_id, identities))
        chunk = _chunk("other")
        chunk.notebook_id = notebook_id
        return [chunk]

    context = _selected_context(
        call,
        admission_hydrate=hydrate,
        admission_leaf_io=_LeafSlot,
    )
    result = runtime.retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )

    assert [chunk.chunk_id for chunk in result] == ["base", "other", "graph"]
    assert contributor.calls == 1
    assert hydrate_calls == [("notebook", "actor", ("other",))]
    assert leaf_slots == ["enter", "exit"]


def test_graph_only_authority_stays_in_memory_without_fallback_hydration():
    baseline = [_chunk("base")]
    service = _GraphService(_chunk("graph"))
    call = _selected_call(service, baseline)
    hydrate_calls = []

    class _ForbiddenLeafSlot:
        def __enter__(self):
            raise AssertionError("graph request-memory authority acquired leaf slot")

        def __exit__(self, *_args):
            return None

    context = _selected_context(
        call,
        admission_hydrate=lambda notebook_id, actor_id, identities: (
            hydrate_calls.append((notebook_id, actor_id, identities))
        ) or (),
        admission_leaf_io=_ForbiddenLeafSlot,
    )

    result = default_extension_runtime().retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )

    assert [chunk.chunk_id for chunk in result] == ["base", "graph"]
    assert hydrate_calls == []
