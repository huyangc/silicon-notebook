"""E4-4 (plan correction 8): an id that a scale index built BEFORE the Memory
isolation still carries — another member's Memory-derived object or relation
— is judged where each ``load(allow_stale=True)`` reader hydrates it.

The five readers, and where each stale id is stopped:

1. relation ANN (``RetrievalCandidates._retrieve_relations_scored``):
   hydrated by id (``relation_context_rows``), dropped by the run's source
   ceiling at the result boundary (``filter_retrieval_items``) — pinned below;
2. KG ANN (``_retrieve_scored``): hydrated by id (``retrieval_objects``),
   evidence cut to the run's ceiling right after hydration and an object with
   no surviving evidence dropped — pinned below;
3. chunk ANN: a Memory source has no chunk (E4-1 write guard, E4-5 migration
   deletes pre-guard rows), so a stale chunk id hydrates to nothing — pinned
   by ``test_memory_chunk_guard.py`` / the E4-5 migration test;
4. relation completion (``KnowledgeLifecycleService`` ANN over-fetch):
   hydrated by ``completion_candidate_rows``, which only returns objects of
   the source being completed — pinned below;
5. Tier-2 bridge (incremental fusion): hydrated by
   ``valid_object_ids(exclude_memory_derived=True)`` — pinned by E4-3's
   ``test_incremental_fusion.py``.

Readers 1 and 2 are retrieval legs: the viewer's ceiling (D1: the notebook's
visible sources plus the viewer's own hidden ones) is what judges them; each
pin runs the reader without a scope first (control: the stale id really
reaches hydration).  The folded-name half of correction 8 (a pre-isolation
cluster minted from a Memory seed) is a service fold (``cluster_fold_rows`` +
E4-7's relabel), pinned by E4-7's mixed-cluster search tests.  PostgreSQL
twin: ``postgres/test_memory_stale_index_readers_pg.py``.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories.sqlite import knowledge_counts_cache
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_kg_reader_cases as cases
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("RELATION_RETRIEVAL_ENABLED", "true")
    repo = SQLiteRepository(Settings())
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    knowledge_counts_cache.invalidate()
    return cases.build_world(repo, postgres=False)


def test_kg_ann_stale_label_of_foreign_memory_is_dropped_at_hydration(world, monkeypatch):
    cases.check_stale_kg_ann_label(world, monkeypatch)


def test_relation_ann_stale_label_of_foreign_memory_is_dropped_at_the_boundary(
    world, monkeypatch,
):
    cases.check_stale_relation_ann_label(world, monkeypatch)


def test_relation_completion_hydrates_only_the_completed_sources_objects(world):
    cases.check_stale_completion_candidates(world)
