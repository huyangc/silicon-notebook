"""P1-2 (perf audit): cluster_map 版本缓存 + 失效审计 + incremental_fuse_source 内共享。

cluster_map used to be an UNCACHED full scan of concept_clusters (member_object_id,
canonical_id) on every call — at production scale (millions of member rows) this is
a multi-second table scan + dict build. incremental_fuse_source calls it TWICE per
upload (once for the Tier1/Tier2 concept pass, once for the non-concept
claim/formula/procedure pass), so every interactive source upload paid for it twice,
synchronously, on the extraction path.

Fix (mirrors _ent_chunk_map/_elem_chunk_map, landed PR#157): cluster_map is now
version-cached via repo._vector_cache (single-flight + LRU), keyed on
tuple(repo._scale_index_version(nb)) — the same version key the scale index and
other cluster-derived caches already use. _invalidate_unified_cache gained a sibling
":clustermap" eviction line so any explicit invalidation call also drops this cache
(defense against same-second in-place rewrites that don't move the version tuple —
see the mutation-site audit below). incremental_fuse_source's two call sites now
share one local variable for its own (already-idempotent) Tier1 concept pass instead
of calling cluster_map twice.

Mutation-site audit (grepped every `UPDATE concept_clusters` / `DELETE FROM
concept_clusters` in app/): there is no literal `UPDATE concept_clusters` anywhere —
all mutations are DELETE+INSERT. Site-by-site:

  1. write_clusters       — test-only, no prod caller. Found to be MISSING
                             invalidation during this task's TDD pass (a full-suite
                             run caught test_write_clusters_is_per_type_isolated
                             regressing on a same-second rewrite) -> now
                             self-invalidates (_invalidate_unified_cache at its end).
  2. append_clusters      — prod caller: incremental_fuse_source (both call sites).
                             Self-invalidates when it adds >=1 row (added defensively
                             in this task; the caller ALSO invalidates at its own
                             end, so this is belt-and-suspenders against a future
                             caller that forgets, and against the same-second
                             INSERT-ties-MAX(created_at) hazard).
  3. _write_cluster_map_streamed — prod caller: rebuild_unified_kg only, which
                             calls _invalidate_unified_cache at its end.
  4. incremental_fuse_source's own orphan-row DELETE — same method, same
                             _invalidate_unified_cache at its end.

So every concept_clusters writer now either self-invalidates or is reached by its
caller's explicit invalidation — verified by a passing full-suite run (baseline vs.
after: see report). The "same-second rename" test below exercises the DELETE+INSERT
re-write hazard directly via a RAW SQL rewrite that bypasses every bump helper
(COUNT/MAX(created_at) do NOT move within the same second, and P0-A's
cluster_mutation_seq is untouched by a raw SQL write too — both would leave
_scale_index_version's version tuple pinned) to prove the explicit invalidation is
what saves us, not the version tuple.
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
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _now():
    return "2026-07-02T00:00:00"


def _oracle_cluster_map(repo, notebook_id):
    """Verbatim copy of the OLD (pre-cache) full-scan implementation."""
    with repo._connect() as db:
        rows = db.execute(
            "SELECT member_object_id, canonical_id FROM concept_clusters WHERE notebook_id=?",
            (notebook_id,),
        ).fetchall()
    return {r["member_object_id"]: r["canonical_id"] for r in rows}


def _seed_cluster_row(repo, nb_id, *, cc_id, canonical_id, member_id, canonical_name,
                       object_type="concept", created_at=None):
    """Raw-SQL row insert (test-only fixture, no prod caller uses this shape
    directly). P0-A: bump cluster_mutation_seq the way every real
    concept_clusters writer does (write_clusters/append_clusters/...) — mirrors
    test_scale_index_version_probe.py's _add_chunk/_add_embedding, which bump
    kg_mutation_seq explicitly after their own raw INSERTs for the same reason:
    a raw INSERT that bypassed the choke point would, correctly, be invisible
    to the seq-keyed fast path (that is not a production code path either)."""
    now = created_at or _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,canonical_description,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cc_id, nb_id, canonical_id, member_id, canonical_name, object_type, "", now))
        repo._bump_cluster_mutation_seq(db, nb_id)


def _loader_spy(repo, monkeypatch):
    """Count calls to the concept_clusters full-scan query specifically (not just
    any db.execute), so we measure loader invocations rather than incidental SQL.
    Wraps runtime.database.connect — the boundary the cluster_map loader actually
    goes through since c9ddf31 single-owner'd it into RetrievalCandidateService
    (the old repo._connect seat counted 0 forever). execute() is spied."""
    calls = {"n": 0}
    orig_connect = repo._runtime.database.connect

    class _SpyConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            if "member_object_id, canonical_id FROM concept_clusters" in sql:
                calls["n"] += 1
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    def _wrapped_connect():
        return _SpyConn(orig_connect())

    monkeypatch.setattr(repo._runtime.database, "connect", _wrapped_connect)
    return calls


# ── equivalence oracle ──────────────────────────────────────────────────────

def test_cluster_map_equals_oracle_basic(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_cluster_row(repo, nb.id, cc_id="cc-1", canonical_id="K-a", member_id="o1",
                       canonical_name="A")
    _seed_cluster_row(repo, nb.id, cc_id="cc-2", canonical_id="K-a", member_id="o2",
                       canonical_name="A")
    _seed_cluster_row(repo, nb.id, cc_id="cc-3", canonical_id="K-b", member_id="o3",
                       canonical_name="B")
    got = repo.cluster_map(nb.id)
    want = _oracle_cluster_map(repo, nb.id)
    assert got == want == {"o1": "K-a", "o2": "K-a", "o3": "K-b"}


def test_cluster_map_equals_oracle_empty_notebook(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    assert repo.cluster_map(nb.id) == _oracle_cluster_map(repo, nb.id) == {}


def test_candidate_cluster_map_reads_only_requested_ids(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_cluster_row(repo, nb.id, cc_id="cc-1", canonical_id="K-a", member_id="o1",
                      canonical_name="A")
    _seed_cluster_row(repo, nb.id, cc_id="cc-2", canonical_id="K-b", member_id="o2",
                      canonical_name="B")
    service = repo.retrieval.candidates
    batches = []
    real_fold = service.unified_kg.cluster_fold_rows

    def bounded_fold(database, notebook_id, ids):
        batches.append(list(ids))
        return real_fold(database, notebook_id, ids)

    def reject_full_scan(*_args, **_kwargs):
        raise AssertionError("candidate canonical fold must not load the full cluster map")

    monkeypatch.setattr(service.unified_kg, "cluster_fold_rows", bounded_fold)
    monkeypatch.setattr(service.unified_kg, "cluster_map_rows", reject_full_scan)

    requested = ["o1", *(f"missing-{index}" for index in range(900))]
    assert service._candidate_cluster_map(nb.id, requested) == {"o1": "K-a"}
    assert [len(batch) for batch in batches] == [900, 1]
    assert {item for batch in batches for item in batch} == set(requested)


# ── caching behavior: loader-count memoization ──────────────────────────────

def test_cluster_map_second_call_no_sql_scan(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_cluster_row(repo, nb.id, cc_id="cc-1", canonical_id="K-a", member_id="o1",
                       canonical_name="A")
    first = repo.cluster_map(nb.id)
    calls = _loader_spy(repo, monkeypatch)
    second = repo.cluster_map(nb.id)
    assert calls["n"] == 0   # cache hit — loader's SELECT never runs
    assert second == first


def test_cluster_map_many_calls_one_loader_run(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_cluster_row(repo, nb.id, cc_id="cc-1", canonical_id="K-a", member_id="o1",
                       canonical_name="A")
    calls = _loader_spy(repo, monkeypatch)
    for _ in range(5):
        repo.cluster_map(nb.id)
    assert calls["n"] == 1   # only the first call actually scans


# ── invalidation: writes force a refresh ────────────────────────────────────

def test_cluster_map_refreshes_after_append_clusters(repo):
    """append_clusters self-invalidates (no caller-remembers-to-invalidate burden),
    including the same-second case: a first cluster_map() call warms the cache,
    then append_clusters (same second, since the test runs in well under 1s)
    must still force a refresh rather than serve the pre-append cached dict."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                   "payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-1", nb.id, "concept", "approved", "", json.dumps({"name": "X"}), "[]",
                    "src-A", _now(), _now()))
    before = repo.cluster_map(nb.id)   # warm the cache on the empty state
    assert before == {}
    repo.append_clusters(nb.id, [{"canonical_id": "K-x", "member_object_id": "ko-1",
                                   "canonical_name": "X"}], object_type="concept")
    after = repo.cluster_map(nb.id)    # append_clusters must have invalidated itself
    assert after == {"ko-1": "K-x"} == _oracle_cluster_map(repo, nb.id)


