"""P0-5: _ent_chunk_map / _kg_source_chunks 版本缓存 + element→chunk 反查。

_ent_chunk_map used to full-scan ALL knowledge_objects.evidence + ALL
chunks.element_ids (per-row json.loads), uncached, on every call — paid by
the PPR-fallback query path. _kg_source_chunks full-scanned all chunks and
set-intersected per query, paid by the default chunk-overlay + graph-mode
paths. Both are now version-cached via repo._vector_cache (same single-flight
+ LRU-32 machinery as _vector_matrix/_keyword_token_sets), keyed on
tuple(repo._scale_index_version(nb)).

These tests:
  1. verify the cache actually elides the SQL scan on a second call (spy on
     repo._connect call count around the boundary of interest);
  2. verify KG mutations bump the version and force a recompute;
  3. verify the new/rewritten implementations are byte-equivalent to the OLD
     full-scan implementations (copied here as test-local oracles) across
     several shapes: empty object_ids, object with no evidence, multi-chunk
     fan-out, element_id with no chunk hit.
"""
import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    r.embedder = FakeEmbedder(dim=16)
    return r


# ── oracles: verbatim copies of the OLD (pre-P0-5) full-scan implementations ──

def _oracle_ent_chunk_map(repo, notebook_id):
    with repo._connect() as db:
        obj_rows = db.execute(
            "SELECT id, evidence FROM knowledge_objects WHERE notebook_id=?",
            (notebook_id,),
        ).fetchall()
        chunk_rows = db.execute(
            "SELECT id, element_ids FROM chunks WHERE notebook_id=?",
            (notebook_id,),
        ).fetchall()
    elem_to_chunks = {}
    for cr in chunk_rows:
        for el in json.loads(cr["element_ids"] or "[]"):
            elem_to_chunks.setdefault(el, set()).add(cr["id"])
    out = {}
    for orow in obj_rows:
        chunks = set()
        for e in json.loads(orow["evidence"] or "[]"):
            if isinstance(e, dict) and e.get("element_id"):
                chunks |= elem_to_chunks.get(e["element_id"], set())
        if chunks:
            out[orow["id"]] = chunks
    return out


def _oracle_kg_source_chunks(repo, notebook_id, object_ids):
    from app.services.retrieval import RetrievedChunk
    if not object_ids:
        return []
    with repo._connect() as db:
        ph = ",".join("?" * len(object_ids))
        erows = db.execute(
            f"SELECT evidence FROM knowledge_objects WHERE id IN ({ph})", list(object_ids)).fetchall()
        elem_ids = set()
        for r in erows:
            for e in json.loads(r["evidence"] or "[]"):
                if isinstance(e, dict) and e.get("element_id"):
                    elem_ids.add(e["element_id"])
        if not elem_ids:
            return []
        crows = db.execute(
            "SELECT id, source_id, text, section_path, element_ids FROM chunks WHERE notebook_id=?",
            (notebook_id,)).fetchall()
    out, seen = [], set()
    for cr in crows:
        cids = set(json.loads(cr["element_ids"] or "[]"))
        if cids & elem_ids and cr["id"] not in seen:
            seen.add(cr["id"])
            out.append(RetrievedChunk(
                chunk_id=cr["id"], source_id=cr["source_id"], source_title="",
                section_path=cr["section_path"], text=cr["text"],
                element_ids=json.loads(cr["element_ids"] or "[]"), relevance=0.3))
    return out


# ── fixtures: seed a small KG with evidence/chunks ─────────────────────────

def _now():
    return "2026-07-02T00:00:00"


_SEED_SEQ = {"n": 0}


