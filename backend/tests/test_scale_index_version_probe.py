"""P1-8: O(1) memoized _scale_index_version fast path.

Two guarantees under test:

1. CORRECTNESS (never serve a stale version): for the SAME notebook, the
   version key must CHANGE after every class of KG write (object add, relation
   add, chunk add, embedding write, cluster rebuild, object status flip via
   merge, edge review flip). This is the invariant the memoized fast path must
   NOT break — if kg_mutation_seq fails to advance on some write, the cache
   would serve a stale version and the scale index would go stale silently.

2. PERFORMANCE: when kg_mutation_seq is unchanged between two calls, the second
   call must NOT re-run the 5 COUNT/MAX aggregates (it returns the memoized
   list). We assert this by spying on db.execute and counting aggregate queries.
"""
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, MergeRequest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _add_concept(repo, nb_id, local_id, name):
    repo.store_kg(nb_id, None, [{
        "local_id": local_id, "object_type": "concept",
        "payload": {"name": name, "section_path": ""}, "evidence": [],
    }], [])


def _seq(repo, nb_id):
    with repo._connect() as db:
        r = db.execute(
            "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?", (nb_id,)
        ).fetchone()
    return int(r["kg_mutation_seq"]) if r else 0


def _add_chunk(repo, nb_id, cid, text):
    """Insert a chunk the way production does: write + bump kg_mutation_seq.

    In production chunk inserts go through _build_chunks_for_source whose callers
    (stage / build_notebook_kg) mark the KG dirty; we mirror that here so the
    seq-keyed fast path observes the change (a raw INSERT that bypasses the choke
    point would, correctly, be invisible to seq — that is not a production path).
    """
    from app.services.sqlite_repository import _now
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


def _add_embedding(repo, nb_id, obj_id):
    """Insert an embedding row for a NOT-yet-embedded object id (count 0→1),
    marking dirty as the production embed paths (_embed_knowledge callers) do."""
    from app.services.sqlite_repository import _now
    vec = json.dumps([0.1] * 16)
    with repo._write() as db:
        db.execute(
            "INSERT OR REPLACE INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)",
            (obj_id, nb_id, vec, _now()),
        )
    repo._mark_unified_kg_dirty(nb_id)


# ---------------------------------------------------------------------------
# Test 1 — CORRECTNESS: every write class changes the version key.
# ---------------------------------------------------------------------------

def test_version_changes_on_object_add(repo):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")
    v0 = repo._scale_index_version(nb.id)
    _add_concept(repo, nb.id, "b", "current mirror")
    v1 = repo._scale_index_version(nb.id)
    assert v1 != v0, "adding a knowledge_object must change the scale version"


def test_version_changes_on_relation_add(repo):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")
    _add_concept(repo, nb.id, "b", "current mirror")
    v0 = repo._scale_index_version(nb.id)
    # store_kg with a relation between the two already-present concepts.
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a",
         "edge_type": "depends_on", "evidence": []}])
    v1 = repo._scale_index_version(nb.id)
    assert v1 != v0, "adding a knowledge_relation must change the scale version"


def test_version_changes_on_chunk_add(repo):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")
    v0 = repo._scale_index_version(nb.id)
    _add_chunk(repo, nb.id, "c1", "a chunk about mosfets")
    v1 = repo._scale_index_version(nb.id)
    assert v1 != v0, "adding a chunk must change the scale version"


def test_version_changes_on_embedding_write(repo):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    # store_kg auto-embeds its object; add an embedding for a DIFFERENT, not-yet-
    # embedded object id so the embedding COUNT moves (independent of the object's
    # own auto-embed).
    _add_concept(repo, nb.id, "a", "MOSFET")
    v0 = repo._scale_index_version(nb.id)
    _add_embedding(repo, nb.id, "ko-standalone-emb")
    v1 = repo._scale_index_version(nb.id)
    assert v1 != v0, "writing a knowledge_embedding must change the scale version"


def test_merge_bumps_seq_and_does_not_mask_later_edit(repo):
    """merge_knowledge deprecates an object in place (COUNT unchanged; the
    version-key FORMAT — 5×COUNT/MAX — does not capture a review/status flip, so
    the returned list is intentionally identical for this edit, exactly as the
    pre-P1-8 code behaved). What the seq-keyed fast path REQUIRES is that this
    edit still bumps kg_mutation_seq, so a LATER content-changing edit is not
    masked by a stale memo. This test guards that gap (merge_knowledge previously
    did not mark dirty)."""
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "amplifier")
    _add_concept(repo, nb.id, "b", "amp")
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? ORDER BY id", (nb.id,)
        ).fetchall()
    src_id, into_id = rows[0]["id"], rows[1]["id"]
    repo._scale_index_version(nb.id)  # warm the memo
    s0 = _seq(repo, nb.id)
    repo.merge_knowledge(nb.id, src_id, MergeRequest(into_id=into_id))
    assert _seq(repo, nb.id) > s0, "merge_knowledge must bump kg_mutation_seq"
    # And a subsequent object add is still detected (memo not stale).
    v_after_merge = repo._scale_index_version(nb.id)
    _add_concept(repo, nb.id, "c", "buffer")
    assert repo._scale_index_version(nb.id) != v_after_merge