def test_cluster_map_refreshes_after_write_clusters(repo):
    """write_clusters self-invalidates too (same-second DELETE+INSERT hazard)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_clusters(nb.id, [{"canonical_id": "K-a", "member_object_id": "o1",
                                  "canonical_name": "A"}])
    before = repo.cluster_map(nb.id)   # warm the cache
    assert before == {"o1": "K-a"}
    repo.write_clusters(nb.id, [{"canonical_id": "K-b", "member_object_id": "o2",
                                  "canonical_name": "B"}])
    after = repo.cluster_map(nb.id)    # write_clusters must have invalidated itself
    assert after == {"o2": "K-b"} == _oracle_cluster_map(repo, nb.id)


def test_cluster_map_refreshes_after_rebuild(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                   "payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-A", nb.id, "concept", "approved", "", json.dumps({"name": "MoE"}), "[]",
                    "src-A", _now(), _now()))
    before = repo.cluster_map(nb.id)
    assert before == {}
    repo.rebuild_unified_kg(nb.id)
    after = repo.cluster_map(nb.id)
    assert "ko-A" in after
    assert after == _oracle_cluster_map(repo, nb.id)


def test_cluster_map_invalidates_on_kg_mutation_version_bump(repo):
    """Adding a cluster row via a fresh timestamp (normal, non-same-second case)
    must be picked up even without an explicit _invalidate_unified_cache call.
    P0-A: this now flows through cluster_mutation_seq (bumped by
    _seed_cluster_row, mirroring every real concept_clusters writer) rather
    than a live concept_clusters COUNT/MAX re-read — same observable outcome,
    O(1) signal instead of a table aggregate."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_cluster_row(repo, nb.id, cc_id="cc-1", canonical_id="K-a", member_id="o1",
                       canonical_name="A", created_at="2026-07-02T00:00:00")
    before = repo.cluster_map(nb.id)
    assert before == {"o1": "K-a"}
    _seed_cluster_row(repo, nb.id, cc_id="cc-2", canonical_id="K-b", member_id="o2",
                       canonical_name="B", created_at="2026-07-02T00:00:01")
    after = repo.cluster_map(nb.id)
    assert after == {"o1": "K-a", "o2": "K-b"} == _oracle_cluster_map(repo, nb.id)


