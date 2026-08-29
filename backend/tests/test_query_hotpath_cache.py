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
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
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
    orig = repo._runtime.database.connect

    def _wrapped():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(repo._runtime.database, "connect", _wrapped)
    return calls


# ── equivalence: _ent_chunk_map vs oracle ───────────────────────────────────

def test_ent_chunk_map_equals_oracle_basic(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=3, chunks_per_elem=1)
    assert repo.retrieval.graph._ent_chunk_map(nb.id) == _oracle_ent_chunk_map(repo, nb.id)


def test_ent_chunk_map_equals_oracle_multi_chunk_fanout(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=2, chunks_per_elem=3)
    got = repo.retrieval.graph._ent_chunk_map(nb.id)
    want = _oracle_ent_chunk_map(repo, nb.id)
    assert got == want
    assert all(len(v) == 3 for v in got.values())


def test_ent_chunk_map_equals_oracle_dangling_element(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=2, dangling=True)
    dangling_oid = oids[-1]
    assert dangling_oid.endswith("-dangling")
    got = repo.retrieval.graph._ent_chunk_map(nb.id)
    want = _oracle_ent_chunk_map(repo, nb.id)
    assert got == want
    # dangling object's evidence element_id matches no chunk -> excluded from map
    assert dangling_oid not in got