def _seed(repo, nb_id, *, n_objects=3, chunks_per_elem=1, dangling=False):
    """n_objects concepts, each with one evidence element_id `el{i}`, and
    matching chunks. dangling=True adds one extra object whose evidence
    element_id has no matching chunk (exercise the 'no chunk hit' branch).
    Each call uses a fresh id namespace (call-scoped seq) so a notebook can
    be seeded more than once (e.g. to exercise cache invalidation)."""
    _SEED_SEQ["n"] += 1
    tag = f"{nb_id}-{_SEED_SEQ['n']}"
    now = _now()
    with repo._write() as db:
        src_id = f"s-{tag}"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", (src_id, nb_id, "t", "md", "ready", now, now))
        oids = []
        for i in range(n_objects):
            el = f"el-{tag}-{i}"
            oid = f"ko-{tag}-{i}"
            ev = json.dumps([{"source_id": src_id, "source_title": "", "element_id": el,
                              "element_type": "paragraph", "location_label": "p",
                              "quoted_span": f"span{i}", "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects "
                       "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb_id, "concept", "approved", "", json.dumps({"name": f"concept{i}"}), ev,
                        src_id, now, now))
            oids.append(oid)
            for j in range(chunks_per_elem):
                cid = f"c-{tag}-{i}-{j}"
                db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                           "VALUES (?,?,?,?,?,?,?)",
                           (cid, nb_id, src_id, f"text {i} {j}", "sec", json.dumps([el]), now))
        # object with no evidence at all
        oid_empty = f"ko-{tag}-empty"
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (oid_empty, nb_id, "concept", "approved", "", json.dumps({"name": "noev"}), "[]",
                    src_id, now, now))
        oids.append(oid_empty)
        if dangling:
            oid_d = f"ko-{tag}-dangling"
            ev = json.dumps([{"source_id": src_id, "source_title": "", "element_id": "ghost-elem",
                              "element_type": "paragraph", "location_label": "p",
                              "quoted_span": "x", "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects "
                       "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid_d, nb_id, "concept", "approved", "", json.dumps({"name": "dangling"}), ev,
                        src_id, now, now))
            oids.append(oid_d)
    repo._mark_unified_kg_dirty(nb_id)
    return oids


def _connect_spy(repo, monkeypatch):
    calls = {"n": 0}
    orig = repo._connect

    def _wrapped():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(repo, "_connect", _wrapped)
    return calls


# ── equivalence: _ent_chunk_map vs oracle ───────────────────────────────────

def test_ent_chunk_map_equals_oracle_basic(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=3, chunks_per_elem=1)
    assert repo._ent_chunk_map(nb.id) == _oracle_ent_chunk_map(repo, nb.id)


def test_ent_chunk_map_equals_oracle_multi_chunk_fanout(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=2, chunks_per_elem=3)
    got = repo._ent_chunk_map(nb.id)
    want = _oracle_ent_chunk_map(repo, nb.id)
    assert got == want
    assert all(len(v) == 3 for v in got.values())


def test_ent_chunk_map_equals_oracle_dangling_element(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=2, dangling=True)
    dangling_oid = oids[-1]
    assert dangling_oid.endswith("-dangling")
    got = repo._ent_chunk_map(nb.id)
    want = _oracle_ent_chunk_map(repo, nb.id)
    assert got == want
    # dangling object's evidence element_id matches no chunk -> excluded from map
    assert dangling_oid not in got


def test_ent_chunk_map_equals_oracle_empty_notebook(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    assert repo._ent_chunk_map(nb.id) == _oracle_ent_chunk_map(repo, nb.id) == {}


# ── equivalence: _kg_source_chunks vs oracle ────────────────────────────────

def test_kg_source_chunks_equals_oracle_basic(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=3, chunks_per_elem=1)
    got = repo._kg_source_chunks(nb.id, oids)
    want = _oracle_kg_source_chunks(repo, nb.id, oids)
    got_tuples = [(c.chunk_id, c.source_id, c.text, c.section_path, tuple(c.element_ids), c.relevance) for c in got]
    want_tuples = [(c.chunk_id, c.source_id, c.text, c.section_path, tuple(c.element_ids), c.relevance) for c in want]
    assert set(got_tuples) == set(want_tuples)
    assert len(got) == len(want)


def test_kg_source_chunks_equals_oracle_multi_chunk_fanout(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=2, chunks_per_elem=3)
    got = repo._kg_source_chunks(nb.id, oids)
    want = _oracle_kg_source_chunks(repo, nb.id, oids)
    assert {c.chunk_id for c in got} == {c.chunk_id for c in want}
    assert len(got) == len(want) == 6


def test_kg_source_chunks_equals_oracle_empty_object_ids(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=2)
    assert repo._kg_source_chunks(nb.id, []) == _oracle_kg_source_chunks(repo, nb.id, []) == []


def test_kg_source_chunks_equals_oracle_object_no_evidence(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=2)
    empty_oid = oids[-1]
    assert empty_oid.endswith("-empty")
    got = repo._kg_source_chunks(nb.id, [empty_oid])
    want = _oracle_kg_source_chunks(repo, nb.id, [empty_oid])
    assert got == want == []


def test_kg_source_chunks_equals_oracle_dangling_element(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=1, dangling=True)
    dangling_oid = oids[-1]
    assert dangling_oid.endswith("-dangling")
    got = repo._kg_source_chunks(nb.id, [dangling_oid])
    want = _oracle_kg_source_chunks(repo, nb.id, [dangling_oid])
    assert got == want == []


def test_kg_source_chunks_equals_oracle_unknown_object_id(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=2)
    got = repo._kg_source_chunks(nb.id, ["does-not-exist"])
    want = _oracle_kg_source_chunks(repo, nb.id, ["does-not-exist"])
    assert got == want == []


# ── caching behavior ─────────────────────────────────────────────────────────

def test_ent_chunk_map_second_call_no_sql(repo, monkeypatch):
    """Second call must not re-scan knowledge_objects/chunks. The only SQL left
    is _scale_index_version's O(1) fast-path probe (1 connect/call, memoized on
    kg_mutation_seq — not the O(N) evidence/chunk scan this cache eliminates)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=3)
    first = repo._ent_chunk_map(nb.id)
    calls = _connect_spy(repo, monkeypatch)
    second = repo._ent_chunk_map(nb.id)
    assert calls["n"] == 1  # version-probe only, no full-scan connect
    assert second == first


def test_kg_source_chunks_second_call_no_full_scan(repo, monkeypatch):
    """Second call for the SAME object_ids must not re-scan chunks: the
    element->chunk reverse map is served from cache, and only the (small,
    IN(...)-bounded) evidence + chunk-by-id lookups hit SQL — plus the
    O(1) _scale_index_version probe each cache lookup pays."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=3)
    repo._kg_source_chunks(nb.id, oids)  # warm _elem_chunk_map cache
    calls = _connect_spy(repo, monkeypatch)
    repo._kg_source_chunks(nb.id, oids)
    # 1 connect for the evidence+chunk-by-id lookups (both queries share one
    # `with self._connect() as db:` block) + 1 for the elem_chunk_map cache's
    # version probe (its own load is elided — no chunks full-scan connect).
    assert calls["n"] == 2


def test_elem_chunk_map_cached_across_both_consumers(repo, monkeypatch):
    """_elem_chunk_map is shared: warming it via _ent_chunk_map means
    _kg_source_chunks's element lookup doesn't re-scan chunks either."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=3)
    repo._ent_chunk_map(nb.id)  # warms {nb}:elemchunk
    calls = _connect_spy(repo, monkeypatch)
    repo._kg_source_chunks(nb.id, oids)
    # evidence+chunk-by-id lookup connect + elem_chunk_map version-probe connect;
    # no chunks full-scan (that's the whole point of sharing the cache).
    assert calls["n"] == 2


def test_ent_chunk_map_invalidates_on_kg_mutation(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=2)
    before = repo._ent_chunk_map(nb.id)
    assert len(before) == 2

    _seed(repo, nb.id, n_objects=1)  # adds one more concept+chunk, bumps kg_mutation_seq
    after = repo._ent_chunk_map(nb.id)
    assert after != before
    assert len(after) == 3
    assert after == _oracle_ent_chunk_map(repo, nb.id)


def test_kg_source_chunks_invalidates_on_kg_mutation(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=1)
    target = oids[0]
    before = repo._kg_source_chunks(nb.id, [target])
    assert len(before) == 1
    target_elem = before[0].element_ids[0]
    target_source_id = before[0].source_id

    # add another chunk sharing the SAME element_id as the target object
    now = _now()
    with repo._write() as db:
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (f"extra-{nb.id}", nb.id, target_source_id, "extra text", "sec",
                    json.dumps([target_elem]), now))
    repo._mark_unified_kg_dirty(nb.id)

    after = repo._kg_source_chunks(nb.id, [target])
    assert len(after) == 2
    assert after == _oracle_kg_source_chunks(repo, nb.id, [target])


def test_elem_chunk_map_matches_manual_reverse_index(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=2, chunks_per_elem=2)
    got = repo._elem_chunk_map(nb.id)
    with repo._connect() as db:
        rows = db.execute("SELECT id, element_ids FROM chunks WHERE notebook_id=?", (nb.id,)).fetchall()
    want = {}
    for r in rows:
        for el in json.loads(r["element_ids"] or "[]"):
            want.setdefault(el, []).append(r["id"])
    assert {k: sorted(v) for k, v in got.items()} == {k: sorted(v) for k, v in want.items()}


# ── downstream consumer sanity (PPR / mix / graph paths still work) ────────

def test_ppr_reset_vector_uses_ent_chunk_map_for_specificity_weight(repo, monkeypatch):
    """_ppr_reset_vector divides by len(ent_chunk_map.get(object_id)) — make
    sure the cached map still supports .get()/len() the same as the old dict
    of sets (consumer-shape audit)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=2, chunks_per_elem=2)
    m = repo._ent_chunk_map(nb.id)
    assert isinstance(m, dict)
    for oid in oids[:2]:
        v = m.get(oid)
        assert v is None or hasattr(v, "__len__")


def test_kg_source_chunks_return_shape_is_list_for_ordered_consumers(repo):
    """_mix_retrieve/ask_graph index kg_chunks by position (src[i]) and pass
    the result through truncate_by_tokens, both of which require a concrete
    list, not a set — guard the return type."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=3)
    out = repo._kg_source_chunks(nb.id, oids)
    assert isinstance(out, list)
    assert out[:] == out  # indexable/sliceable