# ── the hard gate: in-place rewrite (rename), same second, explicit invalidation ──

def test_cluster_map_invalidates_on_same_second_rename_via_explicit_invalidation(repo):
    """Real in-place-edit entry point: write_clusters (used by rebuild's streamed
    writer _write_cluster_map_streamed too) DELETEs and re-INSERTs a member's row
    under a renamed canonical_name/canonical_id in the SAME second. This test
    exercises the rewrite as a RAW SQL block (not through write_clusters) so it
    bypasses cluster_mutation_seq's bump too, matching the pre-P0-A hazard this
    test locks in: COUNT stays 1 and MAX(created_at) is pinned to the same
    literal timestamp, so _scale_index_version's version tuple does NOT change —
    it alone would serve a stale cached map. Only the explicit
    _invalidate_unified_cache call (which incremental_fuse_source /
    rebuild_unified_kg already make) saves us."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    same_ts = "2026-07-02T00:00:00"
    _seed_cluster_row(repo, nb.id, cc_id="cc-1", canonical_id="K-old-name", member_id="o1",
                       canonical_name="Old Name", created_at=same_ts)
    before = repo.cluster_map(nb.id)
    assert before == {"o1": "K-old-name"}

    ver_before = tuple(repo._scale_index_version(nb.id))
    # "Rename" = DELETE the member's row, re-INSERT under the new canonical_id, same ts.
    with repo._write() as db:
        db.execute("DELETE FROM concept_clusters WHERE notebook_id=? AND member_object_id=?",
                   (nb.id, "o1"))
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,canonical_description,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("cc-1b", nb.id, "K-new-name", "o1", "New Name", "concept", "", same_ts))
    ver_after_no_invalidate = tuple(repo._scale_index_version(nb.id))
    # Prove the hazard is real: COUNT unchanged (1), MAX(created_at) unchanged (same_ts)
    # -> version tuple is byte-identical, so a version-keyed cache alone would NOT bust.
    assert ver_after_no_invalidate == ver_before

    # Without invalidation the stale entry is still what a version-only cache would
    # return (this is exactly what makes the explicit invalidation load-bearing).
    stale = repo._vector_cache.get(f"{nb.id}:clustermap", ver_after_no_invalidate,
                                    lambda: (_ for _ in ()).throw(AssertionError(
                                        "loader should not run — must be a cache hit")))
    assert stale == {"o1": "K-old-name"}

    # Now invoke the real invalidation path (same call incremental_fuse_source/
    # rebuild_unified_kg make on every write) and confirm the refreshed value.
    repo._invalidate_unified_cache(nb.id)
    after = repo.cluster_map(nb.id)
    assert after == {"o1": "K-new-name"} == _oracle_cluster_map(repo, nb.id)


# ── incremental_fuse_source: single shared load per fuse call ──────────────

def test_incremental_fuse_source_loads_cluster_map_once(repo, monkeypatch):
    """incremental_fuse_source has two call sites for cluster_map (Tier1 concept
    pass + non-concept claim/formula/procedure pass). The whole fuse must trigger
    exactly one loader run (cache hit for the second site), not two full scans."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = _now()
    with repo._write() as db:
        # existing concept (pre-populates concept_clusters so the loader has rows)
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                   "payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-old", nb.id, "concept", "approved", "", json.dumps({"name": "Old"}), "[]",
                    "src-A", now, now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
                   "canonical_name,object_type,canonical_description,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("cc-old", nb.id, "K-old", "ko-old", "Old", "concept", "", now))
        # new source: one concept + one claim + one formula + one procedure
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                   "payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-new", nb.id, "concept", "approved", "", json.dumps({"name": "New"}), "[]",
                    "src-B", now, now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                   "payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("kl-new", nb.id, "claim", "approved", "", json.dumps({"name": "New claim"}), "[]",
                    "src-B", now, now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                   "payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("kf-new", nb.id, "formula", "approved", "", json.dumps({"name": "New formula"}), "[]",
                    "src-B", now, now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                   "payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("kp-new", nb.id, "procedure",
                    "approved", "", json.dumps({"name": "New procedure", "steps": []}), "[]",
                    "src-B", now, now))
    calls = _loader_spy(repo, monkeypatch)
    repo.incremental_fuse_source(nb.id, "src-B")
    assert calls["n"] == 1   # single shared load across the whole fuse call


