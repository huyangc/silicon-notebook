import threading
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.extensions import default_extension_runtime
from app.models.admin import AskDetail
from app.models.ask import ActiveAskJob, AskResponse
from app.models.reports import ReportDetail
from app.models.schemas import NotebookCreate
from app.services.ask_service import AskService
from app.services.retrieval import RetrievedChunk, RetrievalSupport
from app.services.retrieval_enrichment import BaselineProtectedEnrichmentService
from app.services.report_engine import ReportEngine
from app.services.source_graph_activation import SelectedSourceGraphActivationService
from app.services.source_graph_rollout import SourceGraphRolloutDecision
from app.services.source_scope import source_scope_context
from app.services.sqlite_repository import SQLiteRepository


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


def _service(*, snapshot, ppr, primitives=None, partitioned_ppr=None):
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
        partitioned_ppr=partitioned_ppr or SimpleNamespace(),
        enrichment=BaselineProtectedEnrichmentService(),
        event_log=events,
    )
    return service, events


def _snapshot(*chunks):
    return SimpleNamespace(
        allowed_source_ids=("a",),
        scope_hash="scope",
        nodes=(),
        relations=(),
        chunks=chunks,
        memberships=(),
        degraded_reasons=(),
    )


def _enable_active(service, monkeypatch):
    monkeypatch.setattr(
        service,
        "_decision",
        lambda _nb: SourceGraphRolloutDecision(True, False, "quality_approved"),
    )


def _wire_ask_graph_host(ask):
    ask.retrieval_contributors = default_extension_runtime().retrieval_contributors
    ask.retrieval_connection_probe = SimpleNamespace(
        is_connection_held=lambda: False
    )
    ask.retrieval_contributor_hydrate = (
        lambda _notebook_id, _actor_id, _ids: ()
    )
    ask.current_user_id = lambda: "ask-user"
    ask.settings = SimpleNamespace(
        selected_source_graph_enrichment_tokens=1000,
    )
    ask.event_log = _Events()


def _wire_report_graph_host(report, dependencies):
    dependencies.retrieval_contributors = (
        default_extension_runtime().retrieval_contributors
    )
    dependencies.retrieval_connection_probe = SimpleNamespace(
        is_connection_held=lambda: False
    )
    dependencies.retrieval_contributor_hydrate = (
        lambda _notebook_id, _actor_id, _ids: ()
    )
    dependencies.event_log = _Events()
    report.user_id = "report-user"
    report.cancel_event = None


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


def test_selected_source_graph_rollout_defaults_to_invisible_shadow():
    settings = Settings(_env_file=None)

    assert settings.selected_source_graph_rollout_mode == "shadow"
    assert settings.source_subgraph_ppr_enabled is True
    assert settings.source_partitioned_graph_artifacts_enabled is True
    assert settings.source_partitioned_ppr_enabled is True
    assert settings.selected_source_graph_attestation_path == ""
    assert settings.selected_source_graph_rollout_percent == 0.0
    assert settings.selected_source_graph_enrichment_tokens == 4000


def test_selected_source_graph_rollout_state_is_not_part_of_public_ask_schema():
    assert "source_graph" not in AskResponse.model_json_schema()["properties"]


def test_legacy_selected_source_graph_state_is_scrubbed_at_public_model_boundaries():
    internal_step = {
        "step_type": "source_subgraph",
        "summary": "来源子图：shadow",
        "detail": {"state": "shadow"},
    }
    public_step = {"step_type": "retrieve", "summary": "retrieved"}
    answer = AskResponse(
        conclusion="ok",
        reasoning_trace=[internal_step, public_step],
    ).model_dump()
    assert [step["step_type"] for step in answer["reasoning_trace"]] == ["retrieve"]

    active = ActiveAskJob(
        job_id="job", trace=[internal_step, public_step]
    ).model_dump()
    assert active["trace"] == [public_step]

    admin = AskDetail(
        job_id="job",
        notebook_id="nb",
        trace=[internal_step, public_step],
        answer={
            "conclusion": "ok",
            "source_graph": {"state": "shadow"},
            "reasoning_trace": [internal_step, public_step],
        },
    ).model_dump()
    assert admin["trace"] == [public_step]
    assert "source_graph" not in admin["answer"]
    assert admin["answer"]["reasoning_trace"] == [public_step]

    report = ReportDetail(
        id="rep",
        question="q",
        status="done",
        sections=[{
            "title": "section",
            "source_graph": {"state": "shadow"},
        }],
    ).model_dump()
    assert report["sections"] == [{"title": "section"}]


