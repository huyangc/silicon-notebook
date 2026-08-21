from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.domain.extensions import (
    RetrievalContributionCallContext,
    RetrievalEvidenceProposal,
)
from app.extensions import default_extension_runtime
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
        proposal_source=source,
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


def _selected_context(call, cancellation=None):
    return selected_source_graph_call_context(
        call,
        actor_id="actor",
        cancel_event=cancellation,
        connection_probe=SimpleNamespace(is_connection_held=lambda: False),
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