def test_incremental_fuse_source_result_matches_oracle(repo):
    """Equivalence: the fused cluster_map (via the cache) must match the
    uncached oracle scan, i.e. sharing/caching cluster_map inside fuse changes
    nothing about the fusion RESULT."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = _now()
    from app.services.kg_merge import _norm
    cid = "K-" + _norm("Mixture-of-Experts (MoE)")
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                   "payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-A", nb.id, "concept", "approved", "",
                    json.dumps({"name": "Mixture-of-Experts (MoE)"}), "[]", "src-A", now, now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
                   "canonical_name,object_type,canonical_description,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("cc-A", nb.id, cid, "ko-A", "Mixture-of-Experts (MoE)", "concept", "", now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
                   "payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-B", nb.id, "concept", "approved", "",
                    json.dumps({"name": "Mixture-of-Experts (MoE)"}), "[]", "src-B", now, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    got = repo.cluster_map(nb.id)
    want = _oracle_cluster_map(repo, nb.id)
    assert got == want
    assert got.get("ko-B") == got.get("ko-A")


# ── consumer non-mutation audit ──────────────────────────────────────────────

def test_cluster_map_cached_dict_not_mutated_by_repeated_reads(repo):
    """cluster_map returns the SAME cached dict object across calls (cache hit) —
    any consumer that mutated it in place would corrupt subsequent callers/requests.
    This test pins the identity + content stability across several read-only
    consumer-shaped operations (mirroring the .get()-only patterns in
    kg_merge.place_new_concepts / derive_unified_graph / retrieval.fold_by_canonical)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_cluster_row(repo, nb.id, cc_id="cc-1", canonical_id="K-a", member_id="o1",
                       canonical_name="A")
    m1 = repo.cluster_map(nb.id)
    # simulate read-only consumer usage
    _ = {k: m1.get(k, k) for k in ["o1", "o2"]}
    m2 = repo.cluster_map(nb.id)
    assert m1 is m2               # same cached object served
    assert m2 == {"o1": "K-a"}    # untouched by the read-only usage above
