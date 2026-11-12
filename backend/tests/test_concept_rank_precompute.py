"""Ranking publication, freshness and admission through the real SQLite lifecycle."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.domain.concept_ranking_contracts import top_concept_ranks
from app.models.kg_analysis import KgAnalysisResponse
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.kg_findings_world import seed_world
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'rank.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    yield repository
    repository.close()


def _header(repo, notebook_id):
    with repo._runtime.unified_kg.read_snapshot() as db:
        return repo._runtime.unified_kg.concept_rank_header(db, notebook_id)


def _seq(repo, notebook_id):
    with repo._runtime.unified_kg.read_snapshot() as db:
        state = repo._runtime.unified_kg.state_row(db, notebook_id)
        return int(state["kg_mutation_seq"]), int(state["cluster_mutation_seq"])


def _delete_ranks(repo, notebook_id):
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM kg_concept_ranks WHERE notebook_id=?", (notebook_id,))


def test_precompute_matches_full_scope_including_top80_ties_and_orphans(repo):
    world = seed_world(repo, postgres=False)
    nb = world.notebook_id
    with repo._runtime.database.write() as db:
        # More than 80 equal-count canonicals exercise the full tie boundary.
        for index in range(100):
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,'concept','approved','{\"name\":\"rank singleton\"}',"
                "'[]','src-core-0','2026-11-12','2026-11-12')",
                (f"rank-singleton-{index:03d}", nb))
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "VALUES ('rank-orphan',?,'concept','approved','{\"name\":\"orphan\"}',"
            "'[]','owner-missing','2026-11-12','2026-11-12')", (nb,))
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(nb)
    store = repo._runtime.unified_kg
    with store.read_snapshot() as db:
        expected = top_concept_ranks(store.concept_rank_full_rows(db, nb, ceiling=None))
        actual = store.concept_rank_precomputed_rows(db, nb)
    assert len(actual) == 80
    assert [(r["canonical_id"], r["source_count"], r["object_count"])
            for r in actual] == expected
    assert all(r["representative_object_id"] for r in actual)
    assert all(r["canonical_id"] != "rank-orphan" for r in actual)
    assert _header(repo, nb) == _seq(repo, nb)


def test_empty_notebook_publishes_header_and_overview_reads_it_outside_cache(repo):
    nb = repo.create_notebook(NotebookCreate(name="empty")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(nb)
    assert _header(repo, nb) == _seq(repo, nb)
    service = repo._runtime.kg_analysis
    from dataclasses import asdict
    overview = service.overview(nb)
    header = _header(repo, nb)
    assert overview.concept_ranks_built_at_seq == {"kg_seq": header[0], "cluster_seq": header[1]}
    assert KgAnalysisResponse(**asdict(overview)).concept_ranks_built_at_seq.kg_seq == header[0]
    _delete_ranks(repo, nb)
    assert service.overview(nb).concept_ranks_built_at_seq is None
    lifecycle.rebuild_communities(nb, ensure_concept_ranks=True)
    assert _header(repo, nb) == _seq(repo, nb)


def test_ensure_gate_repairs_only_when_requested(repo):
    nb = seed_world(repo, postgres=False).notebook_id
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(nb)
    _delete_ranks(repo, nb)
    lifecycle.rebuild_communities(nb, ensure_concept_ranks=False)
    assert _header(repo, nb) is None
    lifecycle.rebuild_communities(nb, ensure_concept_ranks=True)
    assert _header(repo, nb) == _seq(repo, nb)


def test_ensure_gate_replaces_stale_header_and_rows_only_when_requested(repo):
    nb = seed_world(repo, postgres=False).notebook_id
    lifecycle = repo._runtime.knowledge_lifecycle
    store = repo._runtime.unified_kg
    lifecycle.rebuild_communities(nb)
    with store.read_snapshot() as db:
        expected = store.concept_rank_precomputed_rows(db, nb)
    with repo._runtime.database.write() as db:
        db.execute("UPDATE kg_concept_ranks SET built_at_cluster_seq=-1 "
                   "WHERE notebook_id=? AND rank=0", (nb,))
        db.execute("UPDATE kg_concept_ranks SET source_count=999 "
                   "WHERE notebook_id=? AND rank=1", (nb,))
    lifecycle.rebuild_communities(nb, ensure_concept_ranks=False)
    assert _header(repo, nb)[1] == -1
    with store.read_snapshot() as db:
        assert store.concept_rank_precomputed_rows(db, nb)[0]["source_count"] == 999
    lifecycle.rebuild_communities(nb, ensure_concept_ranks=True)
    assert _header(repo, nb) == _seq(repo, nb)
    with store.read_snapshot() as db:
        assert store.concept_rank_precomputed_rows(db, nb) == expected


def test_visible_owner_read_failure_preserves_ranks_and_publishes_other_artifacts(repo, monkeypatch):
    nb = seed_world(repo, postgres=False).notebook_id
    lifecycle = repo._runtime.knowledge_lifecycle
    store = repo._runtime.unified_kg
    lifecycle.rebuild_communities(nb)
    old_header = _header(repo, nb)
    with store.read_snapshot() as db:
        old_rows = store.concept_rank_precomputed_rows(db, nb)
    with repo._runtime.database.write() as db:
        db.execute("UPDATE unified_kg_state SET kg_mutation_seq=kg_mutation_seq+1 "
                   "WHERE notebook_id=?", (nb,))
    events = []
    monkeypatch.setattr(lifecycle.event_log, "emit", events.append)
    def fail(*args):
        raise RuntimeError("private-source-details")
    monkeypatch.setattr(store, "concept_rank_visible_source_ids", fail)
    lifecycle.rebuild_communities(nb, force=True)
    assert _header(repo, nb) == old_header
    with store.read_snapshot() as db:
        assert store.concept_rank_precomputed_rows(db, nb) == old_rows
        ledger = store.kg_analysis_artifact_rows(db, nb)
        assert ledger and all(row["kg_mutation_seq"] == old_header[0] + 1
                              for row in ledger.values())
        assert store.kg_analysis_finding_rows(db, nb)
    assert [e for e in events if e["kind"] == "concept_rank_visible_sources_failed"] == [
        {"kind": "concept_rank_visible_sources_failed", "notebook_id": nb,
         "error_class": "RuntimeError"}]


def test_representative_read_failure_publishes_ranks_with_canonical_fallback(repo, monkeypatch):
    nb = seed_world(repo, postgres=False).notebook_id
    lifecycle = repo._runtime.knowledge_lifecycle
    store = repo._runtime.unified_kg
    events = []
    monkeypatch.setattr(lifecycle.event_log, "emit", events.append)
    def fail(*args):
        raise RuntimeError("private-object-details")
    monkeypatch.setattr(store, "concept_rank_representatives", fail)
    lifecycle.rebuild_communities(nb)
    assert _header(repo, nb) == _seq(repo, nb)
    with store.read_snapshot() as db:
        rows = store.concept_rank_precomputed_rows(db, nb)
        assert rows and all(row["representative_object_id"] == "" for row in rows)
        assert [(r["canonical_id"], r["source_count"], r["object_count"]) for r in rows] == (
            top_concept_ranks(store.concept_rank_full_rows(db, nb, ceiling=None)))
        assert store.kg_analysis_finding_rows(db, nb)
    assert [e for e in events if e["kind"] == "concept_rank_representatives_failed"] == [
        {"kind": "concept_rank_representatives_failed", "notebook_id": nb,
         "error_class": "RuntimeError"}]


def _set_current_cluster_version(repo, nb):
    lifecycle = repo._runtime.knowledge_lifecycle
    version = lifecycle._cluster_input_version(nb, facts=lifecycle._cluster_input_facts(nb))
    with repo._runtime.database.write() as db:
        db.execute("UPDATE unified_kg_state SET cluster_input_version=? WHERE notebook_id=?",
                   (version, nb))
    return version


def test_skip_decision_reuses_its_single_state_read(repo, monkeypatch):
    nb = seed_world(repo, postgres=False).notebook_id
    lifecycle = repo._runtime.knowledge_lifecycle
    store = repo._runtime.unified_kg
    version = _set_current_cluster_version(repo, nb)
    original = store.state_row
    reads = []
    def read(db, notebook_id):
        row = original(db, notebook_id)
        reads.append(row)
        return row
    monkeypatch.setattr(store, "state_row", read)
    settled = []
    monkeypatch.setattr(lifecycle, "_settle_skip_path", lambda nb, row, ver: settled.append(row))
    for name in ("rebuild_canonical_relations", "rebuild_mention_bridge", "rebuild_communities"):
        monkeypatch.setattr(lifecycle, name, lambda *a, **kw: None)
    assert lifecycle._unified_rebuild_skip(nb, version, None) is not None
    assert len(reads) == 1
    assert settled[0] is reads[0]


def test_background_rebuild_skip_passes_ensure_flag_and_settles(repo, monkeypatch):
    nb = seed_world(repo, postgres=False).notebook_id
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(nb)
    _set_current_cluster_version(repo, nb)
    _delete_ranks(repo, nb)
    # Any entry into a real recluster would violate this unchanged-input scenario.
    def unexpected(*args, **kwargs):
        raise AssertionError("unchanged inputs must skip clustering")
    monkeypatch.setattr(lifecycle, "_unified_rebuild_claim", unexpected)
    assert not lifecycle.concept_rank_refresh_settled(nb)
    job = lifecycle.start_unified_kg_rebuild(nb)
    lifecycle.run_unified_kg_rebuild_job(nb, job["job_id"], ensure_concept_ranks=True)
    assert _header(repo, nb) == _seq(repo, nb)
    assert lifecycle.concept_rank_refresh_settled(nb)
    assert "model_calls_possible" not in lifecycle.unified_kg_rebuild_status(nb)


def test_rank_producibility_disabled_short_circuits(repo, monkeypatch):
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(lifecycle.settings, "community_layer_enabled", False)
    monkeypatch.setattr(lifecycle, "_community_build_refused",
                        lambda nb: pytest.fail("disabled feature must not probe graph admission"))
    assert lifecycle.concept_ranks_producible("unused") is False


def test_model_call_prediction_shares_skip_predicate_and_running_shortcut(repo, monkeypatch):
    nb = seed_world(repo, postgres=False).notebook_id
    lifecycle = repo._runtime.knowledge_lifecycle
    configured = set()
    monkeypatch.setattr(lifecycle.model_clients, "configured", lambda name: name in configured)
    original = lifecycle._cluster_input_version
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(lifecycle, "_cluster_input_version", counted)
    assert lifecycle.rebuild_would_call_models(nb) is False
    assert calls == []
    version = _set_current_cluster_version(repo, nb)
    configured.add("kg_merge_review")
    assert lifecycle._cluster_inputs_unchanged(nb, version)
    assert lifecycle.rebuild_would_call_models(nb) is False
    assert lifecycle._unified_rebuild_skip(nb, version, None) is not None
    with repo._runtime.database.write() as db:
        db.execute("UPDATE unified_kg_state SET kg_mutation_seq=kg_mutation_seq+1 "
                   "WHERE notebook_id=?", (nb,))
    assert lifecycle.rebuild_would_call_models(nb) is True
    new_version = original(nb, facts=lifecycle._cluster_input_facts(nb))
    assert lifecycle._unified_rebuild_skip(nb, new_version, None) is None
    job = lifecycle.start_unified_kg_rebuild(nb)
    before = len(calls)
    monkeypatch.setattr(lifecycle.kg_maintenance, "_require_notebook",
                        lambda *a: pytest.fail("probe must read maintenance state in memory"))
    assert lifecycle.rebuild_would_call_models(nb) is False
    assert len(calls) == before
    lifecycle.fail_unified_kg_rebuild_submission(nb, job["job_id"])


def test_unscoped_catalog_counts_share_memo_without_resolving_ceiling(repo, monkeypatch):
    nb = seed_world(repo, postgres=False).notebook_id
    catalog = repo._runtime.collection_catalog
    original = catalog._unscoped_kg_counts
    calls = []
    def counted(db, notebook_id):
        calls.append(notebook_id)
        return original(db, notebook_id)
    monkeypatch.setattr(catalog, "_unscoped_kg_counts", counted)
    with repo._runtime.database.connect() as db:
        expected = dict(catalog._notebook_kg_counts(db, nb, ceiling_binds=False))
    assert calls == [nb]
    monkeypatch.setattr(catalog, "source_ceiling",
                        lambda *a, **kw: pytest.fail("unscoped counts must not resolve ceiling"))
    actual = catalog.unscoped_usable_counts(nb)
    assert actual == expected
    assert calls == [nb]
    actual["concept"] = -1
    assert catalog.unscoped_usable_counts(nb) == expected
