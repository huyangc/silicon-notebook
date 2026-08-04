from types import SimpleNamespace

from app.services.retrieval import RetrievedChunk, RetrievalSupport
from app.services.retrieval_enrichment import BaselineProtectedEnrichmentService
from app.services.source_graph_activation import SelectedSourceGraphActivationService
from app.services.source_graph_rollout import SourceGraphRolloutDecision
from app.services.source_scope import source_scope_context
from app.core.config import Settings


def _chunk(chunk_id: str, source_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_id=source_id,
        source_title=source_id,
        section_path="S",
        text=f"text-{chunk_id}",
        element_ids=[f"e-{chunk_id}"],
        relevance=0.8,
    )


class _Events:
    def __init__(self):
        self.rows = []

    def emit(self, row):
        self.rows.append(row)


def _service(*, snapshot, ppr, primitives=None):
    settings = SimpleNamespace(
        selected_source_graph_attestation_path="",
        selected_source_graph_expected_model_json="",
        selected_source_graph_notebook_allowlist="",
        selected_source_graph_rollout_mode="off",
        selected_source_graph_rollout_percent=0,
        selected_source_graph_expected_corpus_signature="",
        selected_source_graph_enrichment_tokens=1000,
    )
    events = _Events()
    service = SelectedSourceGraphActivationService(
        settings=settings,
        snapshots=SimpleNamespace(snapshot=lambda _nb, _sources: snapshot),
        primitives=primitives or SimpleNamespace(
            expand_graph=lambda *_args, **_kwargs: SimpleNamespace(
                capability=SimpleNamespace(enabled=False), nodes=()
            )
        ),
        online_ppr=SimpleNamespace(retrieve=lambda *_args, **_kwargs: ppr),
        partitioned_ppr=SimpleNamespace(),
        enrichment=BaselineProtectedEnrichmentService(),
        event_log=events,
    )
    return service, events


def test_quality_attestation_loader_keeps_digest_for_point_of_use_reverify(
    tmp_path, monkeypatch
):
    import json
    import app.services.source_graph_rollout as rollout

    path = tmp_path / "attestation.json"
    path.write_text(json.dumps({"attestation_digest": "signed", "approved": True}))
    monkeypatch.setattr(
        rollout,
        "verify_attestation",
        lambda value: {"approved": bool(value["approved"])},
    )

    loaded = rollout.load_quality_attestation(path)

    assert loaded == {"attestation_digest": "signed", "approved": True}


def test_selected_source_graph_rollout_defaults_are_inert():
    settings = Settings(_env_file=None)

    assert settings.selected_source_graph_rollout_mode == "off"
    assert settings.selected_source_graph_attestation_path == ""
    assert settings.selected_source_graph_rollout_percent == 0.0
    assert settings.selected_source_graph_enrichment_tokens == 4000


def test_whole_scope_is_byte_identical_and_does_no_snapshot_io():
    baseline = [_chunk("b", "a")]
    snapshots = SimpleNamespace(snapshot=lambda *_args: (_ for _ in ()).throw(
        AssertionError("whole scope must not build a selected-source snapshot")
    ))
    service, _events = _service(
        snapshot=None,
        ppr=SimpleNamespace(hits=()),
    )
    service._snapshots = snapshots

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": False}
    ):
        result = service.run("nb", baseline)

    assert result.status.state == "historical"
    assert result.chunks == tuple(baseline)
    assert result.enrichment_chunks == ()


def test_all_selected_participant_drift_is_visible_but_does_no_snapshot_io():
    baseline = [_chunk("b", "a")]
    service, events = _service(snapshot=None, ppr=SimpleNamespace(hits=()))
    service._snapshots = SimpleNamespace(
        snapshot=lambda *_args: (_ for _ in ()).throw(
            AssertionError("drift must fail before snapshot I/O")
        )
    )

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": False}
    ):
        result = service.run("nb", baseline, unsafe_scope_drift=True)

    assert result.chunks == tuple(baseline)
    assert result.status.state == "degraded"
    assert result.status.reason == "scope_drift"
    assert events.rows[-1]["reason"] == "scope_drift"