def test_legacy_report_source_graph_receipt_is_scrubbed_on_repository_read(tmp_path):
    repo = SQLiteRepository(Settings(
        _env_file=None,
        DATABASE_URL=f"sqlite:///{tmp_path / 'legacy.db'}",
        SILICON_NOTEBOOK_STORAGE_DIR=str(tmp_path / "storage"),
    ))
    notebook = repo.create_notebook(NotebookCreate(name="legacy report"))
    report_id = repo.create_report(notebook.id, "q")
    repo.update_report(
        notebook.id,
        report_id,
        sections=[{
            "title": "section",
            "source_graph": {"state": "shadow"},
        }],
    )

    detail = repo.get_report(notebook.id, report_id)

    assert detail["sections"] == [{"title": "section"}]


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


def test_all_selected_drift_callback_uses_leaf_gate_and_propagates_cancel():
    from app.services.cancellation import AskCancelled
    from app.services.retrieval_run import retrieval_fanout_slot, retrieval_run

    baseline = [_chunk("b", "a")]
    service, _events = _service(
        snapshot=None, ppr=SimpleNamespace(hits=())
    )
    calls = []

    with retrieval_run(
        run_kind="report_generation", fanout_limit=1
    ) as state, source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": False}
    ):
        result = service.run(
            "nb",
            baseline,
            unsafe_scope_drift=lambda: calls.append("probe") or False,
            leaf_io=retrieval_fanout_slot,
        )

    assert result.status.state == "historical"
    assert calls == ["probe"]
    assert state.fanout_acquires == 1

    cancel_event = threading.Event()
    cancel_event.set()
    calls.clear()
    with pytest.raises(AskCancelled), retrieval_run(
        run_kind="report_generation",
        fanout_limit=1,
        cancel_event=cancel_event,
    ), source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": False}
    ):
        service.run(
            "nb",
            baseline,
            unsafe_scope_drift=lambda: calls.append("cancelled-probe") or False,
            leaf_io=retrieval_fanout_slot,
        )
    assert calls == []


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


def test_report_activation_routes_each_graph_leaf_through_retrieval_run(monkeypatch):
    from app.services.retrieval_run import retrieval_run

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
    service, _events = _service(snapshot=_snapshot(graph_chunk), ppr=ppr)
    _enable_active(service, monkeypatch)
    report = object.__new__(ReportEngine)
    report.dependencies = SimpleNamespace(
        selected_source_graph=service,
        source_query=SimpleNamespace(source_titles=lambda _ids: {"a": "A"}),
        selected_graph_hydrate=lambda _ids: (),
        scale_version=lambda _nb: "v",
        retrieval=SimpleNamespace(
            unsafe_source_scope_restricted=lambda _nb: True
        ),
    )
    _wire_report_graph_host(report, report.dependencies)
    report.settings = SimpleNamespace(
        ppr_top_chunks=20,
        selected_source_graph_enrichment_tokens=1000,
    )
    result = SimpleNamespace(chunks=list(baseline), top_hits=())

    with retrieval_run(
        run_kind="report_generation", fanout_limit=1
    ) as state, source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        report._activate_selected_source_graph("nb", result)

    # snapshot, source-title read and online PPR are separate leaves.  The
    # activation orchestrator itself never owns a slot around service.run().
    assert state.fanout_acquires == 3
    assert [chunk.chunk_id for chunk in result.chunks] == ["b", "g"]


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


def test_builtin_plugin_preserves_shadow_status_and_single_graph_event(monkeypatch):
    legacy_baseline = [_chunk("b", "a")]
    plugin_baseline = [_chunk("b", "a")]
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
    legacy, legacy_events = _service(
        snapshot=_snapshot(graph_chunk), ppr=ppr
    )
    plugin, plugin_events = _service(
        snapshot=_snapshot(graph_chunk), ppr=ppr
    )
    for service in (legacy, plugin):
        monkeypatch.setattr(
            service,
            "_decision",
            lambda _nb: SourceGraphRolloutDecision(True, True, "shadow"),
        )

    ask = object.__new__(AskService)
    ask.selected_source_graph = plugin
    ask.source_titles = lambda _ids: {"a": "A"}
    ask.selected_graph_hydrate = lambda _ids: ()
    ask.scale_version = lambda _nb: "v"
    ask.retrieval = SimpleNamespace(
        unsafe_source_scope_restricted=lambda _nb: True
    )
    _wire_ask_graph_host(ask)

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        legacy_result = legacy.run(
            "nb",
            legacy_baseline,
            source_titles=lambda _ids: {"a": "A"},
            max_results=20,
        )
        plugin_chunks, plugin_status = ask._activate_selected_source_graph(
            "nb", plugin_baseline
        )

    assert plugin_chunks == list(legacy_result.chunks)
    assert plugin_status == legacy_result.status
    assert plugin_events.rows == legacy_events.rows
    assert [row["state"] for row in plugin_events.rows] == ["shadow"]


