"""A3 (perf-audit P1-4 follow-on): notebook_copy_stats per-version memo.

notebook_copy_stats runs 5 aggregate queries (bytes/sources/chunks/nodes/
edges) and is now on ask-path guards (_scale_index_eligible,
maybe_auto_index) plus share/copy paths (share_notebook, shared_preview,
shared_by_me). Memoize it the same way _scale_index_version/edge_centrality/
clustermap already do: VectorCache keyed on _scale_index_version(nb).

Three guarantees under test:
1. Memo hit: two calls with no intervening mutation run the loader exactly
   once (spy on db.execute's aggregate queries).
2. Refresh after mutation: a chunk write (which bumps kg_mutation_seq, part
   of _scale_index_version) must invalidate the memo — the next call re-runs
   the aggregates and reflects the new count.
3. Output equality: the memoized dict is identical in shape/values to the
   unmemoized computation (no behavior change, only caching).
"""
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_embedding_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_embedding_client(r, FakeEmbedder(dim=16))
    return r


def _add_concept(repo, nb_id, local_id, name):
    repo.store_kg(nb_id, None, [{
        "local_id": local_id, "object_type": "concept",
        "payload": {"name": name, "section_path": ""}, "evidence": [],
    }], [])


def _add_chunk(repo, nb_id, cid, text):
    """Mirror _build_chunks_for_source's production write + dirty-mark."""
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"src-{cid}", nb_id, "t", "md", "ready", now, now),
        )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, nb_id, f"src-{cid}", text, "", "[]", now),
        )
    repo._mark_unified_kg_dirty(nb_id)


class _SpyCursor:
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __getattr__(self, name):
        return getattr(self._cur, name)


def _make_spy_connect(orig_connect, counter, tables):
    class _SpyConn:
        def __init__(self):
            self._ctx = None
            self._conn = None

        def execute(self, sql, *a, **k):
            s = " ".join(str(sql).split())
            if any(t in s for t in tables) and ("SUM(" in s or "COUNT(*)" in s):
                counter["n"] += 1
            return _SpyCursor(self._conn.execute(sql, *a, **k))

        def __enter__(self):
            self._ctx = orig_connect()
            self._conn = self._ctx.__enter__()
            return self

        def __exit__(self, *a):
            return self._ctx.__exit__(*a)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    return lambda: _SpyConn()


def test_copy_stats_memo_hit_runs_loader_once(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")

    first = repo.notebook_copy_stats(nb.id)

    counter = {"n": 0}
    orig_connect = repo._connect
    monkeypatch.setattr(
        repo, "_connect",
        _make_spy_connect(orig_connect, counter,
                           ("sources", "chunks", "knowledge_objects", "knowledge_relations")))

    second = repo.notebook_copy_stats(nb.id)
    assert second == first
    assert counter["n"] == 0, (
        "second call with no intervening mutation must be a memo hit "
        f"(0 aggregate queries); ran {counter['n']}"
    )


def test_copy_stats_memo_refreshes_after_chunk_write(repo):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")

    before = repo.notebook_copy_stats(nb.id)
    assert before["size"]["chunks"] == 0

    _add_chunk(repo, nb.id, "c1", "chunk text")

    after = repo.notebook_copy_stats(nb.id)
    assert after["size"]["chunks"] == 1, (
        "a chunk write (which bumps kg_mutation_seq / _scale_index_version) "
        "must invalidate the memo so the next call reflects the new count"
    )


def test_copy_stats_output_matches_unmemoized_shape(repo):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")
    stats = repo.notebook_copy_stats(nb.id)
    assert set(stats) == {"copyable", "size"}
    assert set(stats["size"]) == {"bytes", "sources", "chunks", "nodes", "edges"}
    assert stats["size"]["nodes"] == 1


def test_copy_stats_invalidated_by_invalidate_unified_cache(repo):
    """Sibling invalidation convention: _invalidate_unified_cache must also
    evict the copystats memo key (defense against same-second in-place edits
    with an unchanged version tuple)."""
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")
    repo.notebook_copy_stats(nb.id)  # warm
    key = f"{nb.id}:copystats"
    assert key in repo._vector_cache._store
    repo._invalidate_unified_cache(nb.id)
    assert key not in repo._vector_cache._store