def test_active_lane_appends_graph_chunks_after_baseline(monkeypatch):
    baseline = [_chunk("b", "a")]
    graph_chunk = SimpleNamespace(
        chunk_id="g", source_id="a", section_path="G", text="graph",
        element_ids=("eg",),
    )
    support = RetrievalSupport("ppr", "ppr", "", 0.9)
    ppr = SimpleNamespace(
        hits=(
            SimpleNamespace(chunk=graph_chunk, score=0.9, support=support),
        ),
        cache_hit=True,
        capability=SimpleNamespace(enabled=True, reason=""),
    )
    snapshot = SimpleNamespace(
        allowed_source_ids=("a",),
        scope_hash="scope",
        nodes=(), relations=(), chunks=(graph_chunk,), memberships=(),
        degraded_reasons=(),
    )
    service, events = _service(snapshot=snapshot, ppr=ppr)
    monkeypatch.setattr(
        service,
        "_decision",
        lambda _nb: SourceGraphRolloutDecision(True, False, "quality_approved"),
    )

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        result = service.run("nb", baseline, source_titles=lambda _ids: {"a": "A"})

    assert [chunk.chunk_id for chunk in result.chunks] == ["b", "g"]
    assert result.status.state == "active"
    assert result.status.baseline_preserved is True
    assert result.status.post_scope_drop_count == 0
    assert events.rows[-1]["scope_hash"] == "scope"
    assert "text" not in events.rows[-1]


def test_post_scope_candidate_discards_entire_enrichment_lane(monkeypatch):
    baseline = [_chunk("b", "a")]
    outside = SimpleNamespace(
        chunk_id="x", source_id="outside", section_path="X", text="outside",
        element_ids=("ex",),
    )
    ppr = SimpleNamespace(
        hits=(SimpleNamespace(
            chunk=outside,
            score=0.9,
            support=RetrievalSupport("ppr", "ppr", "", 0.9),
        ),),
        cache_hit=False,
        capability=SimpleNamespace(enabled=True, reason=""),
    )
    snapshot = SimpleNamespace(
        allowed_source_ids=("a",), scope_hash="scope", nodes=(), relations=(),
        chunks=(), memberships=(), degraded_reasons=(),
    )
    service, _events = _service(snapshot=snapshot, ppr=ppr)
    monkeypatch.setattr(
        service,
        "_decision",
        lambda _nb: SourceGraphRolloutDecision(True, False, "quality_approved"),
    )

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        result = service.run("nb", baseline)

    assert result.chunks == tuple(baseline)
    assert result.enrichment_chunks == ()
    assert result.status.state == "degraded"
    assert result.status.reason == "post_scope_drop"


def test_shadow_lane_never_changes_visible_chunks(monkeypatch):
    baseline = [_chunk("b", "a")]
    graph_chunk = SimpleNamespace(
        chunk_id="g", source_id="a", section_path="G", text="graph",
        element_ids=("eg",),
    )
    ppr = SimpleNamespace(
        hits=(SimpleNamespace(
            chunk=graph_chunk,
            score=0.9,
            support=RetrievalSupport("ppr", "ppr", "", 0.9),
        ),),
        cache_hit=False,
        capability=SimpleNamespace(enabled=True, reason=""),
    )
    snapshot = SimpleNamespace(
        allowed_source_ids=("a",), scope_hash="scope", nodes=(), relations=(),
        chunks=(graph_chunk,), memberships=(), degraded_reasons=(),
    )
    service, _events = _service(snapshot=snapshot, ppr=ppr)
    monkeypatch.setattr(
        service,
        "_decision",
        lambda _nb: SourceGraphRolloutDecision(True, True, "shadow"),
    )

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        result = service.run("nb", baseline)

    assert result.status.state == "shadow"
    assert result.chunks == tuple(baseline)
    assert [chunk.chunk_id for chunk in result.enrichment_chunks] == ["g"]