def test_builtin_plugin_preserves_active_chunks_status_and_graph_event(monkeypatch):
    legacy_baseline = [_chunk("b", "a")]
    plugin_baseline = [_chunk("b", "a")]
    duplicate = SimpleNamespace(
        chunk_id="b", source_id="a", section_path="B", text="duplicate",
        element_ids=("eb",),
    )
    graph_chunk = SimpleNamespace(
        chunk_id="g", source_id="a", section_path="G", text="graph",
        element_ids=("eg",),
    )
    ppr = SimpleNamespace(
        hits=(
            SimpleNamespace(
                chunk=duplicate,
                score=0.95,
                support=RetrievalSupport("ppr", "ppr", "", 0.95),
            ),
            SimpleNamespace(
                chunk=graph_chunk,
                score=0.9,
                support=RetrievalSupport("ppr", "ppr", "", 0.9),
            ),
        ),
        cache_hit=False,
        capability=SimpleNamespace(enabled=True, reason=""),
    )
    legacy, legacy_events = _service(
        snapshot=_snapshot(duplicate, graph_chunk), ppr=ppr
    )
    plugin, plugin_events = _service(
        snapshot=_snapshot(duplicate, graph_chunk), ppr=ppr
    )
    _enable_active(legacy, monkeypatch)
    _enable_active(plugin, monkeypatch)

    ask = object.__new__(AskService)
    ask.selected_source_graph = plugin
    ask.source_titles = lambda _ids: {"a": "A"}
    ask.selected_graph_hydrate = lambda _ids: ()
    ask.scale_version = lambda _nb: "v"
    ask.retrieval = SimpleNamespace(
        unsafe_source_scope_restricted=lambda _nb: True
    )
    _wire_ask_graph_host(ask)

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        legacy_result = legacy.run(
            "nb",
            legacy_baseline,
            source_titles=lambda _ids: {"a": "A"},
            max_results=20,
        )
        plugin_chunks, plugin_status = ask._activate_selected_source_graph(
            "nb", plugin_baseline
        )

    assert plugin_chunks == list(legacy_result.chunks)
    assert plugin_status == legacy_result.status
    assert plugin_events.rows == legacy_events.rows
    assert [chunk.chunk_id for chunk in plugin_chunks] == ["b", "g"]
    assert plugin_chunks[0].retrieval_supports == (
        RetrievalSupport("ppr", "ppr", "", 0.95),
    )


def test_source_title_failure_returns_frozen_baseline(monkeypatch):
    baseline = [_chunk("b", "a")]
    ppr = SimpleNamespace(
        hits=(), cache_hit=False,
        capability=SimpleNamespace(enabled=True, reason=""),
    )
    service, _events = _service(snapshot=_snapshot(), ppr=ppr)
    _enable_active(service, monkeypatch)

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        result = service.run(
            "nb",
            baseline,
            source_titles=lambda _ids: (_ for _ in ()).throw(RuntimeError("db")),
        )

    assert result.chunks == tuple(baseline)
    assert result.enrichment_chunks == ()
    assert result.status.state == "degraded"
    assert result.status.reason == "source_titles_failed"


def test_ppr_failure_discards_graph_primitive_lane(monkeypatch):
    baseline = [_chunk("b", "a")]
    graph_calls = []
    primitives = SimpleNamespace(expand_graph=lambda *_args, **_kwargs: (
        graph_calls.append(True),
        SimpleNamespace(
            capability=SimpleNamespace(enabled=True, reason=""), nodes=()
        ),
    )[1])
    service, _events = _service(
        snapshot=_snapshot(), ppr=SimpleNamespace(), primitives=primitives
    )
    service._online_ppr = SimpleNamespace(
        retrieve=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ppr"))
    )
    _enable_active(service, monkeypatch)

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        result = service.run("nb", baseline, object_seeds={"o": 1.0})

    assert result.chunks == tuple(baseline)
    assert result.status.reason == "ppr_run_failed"
    assert graph_calls == []


def test_graph_failure_discards_successful_ppr_lane(monkeypatch):
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
    primitives = SimpleNamespace(
        expand_graph=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("graph")
        )
    )
    service, _events = _service(
        snapshot=_snapshot(graph_chunk), ppr=ppr, primitives=primitives
    )
    _enable_active(service, monkeypatch)

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        result = service.run("nb", baseline, object_seeds={"o": 1.0})

    assert result.chunks == tuple(baseline)
    assert result.enrichment_chunks == ()
    assert result.status.reason == "graph_expand_failed"