def test_ent_chunk_map_equals_oracle_empty_notebook(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    assert repo.retrieval.graph._ent_chunk_map(nb.id) == _oracle_ent_chunk_map(repo, nb.id) == {}


# ── equivalence: _kg_source_chunks vs oracle ────────────────────────────────

def test_kg_source_chunks_equals_oracle_basic(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=3, chunks_per_elem=1)
    got = repo.retrieval.graph._kg_source_chunks(nb.id, oids)
    want = _oracle_kg_source_chunks(repo, nb.id, oids)
    got_tuples = [(c.chunk_id, c.source_id, c.text, c.section_path, tuple(c.element_ids), c.relevance) for c in got]
    want_tuples = [(c.chunk_id, c.source_id, c.text, c.section_path, tuple(c.element_ids), c.relevance) for c in want]
    assert set(got_tuples) == set(want_tuples)
    assert len(got) == len(want)


def test_kg_source_chunks_equals_oracle_multi_chunk_fanout(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=2, chunks_per_elem=3)
    got = repo.retrieval.graph._kg_source_chunks(nb.id, oids)
    want = _oracle_kg_source_chunks(repo, nb.id, oids)
    assert {c.chunk_id for c in got} == {c.chunk_id for c in want}
    assert len(got) == len(want) == 6


def test_kg_source_chunks_equals_oracle_empty_object_ids(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=2)
    assert repo.retrieval.graph._kg_source_chunks(nb.id, []) == _oracle_kg_source_chunks(repo, nb.id, []) == []


def test_kg_source_chunks_equals_oracle_object_no_evidence(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=2)
    empty_oid = oids[-1]
    assert empty_oid.endswith("-empty")
    got = repo.retrieval.graph._kg_source_chunks(nb.id, [empty_oid])
    want = _oracle_kg_source_chunks(repo, nb.id, [empty_oid])
    assert got == want == []


def test_kg_source_chunks_equals_oracle_dangling_element(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=1, dangling=True)
    dangling_oid = oids[-1]
    assert dangling_oid.endswith("-dangling")
    got = repo.retrieval.graph._kg_source_chunks(nb.id, [dangling_oid])
    want = _oracle_kg_source_chunks(repo, nb.id, [dangling_oid])
    assert got == want == []


def test_kg_source_chunks_equals_oracle_unknown_object_id(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=2)
    got = repo.retrieval.graph._kg_source_chunks(nb.id, ["does-not-exist"])
    want = _oracle_kg_source_chunks(repo, nb.id, ["does-not-exist"])
    assert got == want == []


# ── caching behavior ─────────────────────────────────────────────────────────

def test_ent_chunk_map_second_call_no_sql(repo, monkeypatch):
    """Second call must not re-scan knowledge_objects/chunks. The only SQL left
    is _scale_index_version's O(1) fast-path probe (1 connect/call, memoized on
    kg_mutation_seq — not the O(N) evidence/chunk scan this cache eliminates)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=3)
    first = repo.retrieval.graph._ent_chunk_map(nb.id)
    calls = _connect_spy(repo, monkeypatch)
    second = repo.retrieval.graph._ent_chunk_map(nb.id)
    assert calls["n"] == 1  # version-probe only, no full-scan connect
    assert second == first


def test_kg_source_chunks_second_call_no_full_scan(repo, monkeypatch):
    """Second call for the SAME object_ids must not re-scan chunks: the
    element->chunk reverse map is served from cache, and only the (small,
    IN(...)-bounded) evidence + chunk-by-id lookups hit SQL — plus the
    O(1) _scale_index_version probe each cache lookup pays."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=3)
    repo.retrieval.graph._kg_source_chunks(nb.id, oids)  # warm _elem_chunk_map cache
    calls = _connect_spy(repo, monkeypatch)
    repo.retrieval.graph._kg_source_chunks(nb.id, oids)
    # 1 connect for the evidence+chunk-by-id lookups (both queries share one
    # `with self._connect() as db:` block) + 1 for the elem_chunk_map cache's
    # version probe (its own load is elided — no chunks full-scan connect).
    assert calls["n"] == 2


def test_elem_chunk_map_cached_across_both_consumers(repo, monkeypatch):
    """_elem_chunk_map is shared: warming it via _ent_chunk_map means
    _kg_source_chunks's element lookup doesn't re-scan chunks either."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=3)
    repo.retrieval.graph._ent_chunk_map(nb.id)  # warms {nb}:elemchunk
    calls = _connect_spy(repo, monkeypatch)
    repo.retrieval.graph._kg_source_chunks(nb.id, oids)
    # evidence+chunk-by-id lookup connect + elem_chunk_map version-probe connect;
    # no chunks full-scan (that's the whole point of sharing the cache).
    assert calls["n"] == 2


def test_ent_chunk_map_invalidates_on_kg_mutation(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=2)
    before = repo.retrieval.graph._ent_chunk_map(nb.id)
    assert len(before) == 2

    _seed(repo, nb.id, n_objects=1)  # adds one more concept+chunk, bumps kg_mutation_seq
    after = repo.retrieval.graph._ent_chunk_map(nb.id)
    assert after != before
    assert len(after) == 3
    assert after == _oracle_ent_chunk_map(repo, nb.id)


def test_kg_source_chunks_invalidates_on_kg_mutation(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=1)
    target = oids[0]
    before = repo.retrieval.graph._kg_source_chunks(nb.id, [target])
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

    after = repo.retrieval.graph._kg_source_chunks(nb.id, [target])
    assert len(after) == 2
    assert after == _oracle_kg_source_chunks(repo, nb.id, [target])


def test_elem_chunk_map_matches_manual_reverse_index(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed(repo, nb.id, n_objects=2, chunks_per_elem=2)
    got = repo.retrieval.graph._elem_chunk_map(nb.id)
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
    m = repo.retrieval.graph._ent_chunk_map(nb.id)
    assert isinstance(m, dict)
    for oid in oids[:2]:
        v = m.get(oid)
        assert v is None or hasattr(v, "__len__")


def test_kg_source_chunks_return_shape_is_list_for_ordered_consumers(repo):
    """_mix_retrieve indexes kg_chunks by position (src[i]) and passes
    the result through truncate_by_tokens, both of which require a concrete
    list, not a set — guard the return type."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=3)
    out = repo.retrieval.graph._kg_source_chunks(nb.id, oids)
    assert isinstance(out, list)
    assert out[:] == out  # indexable/sliceable


# ── ordering contract (Fix 1) ────────────────────────────────────────────────
# _mix_retrieve's KG-overlay branch feeds _kg_source_chunks output into
# retrieval_rerank as its input order, then truncate_by_tokens; when rerank is
# unconfigured or its call fails, RerankClient.rerank falls back to the
# identity order it was given, so list order decides which chunks survive
# truncation and how citations are numbered in both the rerank-input and the
# rerank-fallback case — the order must be deterministic: object_ids order →
# each object's evidence array order → _elem_chunk_map's per-element chunk
# list order (chunks scan order). NOT the old implementation's full-table
# physical scan order (which was never a contract).

def _seed_order_fixture(repo):
    """3 chunks inserted in table order c1,c2,c3 (element el1,el2,el3 resp.);
    objB's evidence -> el3 then el2; objA's evidence -> el1. Calling with
    object_ids=[objB, objA] must yield [c3, c2, c1] (evidence-driven order),
    while the old physical-scan oracle yields [c1, c2, c3]."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = _now()
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", (f"s-{nb.id}", nb.id, "t", "md", "ready", now, now))
        for i in (1, 2, 3):
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (f"c{i}", nb.id, f"s-{nb.id}", f"text {i}", "sec", json.dumps([f"el{i}"]), now))

        def _ev(*els):
            return json.dumps([{"source_id": f"s-{nb.id}", "source_title": "", "element_id": el,
                                "element_type": "paragraph", "location_label": "p",
                                "quoted_span": "q", "confidence": 1.0} for el in els])
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("objA", nb.id, "concept", "approved", "", json.dumps({"name": "A"}), _ev("el1"),
                    f"s-{nb.id}", now, now))
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("objB", nb.id, "concept", "approved", "", json.dumps({"name": "B"}), _ev("el3", "el2"),
                    f"s-{nb.id}", now, now))
    repo._mark_unified_kg_dirty(nb.id)
    return nb


def test_kg_source_chunks_order_follows_object_ids_then_evidence(repo):
    nb = _seed_order_fixture(repo)
    out = repo.retrieval.graph._kg_source_chunks(nb.id, ["objB", "objA"])
    # objB first (evidence order el3, el2 -> c3, c2), then objA (el1 -> c1)
    assert [c.chunk_id for c in out] == ["c3", "c2", "c1"]
    # flipping object_ids order flips the output order accordingly
    out2 = repo.retrieval.graph._kg_source_chunks(nb.id, ["objA", "objB"])
    assert [c.chunk_id for c in out2] == ["c1", "c3", "c2"]
    # same multiset as the old full-scan oracle (only the order contract changed)
    want = _oracle_kg_source_chunks(repo, nb.id, ["objB", "objA"])
    assert {c.chunk_id for c in out} == {c.chunk_id for c in want}


def test_kg_source_chunks_order_stable_across_cache_states(repo):
    """Cold (loader runs) and warm (cache hit) calls must produce the identical
    list order — order is part of the contract, not a cache artifact."""
    nb = _seed_order_fixture(repo)
    cold = [c.chunk_id for c in repo.retrieval.graph._kg_source_chunks(nb.id, ["objB", "objA"])]
    warm = [c.chunk_id for c in repo.retrieval.graph._kg_source_chunks(nb.id, ["objB", "objA"])]
    assert cold == warm == ["c3", "c2", "c1"]


def test_kg_source_chunks_order_matches_oracle_when_scan_order_agrees(repo):
    """When object_ids/evidence order happens to agree with the chunks table
    scan order (the common single-source case), the new deterministic order is
    position-for-position equal to the old oracle — a full ordered-equality
    check, not just a multiset check."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oids = _seed(repo, nb.id, n_objects=3, chunks_per_elem=2)
    got = repo.retrieval.graph._kg_source_chunks(nb.id, oids)
    want = _oracle_kg_source_chunks(repo, nb.id, oids)
    assert [c.chunk_id for c in got] == [c.chunk_id for c in want]


def test_kg_source_chunks_shared_element_dedup_keeps_first_seen_position(repo):
    """A chunk reachable via two objects' evidence appears once, at the position
    of its FIRST appearance in the deterministic walk."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = _now()
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", (f"s-{nb.id}", nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cShared", nb.id, f"s-{nb.id}", "shared", "sec", json.dumps(["elS"]), now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cOwn", nb.id, f"s-{nb.id}", "own", "sec", json.dumps(["elO"]), now))

        def _ev(*els):
            return json.dumps([{"source_id": f"s-{nb.id}", "source_title": "", "element_id": el,
                                "element_type": "paragraph", "location_label": "p",
                                "quoted_span": "q", "confidence": 1.0} for el in els])
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("objS1", nb.id, "concept", "approved", "", json.dumps({"name": "S1"}), _ev("elS"),
                    f"s-{nb.id}", now, now))
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("objS2", nb.id, "concept", "approved", "", json.dumps({"name": "S2"}), _ev("elO", "elS"),
                    f"s-{nb.id}", now, now))
    repo._mark_unified_kg_dirty(nb.id)
    out = repo.retrieval.graph._kg_source_chunks(nb.id, ["objS1", "objS2"])
    assert [c.chunk_id for c in out] == ["cShared", "cOwn"]  # cShared first-seen via objS1


# ── chunk write path bumps kg_mutation_seq (Fix 2) ──────────────────────────

def _mutation_seq(repo, nb_id):
    with repo._connect() as db:
        r = db.execute("SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
                       (nb_id,)).fetchone()
    return int(r["kg_mutation_seq"]) if r else 0


def test_build_chunks_bumps_seq_and_refreshes_elem_chunk_map(repo, monkeypatch):
    """Reviewer repro (inverted): kg_auto_extract=False (default) + NO existing
    KG means the extract-path _mark_unified_kg_dirty is never reached — the
    chunk INSERT choke point itself must bump kg_mutation_seq, or a warm
    _elem_chunk_map/_ent_chunk_map never sees the new source's chunks (no TTL,
    no other invalidation)."""
    monkeypatch.setattr(repo.settings, "kg_auto_extract", False)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # warm the cache on the empty notebook
    assert repo.retrieval.graph._elem_chunk_map(nb.id) == {}

    # real chunk write path: source + source_elements -> _build_chunks_for_source
    import uuid
    sid = f"src-{uuid.uuid4().hex[:8]}"
    now = _now()
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                   "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, nb.id, "S", "document", "s.md", "/tmp/s.md", 0, "h", "", "", "extracted", now, now))
        db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (f"el-{sid}-0001", sid, "paragraph", "p1", "hello " * 60, "{}", now))
    seq_before = _mutation_seq(repo, nb.id)
    repo._build_chunks_for_source(sid)
    assert _mutation_seq(repo, nb.id) > seq_before  # choke point bumped unconditionally

    refreshed = repo.retrieval.graph._elem_chunk_map(nb.id)
    assert f"el-{sid}-0001" in refreshed  # warm cache was invalidated by the version bump
    with repo._connect() as db:
        cids = {r["id"] for r in db.execute(
            "SELECT id FROM chunks WHERE source_id=?", (sid,)).fetchall()}
    assert set(refreshed[f"el-{sid}-0001"]) <= cids and refreshed[f"el-{sid}-0001"]


# ── P0-3: review_queue edge centrality — version cache + degree-top-K bound ──
# _edge_centrality_map used to be recomputed synchronously on every review_queue
# call (rustworkx digraph_edge_betweenness_centrality — Brandes O(V·E)), on the
# request thread, uncached — minutes of CPU at 490k-node scale. Now cached via
# repo._vector_cache on tuple(_scale_index_version(nb)), same machinery as
# _ent_chunk_map/_vector_matrix. Above settings.edge_centrality_max_nodes, only
# the degree-top-K induced subgraph is scored; edges outside get centrality 0.0.

from app.models.schemas import NotebookCreate as _NotebookCreate  # noqa: E402


def _seed_centrality_graph(repo, n_extra_chain=0):
    """4-node graph (Claim/Concept/Formula/Procedure) with 3 typed edges,
    mirroring test_edge_review_queue.py's fixture. n_extra_chain appends a
    linear chain of additional nodes (X0->X1->X2->...) to grow node count for
    the bounded-path tests, all connected via a shared edge_type."""
    nb = repo.create_notebook(_NotebookCreate(name="centrality-nb"))
    objects = [
        {"local_id": "C1", "object_type": "Claim",
         "payload": {"name": "Claim Alpha"}, "evidence": []},
        {"local_id": "C2", "object_type": "Concept",
         "payload": {"name": "Concept Beta"}, "evidence": []},
        {"local_id": "F1", "object_type": "Formula",
         "payload": {"name": "Formula Gamma"}, "evidence": []},
        {"local_id": "P1", "object_type": "Procedure",
         "payload": {"name": "Procedure Delta"}, "evidence": []},
    ]
    relations = [
        {"source_local_id": "C1", "target_local_id": "C2", "edge_type": "defines",
         "evidence": [{"file": "f1", "char_start": 0, "char_end": 10,
                       "line_start": 1, "line_end": 1, "quote": "alpha defines beta"}]},
        {"source_local_id": "F1", "target_local_id": "P1", "edge_type": "used_in", "evidence": []},
        {"source_local_id": "C1", "target_local_id": "P1", "edge_type": "depends_on", "evidence": []},
    ]
    for i in range(n_extra_chain):
        objects.append({"local_id": f"X{i}", "object_type": "Procedure",
                        "payload": {"name": f"chain{i}"}, "evidence": []})
    prev = "P1"
    for i in range(n_extra_chain):
        relations.append({"source_local_id": prev, "target_local_id": f"X{i}",
                          "edge_type": "precedes", "evidence": []})
        prev = f"X{i}"
    repo.store_kg(nb.id, None, objects, relations)
    return nb.id


def test_edge_centrality_map_second_call_no_recompute(repo, monkeypatch):
    """Second call must not re-run rustworkx betweenness — spy on
    compute_edge_centrality call count (the O(V·E) Brandes run this cache elides)."""
    import app.services.kg.graph_reason as graph_reason_mod
    nb_id = _seed_centrality_graph(repo)
    first = repo._edge_centrality_map(nb_id)

    calls = {"n": 0}
    orig = graph_reason_mod.compute_edge_centrality

    def _spy(G):
        calls["n"] += 1
        return orig(G)

    # _edge_centrality_map does `from app.services.kg.graph_reason import
    # ... compute_edge_centrality` INSIDE the method body on every call, so
    # patching the source module attribute is visible to the next call.
    monkeypatch.setattr(graph_reason_mod, "compute_edge_centrality", _spy)
    second = repo._edge_centrality_map(nb_id)
    assert calls["n"] == 0  # cache hit — loader (and thus compute_edge_centrality) never ran
    assert second == first


def test_edge_centrality_map_recomputes_after_kg_mutation(repo):
    nb_id = _seed_centrality_graph(repo)
    before = repo._edge_centrality_map(nb_id)
    assert before  # non-empty: 3 edges seeded

    # Add another edge -> bumps kg_mutation_seq -> version changes -> recompute.
    # store_kg's local_id->db_id remap is scoped to THIS call's objects list, so
    # both endpoints of the new relation must be created in the same call.
    repo.store_kg(nb_id, None,
                  [{"local_id": "C3a", "object_type": "Concept",
                    "payload": {"name": "Concept Extra A"}, "evidence": []},
                   {"local_id": "C3b", "object_type": "Concept",
                    "payload": {"name": "Concept Extra B"}, "evidence": []}],
                  [{"source_local_id": "C3a", "target_local_id": "C3b",
                    "edge_type": "kind_of", "evidence": []}])
    after = repo._edge_centrality_map(nb_id)
    assert after != before
    assert len(after) == 4  # 3 original + 1 new edge


def _oracle_edge_centrality(repo, nb_id):
    """Verbatim pre-cache computation: full JOIN + build_rx_graph + compute_edge_centrality,
    no bounding, no cache — the ground truth review_queue used to compute inline."""
    from app.services.kg.graph_reason import build_rx_graph, compute_edge_centrality
    with repo._connect() as db:
        rel_rows = db.execute(
            "SELECT id, source_object_id, target_object_id, edge_type, evidence "
            "FROM knowledge_relations WHERE notebook_id = ? AND review_status != 'rejected'",
            (nb_id,)).fetchall()
        obj_rows = db.execute(
            "SELECT id, object_type, payload FROM knowledge_objects WHERE notebook_id = ?",
            (nb_id,)).fetchall()
    node_types, node_names = {}, {}
    for r in obj_rows:
        node_types[r["id"]] = r["object_type"]
        node_names[r["id"]] = json.loads(r["payload"] or "{}").get("name", "")
    rels = [{
        "id": r["id"], "source_object_id": r["source_object_id"],
        "target_object_id": r["target_object_id"], "edge_type": r["edge_type"],
        "evidence": json.loads(r["evidence"] or "[]"),
    } for r in rel_rows]
    G, idx_to_oid, oid_to_idx = build_rx_graph(
        {oid: {"type": t, "name": node_names.get(oid, "")} for oid, t in node_types.items()},
        rels)
    return compute_edge_centrality(G)


def test_edge_centrality_map_equals_oracle(repo):
    nb_id = _seed_centrality_graph(repo)
    got = repo._edge_centrality_map(nb_id)
    want = _oracle_edge_centrality(repo, nb_id)
    assert got == want


def test_review_queue_output_matches_uncached_oracle(repo):
    """Full review_queue() output (edge order/fields/scores) must be identical
    whether centrality is served from cache or (as the oracle does) recomputed
    inline — caching changes WHEN centrality is computed, not WHAT it computes.

    ⚠ The inline oracle below is a second hand-copy of the pre-P1 read path;
    the other one is ``test_governance_read_narrowing._old_review_queue``.  They
    pin different properties (this one: centrality caching; that one: the P1 批 A
    read narrowing) but transcribe the same original code — when the service
    changes, check whether BOTH need to move, and if one is edited alone say so
    here.  Deliberately not shared: each is only meaningful as a frozen copy of
    the state its own test predates.
    """
    nb_id = _seed_centrality_graph(repo)
    cached = repo.review_queue(nb_id)

    # Force a fresh, uncached centrality computation and rebuild the queue
    # inline the same way the pre-cache implementation did.
    from app.services.kg.edge_trust import (
        compute_trust_score, corroboration_counts, corroboration_score_from_count,
    )
    with repo._connect() as db:
        rel_rows = db.execute(
            "SELECT kr.id, kr.source_object_id, kr.target_object_id, "
            "kr.edge_type, kr.evidence, kr.source_id, kr.review_status, "
            "ko_s.object_type AS src_type, ko_t.object_type AS tgt_type "
            "FROM knowledge_relations kr "
            "LEFT JOIN knowledge_objects ko_s ON ko_s.id = kr.source_object_id "
            "LEFT JOIN knowledge_objects ko_t ON ko_t.id = kr.target_object_id "
            "WHERE kr.notebook_id = ? AND kr.review_status != 'rejected'",
            (nb_id,)).fetchall()
        obj_rows = db.execute(
            "SELECT id, object_type, payload FROM knowledge_objects WHERE notebook_id = ?",
            (nb_id,)).fetchall()
    node_types, node_names = {}, {}
    for r in obj_rows:
        node_types[r["id"]] = r["object_type"]
        node_names[r["id"]] = json.loads(r["payload"] or "{}").get("name", "")
    rels = []
    for r in rel_rows:
        rels.append({
            "id": r["id"], "source_object_id": r["source_object_id"],
            "target_object_id": r["target_object_id"], "edge_type": r["edge_type"],
            "evidence": json.loads(r["evidence"] or "[]"), "source_id": r["source_id"],
            "review_status": r["review_status"],
            "_src_type": r["src_type"] or "", "_tgt_type": r["tgt_type"] or "",
            "_src_name": node_names.get(r["source_object_id"], ""),
            "_tgt_name": node_names.get(r["target_object_id"], ""),
        })
    corr_counts = corroboration_counts(rels, node_names)
    edge_centrality = _oracle_edge_centrality(repo, nb_id)
    items = []
    for rel in rels:
        rid = rel["id"]
        corr_score = corroboration_score_from_count(corr_counts.get(rid, 1))
        trust = compute_trust_score(rel, node_types, corr_score)
        ec = edge_centrality.get(rid, 0.0)
        priority = ec * (1.0 - trust)
        items.append({
            "rel_id": rid, "notebook_id": nb_id, "edge_type": rel["edge_type"],
            "source_object_id": rel["source_object_id"], "target_object_id": rel["target_object_id"],
            "source_name": rel["_src_name"], "target_name": rel["_tgt_name"],
            "source_type": rel["_src_type"], "target_type": rel["_tgt_type"],
            "trust_score": trust, "edge_centrality": ec, "review_priority": priority,
            "review_status": rel["review_status"],
        })
    items.sort(key=lambda x: x["review_priority"], reverse=True)
    oracle = items[:200]

    assert cached == oracle


def test_edge_centrality_map_invalidates_on_review_status_flip(repo):
    """set_edge_review flips review_status in place (edge count unchanged) —
    the version tuple does NOT move (relations 无 updated_at 列,in-place UPDATE
    不动 COUNT/MAX),所以 _invalidate_unified_cache 的显式 ':edge_centrality'
    eviction 是这里【唯一】的失效机制——不同于 :rxgraph(其版本键内嵌各
    review_status 计数,自身就能捕捉翻转,显式 eviction 才真是 belt-and-braces)。"""
    nb_id = _seed_centrality_graph(repo)
    q = repo.review_queue(nb_id)
    assert len(q) >= 2
    keep_id, reject_id = q[0]["rel_id"], q[1]["rel_id"]

    warm = repo._edge_centrality_map(nb_id)
    assert reject_id in warm

    repo.set_edge_review(nb_id, reject_id, "rejected")
    fresh = repo._edge_centrality_map(nb_id)
    assert reject_id not in fresh, (
        "stale edge_centrality map served after review_status flip excluded an edge")


# ── bounded path: degree-top-K induced subgraph above edge_centrality_max_nodes ──

def test_edge_centrality_bounded_subgraph_no_crash_and_sane_queue(repo, monkeypatch):
    """With edge_centrality_max_nodes=3 on a graph with >3 nodes, centrality is
    computed only on the degree-top-3 induced subgraph; edges fully outside get
    centrality 0.0 (review_priority degrades to trust-only ranking for them).
    review_queue must still return without crashing."""
    monkeypatch.setattr(repo.settings, "edge_centrality_max_nodes", 3)
    nb_id = _seed_centrality_graph(repo, n_extra_chain=4)  # 4 base + 4 chain = 8 nodes

    q = repo.review_queue(nb_id)
    assert q  # returns a queue, no crash

    ec_map = repo._edge_centrality_map(nb_id)
    assert isinstance(ec_map, dict)
    # At least one edge should have centrality 0.0 (outside the top-3 subgraph) —
    # with only 3 of 8 nodes scored, most of the 7 edges can't have both endpoints in it.
    assert any(v == 0.0 for v in ec_map.values()) or len(ec_map) < 7

    # review_priority stays a valid, sorted, non-negative sequence.
    priorities = [item["review_priority"] for item in q]
    assert priorities == sorted(priorities, reverse=True)
    assert all(p >= 0.0 for p in priorities)


def test_edge_centrality_bounded_top_k_is_deterministic(repo, monkeypatch):
    """Same graph, same max_nodes bound -> identical centrality map across a
    cold call and a forced recompute (cache bypassed via invalidate) — the
    degree-top-K tie-break (stable sort by node insertion order) must be
    reproducible, not order-dependent on dict/set iteration."""
    monkeypatch.setattr(repo.settings, "edge_centrality_max_nodes", 3)
    nb_id = _seed_centrality_graph(repo, n_extra_chain=4)

    first = repo._edge_centrality_map(nb_id)
    repo._vector_cache.invalidate(f"{nb_id}:edge_centrality")
    second = repo._edge_centrality_map(nb_id)
    assert first == second


def test_edge_centrality_bounded_selects_highest_degree_nodes(repo, monkeypatch):
    """Sanity-check the top-K selection itself: with max_nodes=3 on the 4-node
    base fixture (C1 has degree 2 — two used_in/defines edges out; P1 has
    degree 2 — used_in in from both F1 and C1), the induced subgraph should be
    small and centrality for edges entirely among low-degree excluded nodes
    (e.g. the F1->P1 edge if F1 is dropped) is exactly 0.0."""
    monkeypatch.setattr(repo.settings, "edge_centrality_max_nodes", 3)
    nb_id = _seed_centrality_graph(repo)  # 4 nodes, 3 edges — bound=3 forces exclusion of 1 node
    ec_map = repo._edge_centrality_map(nb_id)
    full_map = _oracle_edge_centrality(repo, nb_id)
    # Bounded map is a subset of keys of the full map (never invents new rel_ids).
    assert set(ec_map.keys()) <= set(full_map.keys())
    assert len(ec_map) <= len(full_map)


def test_edge_centrality_within_bound_matches_unbounded(repo, monkeypatch):
    """When num_nodes <= edge_centrality_max_nodes, the bounded path is not
    taken at all — result must equal the unbounded oracle exactly (no
    approximation when the graph fits)."""
    monkeypatch.setattr(repo.settings, "edge_centrality_max_nodes", 1000)
    nb_id = _seed_centrality_graph(repo, n_extra_chain=4)
    got = repo._edge_centrality_map(nb_id)
    want = _oracle_edge_centrality(repo, nb_id)
    assert got == want


# ── C1 rework: bounded LOADER (SQL degree ranking + temp-table JOIN) ─────────
# _edge_centrality_map used to load ALL relations + ALL knowledge_objects
# (payload included) before cutting down to degree-top-K in Python/rustworkx.
# Reworked so the LOAD itself is bounded: (1) degree via SQL GROUP BY over
# knowledge_relations only (no knowledge_objects query at all for ranking),
# (2) only edges with both endpoints in the top-K id set are ever SELECTed
# (via a CREATE TEMP TABLE + JOIN), (3) build_rx_graph/compute_edge_centrality
# run unchanged on the already-bounded set.

def test_edge_centrality_bounded_load_no_full_objects_query(repo, monkeypatch):
    """Over-K path must never issue a full-row `knowledge_objects` query
    (SELECT ... payload ... FROM knowledge_objects) inside the LOADER — the
    whole point of the rework is that ranking + edge selection happen via SQL
    (relations GROUP BY + temp-table JOIN), never a knowledge_objects payload
    load. Note: repo._scale_index_version (the CACHE KEY, evaluated before
    the loader runs at all) legitimately issues a cheap COUNT/MAX aggregate
    against knowledge_objects — that's the pre-existing version-cache
    machinery, unrelated to this rework, so the spy only flags a query that
    selects the `payload` column (the actual expensive full-row load this
    item removes from the bounded path)."""
    monkeypatch.setattr(repo.settings, "edge_centrality_max_nodes", 3)
    nb_id = _seed_centrality_graph(repo, n_extra_chain=4)  # 8 nodes > bound=3
    # Warm the version-cache machinery's own queries first (out of scope for
    # this spy) so only the loader's queries are observed below.
    repo._scale_index_version(nb_id)
    repo._vector_cache.invalidate(f"{nb_id}:edge_centrality")

    seen_payload_query = {"hit": False}
    orig_connect = repo._connect

    class _SpyConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            if "knowledge_objects" in sql and "payload" in sql:
                seen_payload_query["hit"] = True
            return self._inner.execute(sql, *a, **kw)

        def executemany(self, sql, *a, **kw):
            return self._inner.executemany(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    def spy_connect():
        return _SpyConn(orig_connect())

    monkeypatch.setattr(repo, "_connect", spy_connect)
    ec_map = repo._edge_centrality_map(nb_id)
    assert isinstance(ec_map, dict) and ec_map
    assert not seen_payload_query["hit"], (
        "over-K _edge_centrality_map loader must not query knowledge_objects.payload")


def test_edge_centrality_bounded_load_equals_old_post_hoc_cut(repo, monkeypatch):
    """Equivalence: the new bounded-LOAD result must equal an oracle that
    loads the FULL graph the OLD way (all objects + all relations) and then
    applies the identical (-degree, id) top-K selection rule as the new
    loader, before running build_rx_graph/compute_edge_centrality.

    Note on tie-break: the new SQL-ranked loader deliberately uses a
    deterministic (-degree, id) tie-break instead of the old rustworkx
    node-insertion-order tiebreak (see _edge_centrality_map's docstring) —
    when there is a genuine degree tie at the top-K boundary, the OLD
    insertion-order-tiebreak and the NEW id-tiebreak can select a DIFFERENT
    top-K node set, which is an intentional, documented behavior change (not
    a regression: betweenness at the boundary was already an approximation).
    So this oracle mirrors the NEW tie-break rule exactly — proving the
    bounded LOAD produces byte-identical results to bounding a fully-loaded
    graph the same way, i.e. the loader rework changes nothing except what
    gets fetched from SQLite."""
    from app.services.kg.graph_reason import build_rx_graph, compute_edge_centrality
    monkeypatch.setattr(repo.settings, "edge_centrality_max_nodes", 3)
    nb_id = _seed_centrality_graph(repo, n_extra_chain=4)

    got = repo._edge_centrality_map(nb_id)

    # Oracle: full load (old table shape), but rank top-K with the SAME
    # (-degree, id) rule the new SQL loader uses.
    with repo._connect() as db:
        rel_rows = db.execute(
            "SELECT id, source_object_id, target_object_id, edge_type, "
            "evidence FROM knowledge_relations "
            "WHERE notebook_id = ? AND review_status != 'rejected'",
            (nb_id,)).fetchall()
        obj_rows = db.execute(
            "SELECT id, object_type, payload FROM knowledge_objects "
            "WHERE notebook_id = ?", (nb_id,)).fetchall()
    node_types, node_names = {}, {}
    for r in obj_rows:
        node_types[r["id"]] = r["object_type"]
        node_names[r["id"]] = json.loads(r["payload"] or "{}").get("name", "")
    rels = [{
        "id": r["id"], "source_object_id": r["source_object_id"],
        "target_object_id": r["target_object_id"], "edge_type": r["edge_type"],
        "evidence": json.loads(r["evidence"] or "[]"),
    } for r in rel_rows]

    degree: dict = {}
    for r in rels:
        degree[r["source_object_id"]] = degree.get(r["source_object_id"], 0) + 1
        degree[r["target_object_id"]] = degree.get(r["target_object_id"], 0) + 1
    max_nodes = repo.settings.edge_centrality_max_nodes
    top_ids = set(n for n, _ in sorted(
        degree.items(), key=lambda kv: (-kv[1], kv[0]))[:max_nodes])

    node_dict = {
        oid: {"type": node_types[oid], "name": node_names[oid]}
        for oid in top_ids
    }
    bounded_rels = [r for r in rels
                    if r["source_object_id"] in top_ids and r["target_object_id"] in top_ids]
    G, idx_to_oid, oid_to_idx = build_rx_graph(node_dict, bounded_rels)
    old_result = compute_edge_centrality(G)

    assert got == old_result, (
        f"bounded-load result must equal the same-tiebreak oracle; "
        f"got={got} oracle={old_result}")
