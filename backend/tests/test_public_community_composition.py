"""Community/report compatibility adapters consume public component ports."""
from __future__ import annotations

from types import SimpleNamespace

from app.services.communities import CommunityQueryService
from app.services.reasoning_retrieval import ReasoningRetriever
from app.services.report_engine import ReportEngine
from app.services.retrieval_service import RetrievalService


class _Unified:
    def first_base_notebook_id(self, active_notebook_id):
        return "base" if active_notebook_id != "base" else None

    def resolve_focal(self, notebook_id, key):
        return "focal" if key else None

    def comention_peers(self, notebook_id, focal, min_bridges, top_k):
        return [(f"peer-{min_bridges}", min_bridges)]


def _community_service(threshold=2):
    return CommunityQueryService(
        notebooks=object(),
        unified_kg=_Unified(),
        event_log=SimpleNamespace(emit=lambda event: None),
        sibling_min_bridge=threshold,
    )


def test_community_service_exposes_sibling_peers_with_configured_threshold():
    assert _community_service(7).sibling_peers(
        "base", "Focal", top_k=3
    ) == [("peer-7", 7)]


def test_retrieval_community_provider_observes_current_settings_per_use():
    settings = SimpleNamespace(sibling_min_bridge=2)
    service = RetrievalService(
        candidates=object(),
        graph=object(),
        community_queries=lambda override=None: _community_service(
            (override or settings).sibling_min_bridge
        ),
    )

    first = service.community_queries()
    settings.sibling_min_bridge = 9
    second = service.community_queries()

    assert first is not second
    assert first.sibling_min_bridge == 2
    assert second.sibling_min_bridge == 9


def test_reasoning_factory_gets_communities_from_public_retrieval_port():
    community = _community_service(5)

    class _Retrieval:
        def community_queries(self):
            return community

        def federated_retrieve(self, *args, **kwargs):
            return []

    class _Repository:
        retrieval = _Retrieval()
        reasoning_llm_client = SimpleNamespace(configured=False)

    retriever = ReasoningRetriever.from_repository(
        _Repository(), SimpleNamespace(reasoning_quota_enabled=False)
    )

    assert retriever.retrieval is _Repository.retrieval
    assert retriever.communities is community


def test_report_engine_repository_adapter_uses_public_execution_factory():
    marker = object()
    repository_settings = object()
    calls = []

    class _Execution:
        def engine_factory(self, *, user_id, cancel_event, settings=None):
            calls.append((user_id, cancel_event, settings))
            return marker

    repository = SimpleNamespace(
        report_execution=_Execution(),
        current_user=lambda: SimpleNamespace(id="user-1"),
    )

    assert ReportEngine.from_repository(
        repository, repository_settings, "cancel"
    ) is marker
    assert calls == [("user-1", "cancel", repository_settings)]


def test_report_engine_repository_adapter_preserves_subclass_factory():
    dependencies = SimpleNamespace(settings=object())
    base = ReportEngine(dependencies, user_id="user-1", cancel_event="cancel")
    repository = SimpleNamespace(
        report_execution=SimpleNamespace(engine_factory=lambda **kwargs: base),
        current_user=lambda: SimpleNamespace(id="user-1"),
    )

    class SpecializedReportEngine(ReportEngine):
        pass

    result = SpecializedReportEngine.from_repository(
        repository, dependencies.settings, "cancel"
    )

    assert type(result) is SpecializedReportEngine
    assert result.dependencies is dependencies
    assert result.user_id == "user-1"
    assert result.cancel_event == "cancel"