def test_partition_failure_and_hydration_failure_return_baseline(monkeypatch):
    from app.services.retrieval_run import retrieval_fanout_slot, retrieval_run

    baseline = [_chunk("b", "a")]
    ppr = SimpleNamespace(
        hits=(), cache_hit=False,
        capability=SimpleNamespace(enabled=False, reason="ppr_node_limit_exceeded"),
    )
    partitioned = SimpleNamespace(
        retrieve=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("partition")
        )
    )
    service, _events = _service(
        snapshot=_snapshot(), ppr=ppr, partitioned_ppr=partitioned
    )
    _enable_active(service, monkeypatch)

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        failed_partition = service.run(
            "nb", baseline, parent_version="v", hydrate_chunk_ids=lambda _ids: ()
        )

    assert failed_partition.chunks == tuple(baseline)
    assert failed_partition.status.reason == "source_partition_artifact_load_failed"

    partitioned.retrieve = lambda *_args, **_kwargs: SimpleNamespace(
        capability=SimpleNamespace(enabled=True, reason=""),
        cache_hit=False,
        hits=(SimpleNamespace(
            chunk_id="g", source_id="a", score=0.9,
            support=RetrievalSupport("ppr", "ppr", "", 0.9),
        ),),
    )
    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        failed_hydration = service.run(
            "nb",
            baseline,
            parent_version="v",
            hydrate_chunk_ids=lambda _ids: (_ for _ in ()).throw(RuntimeError("db")),
        )

    assert failed_hydration.chunks == tuple(baseline)
    assert failed_hydration.status.reason == "source_partition_artifact_load_failed"

    with retrieval_run(
        run_kind="report_generation", fanout_limit=1
    ) as state, source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        recovered = service.run(
            "nb",
            baseline,
            parent_version="v",
            hydrate_chunk_ids=lambda _ids: [_chunk("g", "a")],
            leaf_io=retrieval_fanout_slot,
        )

    assert [chunk.chunk_id for chunk in recovered.chunks] == ["b", "g"]
    assert recovered.status.state == "active"
    assert "ppr_node_limit_exceeded" in recovered.status.degraded_reasons
    # snapshot, online PPR capability probe, partitioned PPR and hydration.
    assert state.fanout_acquires == 4


def test_partition_mixed_missing_or_out_of_scope_hydration_discards_all_g(
    monkeypatch,
):
    baseline = [_chunk("b", "a")]
    ppr = SimpleNamespace(
        hits=(), cache_hit=False,
        capability=SimpleNamespace(enabled=False, reason="ppr_node_limit_exceeded"),
    )
    partitioned = SimpleNamespace()
    service, _events = _service(
        snapshot=_snapshot(), ppr=ppr, partitioned_ppr=partitioned
    )
    _enable_active(service, monkeypatch)

    valid_hit = SimpleNamespace(
        chunk_id="g", source_id="a", score=0.9,
        support=RetrievalSupport("ppr", "ppr", "", 0.9),
    )
    outside_hit = SimpleNamespace(
        chunk_id="x", source_id="outside", score=0.8,
        support=RetrievalSupport("ppr", "ppr", "", 0.8),
    )
    missing_hit = SimpleNamespace(
        chunk_id="missing", source_id="a", score=0.7,
        support=RetrievalSupport("ppr", "ppr", "", 0.7),
    )
    cases = (
        ((valid_hit, outside_hit), [_chunk("g", "a"), _chunk("x", "outside")]),
        ((valid_hit, missing_hit), [_chunk("g", "a")]),
    )
    for hits, hydrated_rows in cases:
        partitioned.retrieve = lambda *_args, _hits=hits, **_kwargs: SimpleNamespace(
            capability=SimpleNamespace(enabled=True, reason=""),
            cache_hit=False,
            hits=_hits,
        )
        with source_scope_context(
            "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
        ):
            result = service.run(
                "nb",
                baseline,
                parent_version="v",
                hydrate_chunk_ids=lambda _ids, _rows=hydrated_rows: _rows,
            )

        assert result.chunks == tuple(baseline)
        assert result.enrichment_chunks == ()
        assert result.status.state == "degraded"
        assert result.status.reason == "source_partition_hydration_mismatch"


