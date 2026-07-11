"""Task 26 — every facade compatibility handle is the runtime-owned object.

The consolidated facade publishes write-through properties over the ONE
RepositoryRuntime: reading through the facade returns the runtime object BY
IDENTITY, and writing through the facade retargets every runtime consumer.
No facade-only copies of mutable state may exist.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.vector_cache import VectorCache


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path / 't.db'}",
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
        auth_optional=True,
    ))


def test_settings_storage_and_database_identities(repo):
    runtime = repo._runtime
    assert repo.settings is runtime.settings
    assert repo.settings is runtime.database.settings
    assert repo.storage_dir is runtime.source_files.storage_dir
    assert repo.event_log is runtime.event_log
    assert repo.db_path is runtime.database.db_path
    assert repo._write_lock is runtime.database.write_lock
    assert repo._user_model_cfg_cache is runtime.identity.model_config_cache
    assert repo._user_model_cfg_cache is runtime.models.model_config_cache


def test_storage_and_embedder_replacements_reach_composed_consumers(repo, tmp_path):
    _ = repo.retrieval
    replacement_dir = tmp_path / "elsewhere"
    replacement_embedder = object()
    replacement_languages = {"nb-1": ["zh", "en"]}

    repo.storage_dir = replacement_dir
    repo.embedder = replacement_embedder
    repo._notebook_langs_cache = replacement_languages

    runtime = repo._runtime
    assert "storage_dir" not in repo.__dict__
    assert "embedder" not in repo.__dict__
    assert "_notebook_langs_cache" not in repo.__dict__
    assert runtime.storage_dir is replacement_dir
    assert runtime.source_files.storage_dir is replacement_dir
    assert runtime.notebook_copies._storage_dir() is replacement_dir
    assert runtime.source_ingestion.source_files.storage_dir is replacement_dir
    assert runtime.source_embedding.embedder() is replacement_embedder
    assert runtime.source_ingestion.embedding.embedder() is replacement_embedder
    assert repo.retrieval.candidates.embedder is replacement_embedder
    assert repo.retrieval.graph.embedder is replacement_embedder
    assert runtime.notebook_languages is replacement_languages
    assert runtime.kg_mutations.notebook_languages is replacement_languages
    assert repo.retrieval.candidates._notebook_langs_cache is replacement_languages
    assert repo.retrieval.graph._notebook_langs_cache is replacement_languages


def test_model_client_setters_write_through_to_the_provider(repo):
    llm = object()
    repo.llm_client = llm
    assert repo._runtime.models._system_llm_client is llm
    assert repo.llm_client is llm
    rerank = object()
    repo.rerank_client = rerank
    assert repo._runtime.models._system_rerank_client is rerank


def test_retrieval_and_evidence_services_have_one_owner(repo):
    service = repo.retrieval
    runtime = repo._runtime
    assert service is runtime.retrieval
    assert service.candidates is runtime.candidate_retrieval
    assert service.graph is runtime.graph_retrieval
    assert runtime.evidence_context is not None
    assert runtime.evidence_context.knowledge is runtime.graph_retrieval


def test_vector_cache_swap_write_through(repo):
    runtime = repo._runtime
    assert repo._vector_cache is runtime.retrieval_snapshots.vector_cache
    replacement = VectorCache(max_entries=4)
    repo._vector_cache = replacement
    assert runtime.retrieval_snapshots.vector_cache is replacement
    # The KG mutation coordinator reads through the snapshot cache, so the
    # swap is observed there without a second owner.
    assert runtime.kg_mutations.snapshots is runtime.retrieval_snapshots


def test_unified_cache_swap_write_through(repo):
    runtime = repo._runtime
    assert repo._unified_cache is runtime.retrieval_snapshots.unified_cache
    replacement: dict = {}
    repo._unified_cache = replacement
    assert runtime.retrieval_snapshots.unified_cache is replacement
    assert runtime.knowledge_lifecycle.unified_cache is replacement
    assert runtime.kg_mutations.unified_cache is replacement


def test_scale_and_viz_state_has_one_owner(repo):
    runtime = repo._runtime
    artifacts = runtime.scale_artifacts
    assert repo._scale_idx_cache is artifacts.scale_cache
    assert repo._viz_idx_cache is artifacts.viz_cache
    assert repo._scale_ver_cache is artifacts.version_memo
    assert repo._scale_ver_lock is artifacts.version_lock
    assert repo._scale_ver_locks is artifacts.version_locks
    assert repo._scale_idx_load_lock is artifacts.load_lock
    assert repo._scale_idx_load_locks is artifacts.load_locks
    assert repo._scale_idle_queue is artifacts.idle_queue
    assert repo._viz_building is artifacts.viz_building
    assert repo._viz_building_lock is artifacts.viz_building_lock
    assert repo._scale_building is artifacts.building
    assert repo._scale_building is runtime.scale_builder.building
    assert repo._scale_building_lock is artifacts.building_lock
    assert repo._scale_building_lock is runtime.scale_builder.building_lock
    assert repo._auto_index_checked is artifacts.auto_index_checked
    assert repo._auto_index_checked is runtime.kg_mutations.auto_index_checked


def test_scale_build_state_swaps_retarget_builder_and_coordinator(repo):
    runtime = repo._runtime
    building: set = set()
    repo._scale_building = building
    assert runtime.scale_artifacts.building is building
    assert runtime.scale_builder.building is building
    checked: set = set()
    repo._auto_index_checked = checked
    assert runtime.scale_artifacts.auto_index_checked is checked
    assert runtime.kg_mutations.auto_index_checked is checked
    repo._scale_scheduler_started = True
    assert runtime.scale_artifacts.scheduler_started is True


def test_kg_building_and_language_hint_identities(repo):
    runtime = repo._runtime
    assert repo._kg_building is runtime.knowledge_lifecycle.kg_building
    assert repo._kg_building is runtime.catalog.kg_building
    assert repo._kg_building_lock is runtime.knowledge_lifecycle.kg_building_lock
    assert repo._notebook_langs_cache is runtime.kg_mutations.notebook_languages


def test_ask_and_report_cancellation_registries_have_one_owner(repo):
    from app.services.report_execution import REPORT_CANCELLATIONS

    runtime = repo._runtime
    assert repo._ask_cancel_events is runtime.ask_cancellations.events
    assert repo._ask_cancel_lock is runtime.ask_cancellations.lock
    assert runtime.ask_execution.cancellations is runtime.ask_cancellations
    assert runtime.report_cancellations is REPORT_CANCELLATIONS
    assert repo.report_execution is runtime.report_execution
    assert runtime.report_execution.cancellations is REPORT_CANCELLATIONS
