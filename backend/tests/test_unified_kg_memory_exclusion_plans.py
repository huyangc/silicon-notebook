"""SQLite plan pin for the E4-2 Memory exclusion in ``UnifiedKgStore``.

The statements are CAPTURED from the real store methods (SQLite's trace
callback, which reports the expanded text that actually ran) and then run
through ``EXPLAIN QUERY PLAN`` -- no ``ANALYZE`` statistics, as in production.
Pinned: every changed statement carries the Memory fragment; the fragment's
inner ``sources`` access (``ds``) and every by-id object probe are primary-key
SEARCHes, one point lookup per row; no statement SCANs ``sources``,
``knowledge_objects``, ``knowledge_relations`` or ``concept_clusters`` (a
rewrite into a shape with no index path shows up as a SCAN). The PostgreSQL
pins (custom and generic plans, other notebooks' Memory seeded) are in
``tests/postgres/test_memory_kg_seed_exclusion_pg.py``.
"""
from __future__ import annotations

import re
from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_kg_seed_world as world
from tests.model_testkit import bind_all_embedding_clients

_TABLES = ("sources", "knowledge_objects", "knowledge_relations", "concept_clusters")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _captured(repo, nb_id: str, monkeypatch) -> dict[str, str]:
    store = repo._runtime.unified_kg
    database = repo._runtime.database
    seen: list[str] = []
    real_connect = database.connect

    @contextmanager
    def traced():
        with real_connect() as db:
            db.set_trace_callback(seen.append)
            try:
                yield db
            finally:
                db.set_trace_callback(None)

    with traced() as db:
        list(store.seed_payload_rows(db, nb_id, "concept"))
        list(store.stream_seed_rows(db, nb_id, "concept"))
        list(store.canonical_relation_seed_rows(db, nb_id))
        store.mention_seed_rows(db, nb_id)
        list(store.community_graph_rows(db, nb_id)[1])
        store.community_rows_for_summary(db, nb_id, 0)
        store.catchup_window_members(db, nb_id, 0, "2026-01-01T00:00:00", 5, 100)
    monkeypatch.setattr(database, "connect", traced)
    store.cluster_size_histogram(nb_id)
    store.largest_clusters(nb_id)
    store.relation_provenance_counts(nb_id)
    monkeypatch.undo()
    roles = {
        "seed_payload": lambda s: s.startswith("SELECT o.payload AS payload"),
        "stream_seed": lambda s: s.startswith("SELECT o.id AS id, o.payload"),
        "canonical_relations": lambda s: "AS src_doc" in s,
        "mention_clusters": lambda s: "AS cname" in s,
        "mention_claims": lambda s: "AS nm FROM knowledge_objects" in s,
        "community_graph": lambda s: "FROM knowledge_relations kr" in s and "ORDER BY kr.id" in s,
        "community_summary": lambda s: "json_group_array" in s,
        "histogram": lambda s: "n_excluded" in s,
        "largest": lambda s: "AS members" in s,
        "provenance": lambda s: "endpoint_unusable" in s,
        "catchup": lambda s: "datetime(c.created_at)" in s,
    }
    found = {}
    for name, matches in roles.items():
        hits = [s for s in seen if matches(s.strip())]
        assert len(hits) == 1, (name, hits)
        found[name] = hits[0]
    return found


def test_memory_exclusion_statements_probe_by_primary_key_and_scan_nothing(repo, monkeypatch):
    nb_id = world.seed(repo)
    repo.rebuild_unified_kg(nb_id, force=True)
    statements = _captured(repo, nb_id, monkeypatch)
    with repo._runtime.database.connect() as db:
        for name, statement in statements.items():
            assert "ds.notebook_id" in statement and "'memory'" in statement, name
            plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + statement)]
            text = "\n".join(plan)
            ds = [line for line in plan if re.match(r"SEARCH ds\b", line)]
            assert ds and all("sqlite_autoindex_sources_1 (id=?)" in line for line in ds), (
                name, text)
            for line in plan:
                assert not re.match(r"SCAN (ds|x[sotm]|o|ko|kr|r|c|cc|so|tp|cs|ct)\b", line), (
                    name, text)
                for table in _TABLES:
                    assert not line.startswith(f"SCAN {table}"), (name, text)
            for probe in ("xs", "xt", "xo", "xm"):
                for line in plan:
                    if re.match(rf"SEARCH {probe}\b", line):
                        assert "sqlite_autoindex_knowledge_objects_1 (id=?)" in line, (
                            name, text)