def test_edge_review_bumps_seq_and_does_not_mask_later_edit(repo):
    """set_edge_review flips review_status in place (relation COUNT/MAX(created_at)
    unchanged → the version-key content is intentionally identical, as pre-P1-8).
    Guards that the standalone edge-review API bumps kg_mutation_seq so it can't
    mask a later content edit under the fast path."""
    nb = repo.create_notebook(NotebookCreate(name="b"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a",
         "edge_type": "depends_on", "evidence": []}])
    with repo._connect() as db:
        rel_id = db.execute(
            "SELECT id FROM knowledge_relations WHERE notebook_id=?", (nb.id,)
        ).fetchone()["id"]
    repo._scale_index_version(nb.id)  # warm the memo
    s0 = _seq(repo, nb.id)
    repo.set_edge_review(nb.id, rel_id, "verified")
    assert _seq(repo, nb.id) > s0, "set_edge_review must bump kg_mutation_seq"
    v_after = repo._scale_index_version(nb.id)
    _add_concept(repo, nb.id, "z", "cascode")
    assert repo._scale_index_version(nb.id) != v_after


def test_cluster_rebuild_detected_despite_preserved_seq(repo):
    """rebuild_unified_kg rewrites concept_clusters but DELIBERATELY preserves
    kg_mutation_seq. The fast path must therefore never elide the cluster
    aggregates — this test locks in that a rebuild (which moves cluster COUNT) is
    still reflected in the version even though seq is unchanged across it."""
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")
    _add_concept(repo, nb.id, "b", "mosfet")
    s0 = _seq(repo, nb.id)
    v0 = repo._scale_index_version(nb.id)  # warm memo (0 clusters)
    repo.rebuild_unified_kg(nb.id, force=True)
    assert _seq(repo, nb.id) == s0, "rebuild is expected to preserve seq (PR#132 invariant)"
    v1 = repo._scale_index_version(nb.id)
    assert v1 != v0, (
        "rebuild changed concept_clusters; the version must reflect it even though "
        "kg_mutation_seq is preserved across the rebuild"
    )


# ---------------------------------------------------------------------------
# Test 2 — PERFORMANCE: unchanged seq ⇒ second call skips the 5 aggregates.
# ---------------------------------------------------------------------------

def test_unchanged_seq_skips_noncluster_aggregates(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")

    # Warm the cache (first call computes + memoizes).
    v_first = repo._scale_index_version(nb.id)

    # Spy: count aggregate queries per table on the next call. With seq unchanged
    # the fast path must skip the FOUR non-cluster tables' aggregates entirely,
    # and read clusters exactly once (rebuild can move clusters without bumping
    # seq, so they are never elided).
    orig_connect = repo._connect
    noncluster = {"n": 0}
    cluster = {"n": 0}

    class _SpyCursor:
        def __init__(self, cur):
            self._cur = cur

        def fetchone(self):
            return self._cur.fetchone()

        def fetchall(self):
            return self._cur.fetchall()

        def __getattr__(self, name):
            return getattr(self._cur, name)

    class _SpyConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **k):
            s = " ".join(str(sql).split())
            if "COUNT(*)" in s:
                if "concept_clusters" in s:
                    cluster["n"] += 1
                elif (
                    "knowledge_objects" in s or "knowledge_relations" in s
                    or "FROM chunks" in s or "knowledge_embeddings" in s
                ):
                    noncluster["n"] += 1
            return _SpyCursor(self._conn.execute(sql, *a, **k))

        def __enter__(self):
            self._ctx = orig_connect()
            self._conn = self._ctx.__enter__()
            return self

        def __exit__(self, *a):
            return self._ctx.__exit__(*a)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(repo, "_connect", lambda: _SpyConn(None))

    v_second = repo._scale_index_version(nb.id)

    assert v_second == v_first, "memoized fast path must return the same version"
    assert noncluster["n"] == 0, (
        "with unchanged kg_mutation_seq the second call must NOT run the four "
        f"non-cluster COUNT/MAX aggregates; ran {noncluster['n']}"
    )
    assert cluster["n"] == 1, (
        "clusters must be re-read every call (never elided by seq); "
        f"cluster aggregate ran {cluster['n']} times"
    )


def test_changed_seq_recomputes(repo):
    """After a mutation bumps seq, the fast path must fall through and recompute
    (already covered by Test 1, but asserted here as the seq-transition case)."""
    nb = repo.create_notebook(NotebookCreate(name="b"))
    _add_concept(repo, nb.id, "a", "MOSFET")
    v0 = repo._scale_index_version(nb.id)
    v0b = repo._scale_index_version(nb.id)
    assert v0b == v0
    _add_concept(repo, nb.id, "b", "current mirror")
    v1 = repo._scale_index_version(nb.id)
    assert v1 != v0