def test_ask_shared_seam_preserves_historical_shape_and_laziness(monkeypatch):
    baseline = [_chunk("b", "a")]
    service, _events = _service(snapshot=None, ppr=SimpleNamespace())
    service._snapshots = SimpleNamespace(
        snapshot=lambda *_args: (_ for _ in ()).throw(
            AssertionError("all-selected must not snapshot")
        )
    )
    ask = object.__new__(AskService)
    ask.selected_source_graph = service
    ask.source_titles = lambda _ids: (_ for _ in ()).throw(
        AssertionError("all-selected must not load titles")
    )
    ask.selected_graph_hydrate = lambda _ids: ()
    ask.scale_version = lambda _nb: (_ for _ in ()).throw(
        AssertionError("all-selected must not open scale metadata")
    )
    ask.retrieval = SimpleNamespace(unsafe_source_scope_restricted=lambda _nb: False)
    _wire_ask_graph_host(ask)

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": False}
    ):
        chunks, status = ask._activate_selected_source_graph("nb", baseline)

    assert chunks == baseline
    assert status is None


def test_graph_mode_freezes_real_source_chunk_baseline_before_appending_g():
    baseline = [_chunk("b", "a")]
    baseline[0].score = 0.42
    baseline[0].relevance = 0.73
    graph_chunk = _chunk("g", "a")
    seen = {}
    ask = object.__new__(AskService)
    ask.graph = SimpleNamespace(source_chunks=lambda _nb, object_ids: (
        seen.setdefault("object_ids", object_ids), baseline
    )[1])
    ask.settings = SimpleNamespace(ppr_top_chunks=20)

    def activate(_notebook_id, chunks, **_kwargs):
        seen["baseline"] = list(chunks)
        return [*chunks, graph_chunk], SimpleNamespace(state="active")

    ask._activate_selected_source_graph = activate
    subgraph = [({"object_id": "o", "name": "O"}, None, None)]

    chunks, status = AskService._graph_source_chunks_with_activation(
        ask, "nb", subgraph, (), unsafe_scope=True
    )

    assert seen["object_ids"] == ["o"]
    assert seen["baseline"] == baseline
    assert [chunk.chunk_id for chunk in chunks] == ["b", "g"]
    assert chunks[0] is baseline[0]
    assert chunks[0].score == 0.42
    assert chunks[0].relevance == 0.73
    assert status.state == "active"


def test_ask_and_report_consumers_keep_baseline_on_graph_io_failure(monkeypatch):
    baseline = [_chunk("b", "a")]
    ppr = SimpleNamespace(
        hits=(), cache_hit=False,
        capability=SimpleNamespace(enabled=False, reason="ppr_node_limit_exceeded"),
    )
    partitioned = SimpleNamespace(retrieve=lambda *_args, **_kwargs: SimpleNamespace(
        capability=SimpleNamespace(enabled=True, reason=""),
        cache_hit=False,
        hits=(),
    ))
    service, _events = _service(
        snapshot=_snapshot(), ppr=ppr, partitioned_ppr=partitioned
    )
    _enable_active(service, monkeypatch)

    ask = object.__new__(AskService)
    ask.selected_source_graph = service
    ask.source_titles = lambda _ids: {"a": "A"}
    ask.selected_graph_hydrate = lambda _ids: ()
    ask.scale_version = lambda _nb: (_ for _ in ()).throw(RuntimeError("scale"))
    ask.retrieval = SimpleNamespace(unsafe_source_scope_restricted=lambda _nb: True)
    _wire_ask_graph_host(ask)

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        ask_chunks, ask_status = ask._activate_selected_source_graph("nb", baseline)

    assert ask_chunks == baseline
    assert ask_status.state == "degraded"
    assert ask_status.reason == "source_partition_artifact_load_failed"

    dependencies = SimpleNamespace(
        selected_source_graph=service,
        source_query=SimpleNamespace(
            source_titles=lambda _ids: (_ for _ in ()).throw(RuntimeError("titles"))
        ),
        selected_graph_hydrate=lambda _ids: (),
        scale_version=lambda _nb: "v",
        retrieval=SimpleNamespace(unsafe_source_scope_restricted=lambda _nb: True),
    )
    report = object.__new__(ReportEngine)
    report.dependencies = dependencies
    _wire_report_graph_host(report, dependencies)
    report.settings = SimpleNamespace(
        ppr_top_chunks=20,
        selected_source_graph_enrichment_tokens=1000,
    )
    result = SimpleNamespace(chunks=list(baseline), top_hits=(), trace=[])

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["a"], "narrowed": True}
    ):
        report._activate_selected_source_graph("nb", result)

    assert result.chunks == baseline
    assert not hasattr(result, "source_graph_status")
    assert result.trace == []
