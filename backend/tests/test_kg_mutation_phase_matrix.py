"""Task 14 — KgMutationCoordinator event-order matrix.

Every KG mutation's post-commit side effects (unified-cache invalidation +
kg_mutation_seq dirty bump) now route through ``runtime.kg_mutations``. These
tests freeze the EXACT per-operation order matrix:

    store_kg               object chunks of 1000; relation chunks of 1000;
                           dirty bump — ALL in ONE transaction
                           (mark_unified_kg_dirty_in_tx, codex #638 R5 P1);
                           then object embed; relation embed; invalidate
    complete_relations_for_source
                           one transaction PER PAGE, each page's relation
                           insert carrying its own dirty bump when it inserted
                           rows (codex #638 R5); embed + invalidate in the run
                           tail
    relink_notebook_kg     dirty rides EACH SOURCE's own relation transaction
                           (mark_unified_kg_dirty_in_tx, committed atomically
                           with that source's edge insert — a `finally`-keyed
                           bump cannot survive a kill -9 between a source's
                           commit and the run finishing); invalidate stays a
                           single run-level `finally` call after every source;
                           no side effects anywhere when zero edges added
    set_edge_review        relation UPDATE + dirty bump in ONE transaction
                           (mark_unified_kg_dirty_in_tx, R2 P2 fix, codex
                           #638 R2); invalidate stays a separate post-commit
                           call
    write_clusters         replace + cluster-seq bump in ONE transaction; invalidate
    append_clusters        append + cluster-seq bump in one transaction;
                           invalidate only when rows added
    confirm/reject merge   candidate transaction; invalidate; dirty
    review_pending_merges  decision transaction; dirty then invalidate
                           only when decisions exist
    approve_promotion      candidate/base/provenance transaction + dirty bump
                           in ONE transaction (codex #638 R5); best-effort
                           embed; invalidate
    update_knowledge       object transaction + dirty bump in ONE transaction
                           (codex #638 R5); best-effort embed — a payload
                           edit REPLACES the object's existing vector, which
                           carries its OWN second dirty bump inside that
                           replace's own transaction (codex #638 R6 P1: emb_c
                           COUNT(*) cannot see a same-row upsert); invalidate
    merge_knowledge        evidence/provenance/source-status transaction +
                           dirty bump in ONE transaction (codex #638 R5);
                           invalidate
    edge conflict discard  edge UPDATE + dirty bump in ONE transaction (rides
                           set_edge_review, R2 P2 fix); invalidate; second
                           dirty bump (post-commit, belt-and-suspenders)
    node conflict discard/modify
                           object transaction + dirty bump (rides
                           update_knowledge, codex #638 R5); invalidate;
                           second dirty bump (post-commit) — "modify" ALSO
                           carries update_knowledge's own THIRD bump, inside
                           the payload re-embed's replace transaction (codex
                           #638 R6 P1); "discard" is status-only (no embed,
                           no third bump)
    confirm_conflict       apply mutation, then candidate-status transaction
    unified rebuild        cluster rewrite + cluster seq; NO kg_mutation_seq bump
    deep copy / migration / fixture
                           never call the coordinator

The remaining asymmetries (which of invalidate/second-bump comes first, the
conflict double-bumps) are frozen baseline behavior — do NOT normalize them
here.  What is NO LONGER an asymmetry, and must not be reintroduced: whether
a graph-row write's own bump commits with its rows.  codex #638 R5 swept the
whole matrix and made that uniform — see ``kg_mutation``'s module docstring
("FULL CENSUS") for the per-operation result and for the operations
deliberately left with a post-commit or absent bump.

The Task-1 fixture ``mutation_phases.json`` is treated as READ-ONLY input:
these tests replay it, they never regenerate it.

Observation seams are component seams (runtime.database.write /
runtime.kg_mutations / runtime.source_embedding / runtime.embedding_store /
runtime.governance / runtime.unified_kg), never fresh facade patch targets.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path

import pytest

import app.services.concept_merge_review as cmr
import app.services.kg.relink as kg_relink
from app.core.config import Settings
from app.models.schemas import KnowledgeUpdate, MergeRequest, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "backend" / "tests" / "fixtures" / "repository_contract"

# The frozen matrix's documented exemptions (mutation_phases.json is read-only).
EXEMPT_OPERATIONS = {"copy_notebook", "migration_recovery_seed", "streaming_ask"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'phase.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _trace_transactions(repo, monkeypatch, events):
    """Trace commit boundaries at the runtime database component seam (every
    facade `_write` rides it), so no fresh facade patch target is minted."""
    database = repo._runtime.database
    original = database.write

    @contextmanager
    def traced():
        events.append("write.begin")
        with original() as db:
            yield db
        events.append("write.commit")

    monkeypatch.setattr(database, "write", traced)


def _spy_coordinator(repo, monkeypatch, events):
    """Replace the coordinator hooks with pure recorders (no delegation) —
    the facade wrappers must funnel through these for the events to appear."""
    coordinator = repo._runtime.kg_mutations
    monkeypatch.setattr(
        coordinator,
        "invalidate_unified_cache",
        lambda notebook_id: events.append("invalidate"),
    )
    monkeypatch.setattr(
        coordinator,
        "mark_unified_kg_dirty",
        lambda notebook_id: events.append("dirty"),
    )


def _spy_cluster_seq(repo, monkeypatch, events):
    """Wrap (and still delegate) the in-transaction cluster-seq bump."""
    coordinator = repo._runtime.kg_mutations
    original = coordinator.bump_cluster_mutation_seq

    def wrapped(db, notebook_id):
        events.append("cluster_seq")
        return original(db, notebook_id)

    monkeypatch.setattr(coordinator, "bump_cluster_mutation_seq", wrapped)


def _spy_dirty_in_tx(repo, monkeypatch, events):
    """Wrap (and still delegate) the in-transaction dirty bump — the seam
    ``set_edge_review`` rides since the R2 P2 fix (codex #638 R2), same
    wrap-and-delegate shape as ``_spy_cluster_seq``: callers need the real
    return value (the freshly-bumped seq), not a pure recorder."""
    coordinator = repo._runtime.kg_mutations
    original = coordinator.mark_unified_kg_dirty_in_tx

    def wrapped(db, notebook_id):
        events.append("dirty")
        return original(db, notebook_id)

    monkeypatch.setattr(coordinator, "mark_unified_kg_dirty_in_tx", wrapped)


def _spy_embed_store(repo, monkeypatch, events):
    """Wrap (and still delegate) the best-effort payload-embed hook at the
    embedding-store seam. Was a pure recorder before codex #638 R6 P1 (the
    hook's own flush transaction was not under test); now update_knowledge
    threads its in-tx dirty callback INTO that flush transaction (see
    ``SourceEmbeddingService.embed_knowledge``'s docstring), so this seam
    must actually run the write — its "write.begin"/"dirty"/"write.commit"
    are observed through the SAME instrumented seams as every other write,
    not synthesized here."""
    original = repo._runtime.embedding_store.replace_knowledge_vectors

    def wrapped(notebook_id, rows, created_at="", mark_dirty_in_tx=None):
        events.append("embed")
        return original(
            notebook_id, rows, created_at=created_at,
            mark_dirty_in_tx=mark_dirty_in_tx,
        )

    monkeypatch.setattr(
        repo._runtime.embedding_store, "replace_knowledge_vectors", wrapped
    )


def _claim(local_id, name):
    return {
        "local_id": local_id,
        "object_type": "claim",
        "payload": {"name": name},
        "evidence": [],
    }


def _seed_kg(repo, name="phase kg"):
    notebook = repo.create_notebook(NotebookCreate(name=name))
    repo.store_kg(
        notebook.id,
        None,
        [_claim("a", "claim a"), _claim("b", "claim b"), _claim("c", "claim c")],
        [
            {"source_local_id": "a", "target_local_id": "b",
             "edge_type": "contrasts_with", "evidence": []},
            {"source_local_id": "b", "target_local_id": "c",
             "edge_type": "supports", "evidence": []},
        ],
    )
    with repo._connect() as db:
        object_ids = [r["id"] for r in db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? ORDER BY created_at, id",
            (notebook.id,)).fetchall()]
        relation_ids = [r["id"] for r in db.execute(
            "SELECT id FROM knowledge_relations WHERE notebook_id=? ORDER BY created_at, id",
            (notebook.id,)).fetchall()]
    return notebook.id, object_ids, relation_ids


def _state_row(repo, notebook_id):
    with repo._connect() as db:
        return repo._runtime.unified_kg.state_row(db, notebook_id)


# ---------------------------------------------------------------- coordinator


def test_runtime_coordinator_shares_facade_cache_objects(repo):
    """The coordinator wraps the EXISTING facade cache objects by identity —
    no replacement caches are allocated (Task 17 transfers ownership later)."""
    coordinator = repo._runtime.kg_mutations
    assert coordinator.unified_store is repo._runtime.unified_kg
    assert coordinator.unified_cache is repo._unified_cache
    assert coordinator.vector_cache is repo._vector_cache
    assert coordinator.auto_index_checked is repo._auto_index_checked
    assert coordinator.notebook_languages is repo._notebook_langs_cache


def test_frozen_mutation_matrix_documents_the_exemptions():
    """mutation_phases.json is the frozen Task-1 matrix (read-only input):
    exactly copy_notebook / migration_recovery_seed / streaming_ask are exempt
    from the coordinator; every other operation invalidates + marks dirty +
    bumps the version."""
    mutation = json.loads(
        (FIXTURES / "mutation_phases.json").read_text(encoding="utf-8")
    )
    exempt = {op for op, contract in mutation.items() if contract["exemption"]}
    assert exempt == EXEMPT_OPERATIONS
    for op, contract in mutation.items():
        expected = op not in exempt
        assert contract["cache_invalidation"] is expected, op
        assert contract["unified_dirty"] is expected, op
        assert contract["version_bump"] is expected, op


# ------------------------------------------------------------------- store_kg


def test_store_kg_preserves_post_commit_order(repo, monkeypatch):
    """codex #638 R5 P1: the dirty bump moved from the post-embed tail INTO
    the graph-row transaction, so it now precedes both embeds; only the cache
    eviction is left after them."""
    runtime = repo._runtime
    events = []
    monkeypatch.setattr(
        runtime.source_embedding,
        "embed_objects_batch",
        lambda notebook_id, items, progress=None, commit_every=None: (
            events.append("embed_objects")
        ),
    )
    monkeypatch.setattr(
        runtime.source_embedding,
        "embed_relations_batch",
        lambda notebook_id, rel_items: events.append("embed_relations"),
    )
    monkeypatch.setattr(
        runtime.kg_mutations,
        "invalidate_unified_cache",
        lambda notebook_id: events.append("invalidate"),
    )
    # Pure recorder (no delegation) here, unlike ``_spy_dirty_in_tx``: this
    # case drives a synthetic notebook id that has no ``notebooks`` row, so a
    # real bump would trip unified_kg_state's foreign key. store_kg ignores the
    # returned seq, so nothing under test needs the delegation.
    monkeypatch.setattr(
        runtime.kg_mutations,
        "mark_unified_kg_dirty_in_tx",
        lambda db, notebook_id: events.append("dirty") or 0,
    )
    monkeypatch.setattr(
        runtime.kg_mutations,
        "mark_unified_kg_dirty",
        lambda notebook_id: events.append("dirty.post_tx"),
    )
    repo.store_kg("nb", None, [], [])
    assert events == [
        "dirty", "embed_objects", "embed_relations", "invalidate"
    ]


def test_store_kg_chunks_thousand_rows_before_embeds_and_hooks(repo, monkeypatch):
    """1500 objects / 1200 relations share one source-atomic transaction —
    and since codex #638 R5 P1 so does the dirty bump, which therefore lands
    BEFORE that commit. Embeds and invalidate follow it."""
    notebook = repo.create_notebook(NotebookCreate(name="chunked"))
    objects = [_claim(f"L{i}", f"claim {i}") for i in range(1500)]
    relations = [
        {"source_local_id": "L0", "target_local_id": f"L{i}",
         "edge_type": "about", "evidence": []}
        for i in range(1, 1201)
    ]
    events = []
    _trace_transactions(repo, monkeypatch, events)
    monkeypatch.setattr(
        repo._runtime.source_embedding,
        "embed_objects_batch",
        lambda notebook_id, items, progress=None, commit_every=None: (
            events.append("embed_objects")
        ),
    )
    monkeypatch.setattr(
        repo._runtime.source_embedding,
        "embed_relations_batch",
        lambda notebook_id, rel_items: events.append("embed_relations"),
    )
    _spy_coordinator(repo, monkeypatch, events)
    _spy_dirty_in_tx(repo, monkeypatch, events)

    n_obj, n_rel = repo.store_kg(notebook.id, None, objects, relations)

    assert (n_obj, n_rel) == (1500, 1200)
    assert events == [
        "write.begin",
        "dirty",
        "write.commit",
        "embed_objects",
        "embed_relations",
        "invalidate",
    ]


# --------------------------------------------------------- relink_notebook_kg


def test_relink_zero_added_edges_has_no_side_effects(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="relink empty"))
    events = []
    _trace_transactions(repo, monkeypatch, events)
    _spy_coordinator(repo, monkeypatch, events)

    out = repo.relink_notebook_kg(notebook.id)

    assert out["edges_added"] == 0
    assert events == []


# -------------------------------------------------------------- edge review


def test_set_edge_review_bumps_dirty_inside_the_transaction_then_invalidates(
    repo, monkeypatch
):
    """R2 P2 fix (codex #638 R2): the dirty bump now rides INSIDE the same
    write transaction as the review_status UPDATE (mark_unified_kg_dirty_in_tx,
    same shape write_clusters/append_clusters already use for their own
    cluster-seq bump) instead of committing in a separate transaction AFTER
    the UPDATE's own commit — see review_queue_memo's module docstring for
    the two races that separation used to open. invalidate stays a separate
    post-commit call, unchanged."""
    notebook_id, _objects, relation_ids = _seed_kg(repo, "edge review phase")
    events = []
    _trace_transactions(repo, monkeypatch, events)
    monkeypatch.setattr(
        repo._runtime.kg_mutations,
        "invalidate_unified_cache",
        lambda notebook_id: events.append("invalidate"),
    )
    _spy_dirty_in_tx(repo, monkeypatch, events)

    repo.set_edge_review(notebook_id, relation_ids[0], "rejected")

    assert events == ["write.begin", "dirty", "write.commit", "invalidate"]


# ------------------------------------------------------------------ clusters


def test_write_clusters_bumps_cluster_seq_in_the_replace_transaction(
    repo, monkeypatch
):
    notebook_id, object_ids, _relations = _seed_kg(repo, "write clusters phase")
    events = []
    _trace_transactions(repo, monkeypatch, events)
    _spy_coordinator(repo, monkeypatch, events)
    _spy_cluster_seq(repo, monkeypatch, events)

    repo.write_clusters(notebook_id, [{
        "canonical_id": "K-1",
        "member_object_id": object_ids[0],
        "canonical_name": "claim a",
    }])

    assert events == ["write.begin", "cluster_seq", "write.commit", "invalidate"]
    assert "dirty" not in events  # rebuild-owned rewrites never bump kg_mutation_seq
    assert int(_state_row(repo, notebook_id)["cluster_mutation_seq"]) >= 1


def test_append_clusters_only_invalidates_when_rows_were_added(repo, monkeypatch):
    notebook_id, object_ids, _relations = _seed_kg(repo, "append clusters phase")
    rows = [{
        "canonical_id": "K-1",
        "member_object_id": object_ids[0],
        "canonical_name": "claim a",
    }]
    events = []
    _trace_transactions(repo, monkeypatch, events)
    _spy_coordinator(repo, monkeypatch, events)
    _spy_cluster_seq(repo, monkeypatch, events)

    assert repo.append_clusters(notebook_id, rows) == 1
    assert events == ["write.begin", "cluster_seq", "write.commit", "invalidate"]

    events.clear()
    assert repo.append_clusters(notebook_id, rows) == 0  # idempotent no-op
    assert events == ["write.begin", "write.commit"]  # no bump, no invalidate


# ------------------------------------------------------------- merge review


def _seed_merge_candidate(repo, notebook_id, a="K-a", b="K-b"):
    repo.write_merge_candidate(notebook_id, a, b, 0.9)
    with repo._connect() as db:
        return db.execute(
            "SELECT id FROM concept_merge_candidates WHERE notebook_id=? "
            "AND canonical_a=? AND canonical_b=?",
            (notebook_id, a, b),
        ).fetchone()["id"]


def test_only_confirm_merge_invalidates_and_marks_dirty(
    repo, monkeypatch
):
    notebook_id, _objects, _relations = _seed_kg(repo, "merge decision phase")
    first = _seed_merge_candidate(repo, notebook_id, "K-a", "K-b")
    duplicate = _seed_merge_candidate(repo, notebook_id, "K-a", "K-b")
    second = _seed_merge_candidate(repo, notebook_id, "K-c", "K-d")
    events = []
    _trace_transactions(repo, monkeypatch, events)
    _spy_coordinator(repo, monkeypatch, events)

    repo.confirm_merge(notebook_id, first)
    assert events == ["write.begin", "write.commit", "invalidate", "dirty"]

    # Model a legacy/stale pending duplicate beside an already-confirmed row.
    # Rejecting the visible pending id is still a reversal of the pair's
    # materialized union, so every sibling must settle and the graph must dirty.
    with repo._write() as db:
        db.execute(
            "UPDATE concept_merge_candidates SET status='pending' WHERE id=?",
            (duplicate,),
        )

    events.clear()
    repo.reject_merge(notebook_id, second)
    # A rejection is a durable cannot-link but does not alter the current
    # clusters, so it neither invalidates retrieval nor dirties the graph.
    assert events == ["write.begin", "write.commit"]

    events.clear()
    repo.reject_merge(notebook_id, duplicate)
    assert events == ["write.begin", "write.commit", "invalidate", "dirty"]
    with repo._connect() as db:
        statuses = db.execute(
            "SELECT status FROM concept_merge_candidates WHERE notebook_id=? AND "
            "((canonical_a='K-a' AND canonical_b='K-b') OR "
            "(canonical_a='K-b' AND canonical_b='K-a'))",
            (notebook_id,),
        ).fetchall()
    assert statuses and {row["status"] for row in statuses} == {"rejected"}


def test_review_pending_merges_dirty_then_invalidate_only_with_decisions(
    repo, monkeypatch
):
    notebook_id, _objects, _relations = _seed_kg(repo, "review merges phase")
    candidate_id = _seed_merge_candidate(repo, notebook_id)
    monkeypatch.setattr(
        cmr,
        "review_merge_candidates",
        lambda client, pending, **kwargs: [{
            "candidate_id": c["id"], "decision": "merge",
            "confidence": 0.99, "rationale": "",
        } for c in pending],
    )
    events = []
    _trace_transactions(repo, monkeypatch, events)
    _spy_coordinator(repo, monkeypatch, events)

    out = repo.review_pending_merges(notebook_id)

    assert out["confirmed"] == 1
    assert events == ["write.begin", "write.commit", "dirty", "invalidate"]
    assert candidate_id  # decided candidate came from the seeded queue

    # No decisions (queue drained) → decision transaction still runs, but the
    # dirty/invalidate hooks are skipped.
    monkeypatch.setattr(
        cmr, "review_merge_candidates", lambda client, pending, **kwargs: []
    )
    events.clear()
    out = repo.review_pending_merges(notebook_id)
    assert out["reviewed"] == 0
    assert events == ["write.begin", "write.commit"]


# ---------------------------------------------------------------- promotion


def test_approve_promotion_bumps_inside_the_transaction_then_embeds(
    repo, monkeypatch
):
    """codex #638 R5: the base-object insert and its dirty bump share one
    transaction, so the bump precedes the best-effort payload embed instead of
    trailing it. That embed is not wrapped in try/except here, so the old
    ex-post bump was simply skipped whenever the embedder was down.

    codex #638 R6 P1: approve_promotion's embed is a first-time INSERT for a
    brand-new base object_id (never a same-row replace — see kg_mutation's
    VECTOR-REPLACE CENSUS), so it passes no ``mark_dirty_in_tx`` and gets no
    second bump — only the "write.begin"/"write.commit" pair around the
    (now genuinely executed, not stubbed) vector INSERT itself, no "dirty"
    inside it."""
    personal = repo.create_notebook(NotebookCreate(name="personal"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(personal.id, [base.id], "user-local")
    object_id = repo._test_insert_object(
        personal.id, "claim", {"name": "promotion phase"}
    )
    candidate = repo.propose_promotion(personal.id, object_id)
    events = []
    _trace_transactions(repo, monkeypatch, events)
    _spy_coordinator(repo, monkeypatch, events)
    _spy_dirty_in_tx(repo, monkeypatch, events)
    _spy_embed_store(repo, monkeypatch, events)

    repo.approve_promotion(candidate["id"])

    assert events == [
        "write.begin", "dirty", "write.commit",       # base-object insert
        "embed", "write.begin", "write.commit",       # vector INSERT, no bump
        "invalidate",
    ]


# --------------------------------------------------- knowledge update / merge


def test_update_knowledge_bumps_inside_the_transaction_then_embeds(
    repo, monkeypatch
):
    """codex #638 R5: the object UPDATE and its dirty bump share one
    transaction, so the bump precedes the best-effort re-embed instead of
    trailing it — no window where the edited row is durable under the old
    seq, and no way for a crash between the two commits to strand it there.

    codex #638 R6 P1: the re-embed itself REPLACES the object's existing
    vector (same object_id), which emb_c's COUNT(*) cannot see — so it now
    carries its OWN second bump, inside ITS OWN write transaction (a second
    "write.begin"/"dirty"/"write.commit" triple after "embed", before
    "invalidate")."""
    notebook_id, object_ids, _relations = _seed_kg(repo, "update phase")
    events = []
    _trace_transactions(repo, monkeypatch, events)
    _spy_coordinator(repo, monkeypatch, events)
    _spy_dirty_in_tx(repo, monkeypatch, events)
    _spy_embed_store(repo, monkeypatch, events)

    repo.update_knowledge(
        notebook_id, object_ids[0], KnowledgeUpdate(payload={"name": "renamed"})
    )
    assert events == [
        "write.begin", "dirty", "write.commit",           # object update
        "embed", "write.begin", "dirty", "write.commit",  # vector replace (R6 P1)
        "invalidate",
    ]

    # Status-only edit skips the payload re-embed; the in-tx bump is unchanged.
    events.clear()
    repo.update_knowledge(
        notebook_id, object_ids[0], KnowledgeUpdate(status="reviewed")
    )
    assert events == ["write.begin", "dirty", "write.commit", "invalidate"]


def test_update_knowledge_vector_replace_advances_seq_past_concurrent_reader(
    repo, monkeypatch
):
    """Reproduces the codex #638 R6 race directly, in seq VALUES rather than
    event names: a concurrent reader landing in the window BETWEEN
    update_knowledge's row-update commit (bump to N) and its vector-replace
    commit sees seq N paired with the STILL-OLD vector — exactly what a
    racing unified-KG rebuild would read and stamp as "current" at seq N,
    with nothing to ever invalidate it again if the replace did not bump.
    The fix: the replace's OWN bump advances seq a SECOND time (to N+1), so
    that stale-looking snapshot is provably not the last word — the very
    next seq read moves past it, and a seq-keyed cache built from it is due
    for a recompute.

    Also covers the failure boundary: when the embed store itself raises,
    the vector never changes, so seq must NOT advance a second time either —
    it stops at N, identical to pre-R6-P1 behavior (the row edit's own R5
    bump is untouched either way)."""
    import numpy as np

    from app.domain.vector_index import decode_vector

    notebook_id, object_ids, _relations = _seed_kg(repo, "vector replace race")
    object_id = object_ids[0]

    def _seq() -> int:
        with repo._connect() as db:
            return repo._runtime.unified_kg.graph_seq_row(db, notebook_id)[0]

    def _vector():
        with repo._connect() as db:
            row = db.execute(
                "SELECT vector FROM knowledge_embeddings WHERE object_id=?",
                (object_id,),
            ).fetchone()
        return decode_vector(row["vector"]) if row else None

    seq_before = _seq()
    old_vector = _vector()
    assert old_vector is not None  # store_kg already embedded it while seeding

    # Land the "concurrent reader" exactly between the two commits: hook the
    # embed step's ENTRY, which runs strictly after update_object_in_
    # transaction's `with self._write()` block has already exited (committed)
    # and strictly before replace_knowledge_vectors opens its own transaction.
    concurrent_read: dict = {}
    original_embed = repo._runtime.source_embedding.embed_knowledge

    def spy_embed(*args, **kwargs):
        concurrent_read["seq"] = _seq()
        concurrent_read["vector"] = _vector()
        return original_embed(*args, **kwargs)

    monkeypatch.setattr(
        repo._runtime.source_embedding, "embed_knowledge", spy_embed
    )

    repo.update_knowledge(
        notebook_id, object_id, KnowledgeUpdate(payload={"name": "renamed for race"})
    )

    # The row-update's own bump (codex #638 R5) already landed by the time the
    # vector-replace transaction opened...
    assert concurrent_read["seq"] == seq_before + 1
    # ...paired with the STALE vector — the exact race codex #638 R6 P1 closes.
    assert np.array_equal(concurrent_read["vector"], old_vector)

    # The fix: the replace advances seq a SECOND time, past the stale
    # snapshot a concurrent reader could have taken away as "current".
    seq_after = _seq()
    new_vector = _vector()
    assert seq_after == seq_before + 2
    assert not np.array_equal(new_vector, old_vector)

    # Failure boundary: an embed-store outage must not fabricate a second
    # bump for a vector that never actually changed.
    seq_before_failure = _seq()
    monkeypatch.setattr(
        repo._runtime.embedding_store,
        "replace_knowledge_vectors",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("embed store down")),
    )
    repo.update_knowledge(
        notebook_id, object_id, KnowledgeUpdate(payload={"name": "renamed again"})
    )
    assert _seq() == seq_before_failure + 1              # row bump only, exactly one
    assert np.array_equal(_vector(), new_vector)          # vector untouched by the failure


def test_merge_knowledge_bumps_inside_the_transaction_then_invalidates(
    repo, monkeypatch
):
    notebook_id, object_ids, _relations = _seed_kg(repo, "merge objects phase")
    events = []
    _trace_transactions(repo, monkeypatch, events)
    _spy_coordinator(repo, monkeypatch, events)
    _spy_dirty_in_tx(repo, monkeypatch, events)

    repo.merge_knowledge(
        notebook_id, object_ids[1], MergeRequest(into_id=object_ids[0])
    )

    # codex #638 R5: the deprecation and its seq bump are one commit; only the
    # process-local cache eviction is left after it.
    assert events == ["write.begin", "dirty", "write.commit", "invalidate"]


# ------------------------------------------------------- conflict resolution


def test_edge_conflict_discard_adds_a_second_dirty_bump(repo, monkeypatch):
    """The loser edge's rejection rides ``set_edge_review`` (R2 P2 fix, codex
    #638 R2: its own dirty bump now happens INSIDE its write transaction, so
    the first "dirty" moves before "write.commit" instead of after);
    ``apply_conflict_resolution``'s explicit belt-and-suspenders SECOND bump
    is unchanged — still the post-tx, non-tx ``mark_unified_kg_dirty`` call
    ``_spy_coordinator`` observes."""
    notebook_id, _objects, relation_ids = _seed_kg(repo, "edge conflict phase")
    events = []
    _trace_transactions(repo, monkeypatch, events)
    _spy_coordinator(repo, monkeypatch, events)
    _spy_dirty_in_tx(repo, monkeypatch, events)

    out = repo.apply_conflict_resolution(
        notebook_id,
        kind="edge",
        left_ref=relation_ids[0],
        right_ref=relation_ids[1],
        resolution="discard",
        winner_ref=relation_ids[0],
    )

    assert out == {"action": "discard", "loser": relation_ids[1]}
    assert events == [
        "write.begin", "dirty", "write.commit", "invalidate", "dirty"
    ]


def test_node_conflict_discard_and_modify_add_a_second_dirty_bump(
    repo, monkeypatch
):
    """``update_knowledge``'s own bump now rides its transaction (codex #638
    R5), so the FIRST "dirty" moves before "write.commit";
    ``apply_conflict_resolution``'s explicit belt-and-suspenders SECOND bump
    is unchanged — still the post-tx, non-tx call ``_spy_coordinator``
    observes, at the very end. "modify" carries a THIRD bump (codex #638 R6
    P1): its payload re-embed REPLACES the object's vector, inside that
    replace's own transaction, between "embed" and "invalidate"."""
    notebook_id, object_ids, _relations = _seed_kg(repo, "node conflict phase")
    events = []
    _trace_transactions(repo, monkeypatch, events)
    _spy_coordinator(repo, monkeypatch, events)
    _spy_dirty_in_tx(repo, monkeypatch, events)
    _spy_embed_store(repo, monkeypatch, events)

    repo.apply_conflict_resolution(
        notebook_id,
        kind="node",
        left_ref=object_ids[0],
        right_ref=object_ids[1],
        resolution="discard",
        winner_ref=object_ids[0],
    )
    # status-only update_knowledge (no embed, no R6 P1 bump) + the second
    # dirty bump
    assert events == [
        "write.begin", "dirty", "write.commit", "invalidate", "dirty"
    ]

    events.clear()
    repo.apply_conflict_resolution(
        notebook_id,
        kind="node",
        left_ref=object_ids[0],
        right_ref=object_ids[1],
        resolution="modify",
        winner_ref=object_ids[0],
        resolved_payload={"name": "modified"},
    )
    # payload update_knowledge (embed, carrying its own R6 P1 replace bump)
    # + apply_conflict_resolution's belt-and-suspenders second bump
    assert events == [
        "write.begin", "dirty", "write.commit",           # object update
        "embed", "write.begin", "dirty", "write.commit",  # vector replace (R6 P1)
        "invalidate", "dirty",                             # belt-and-suspenders
    ]


def test_confirm_conflict_applies_mutation_before_candidate_status(
    repo, monkeypatch
):
    notebook_id, object_ids, _relations = _seed_kg(repo, "confirm conflict phase")
    candidate_id = repo.write_conflict_candidate(
        notebook_id,
        "node",
        object_ids[0],
        object_ids[1],
        resolution="modify",
        winner_ref=object_ids[0],
        resolved_payload=json.dumps({"name": "after"}),
    )
    events = []
    _trace_transactions(repo, monkeypatch, events)
    _spy_coordinator(repo, monkeypatch, events)
    _spy_dirty_in_tx(repo, monkeypatch, events)
    _spy_embed_store(repo, monkeypatch, events)
    governance = repo._runtime.governance
    original_status = governance.set_conflict_status

    def traced_status(db, nb, cid, status, now):
        events.append("candidate-status")
        return original_status(db, nb, cid, status, now)

    monkeypatch.setattr(governance, "set_conflict_status", traced_status)

    repo.confirm_conflict(notebook_id, candidate_id)

    # The KG mutation (now bumping inside its own transaction, codex #638 R5,
    # plus its own R6 P1 vector-replace bump) still commits fully before the
    # candidate-status transaction opens — that boundary is the point of
    # this test and is unchanged.
    assert events == [
        "write.begin", "dirty", "write.commit",           # object update
        "embed", "write.begin", "dirty", "write.commit",  # vector replace (R6 P1)
        "invalidate", "dirty",                             # belt-and-suspenders
        "write.begin", "candidate-status", "write.commit",
    ]


# ------------------------------------------------------------- exemptions


def test_unified_rebuild_rewrites_clusters_without_kg_mutation_seq_bump(
    repo, monkeypatch
):
    """The cluster-rebuild exemption: rebuild_unified_kg rewrites clusters and
    bumps cluster_mutation_seq (through the coordinator's in-transaction
    primitive) but must NEVER bump kg_mutation_seq — its idempotency gate
    (_cluster_input_version) depends on the seq being rebuild-stable."""
    notebook_id, _objects, _relations = _seed_kg(repo, "rebuild exemption")
    monkeypatch.setattr(
        cmr, "review_merge_candidates", lambda client, pending, **kwargs: []
    )
    seq_before = int(_state_row(repo, notebook_id)["kg_mutation_seq"])
    events = []
    _spy_coordinator(repo, monkeypatch, events)
    _spy_cluster_seq(repo, monkeypatch, events)

    repo.rebuild_unified_kg(notebook_id)

    assert "dirty" not in events
    assert "cluster_seq" in events
    state = _state_row(repo, notebook_id)
    assert int(state["kg_mutation_seq"]) == seq_before
    assert int(state["cluster_mutation_seq"]) >= 1


def test_deep_copy_never_calls_the_mutation_coordinator(repo, monkeypatch):
    """The deep-copy exemption (frozen matrix: copy_notebook has
    cache_invalidation/unified_dirty/version_bump all False): a deep copy
    preserves source versions and must not add dirty/version side effects."""
    notebook_id, _objects, _relations = _seed_kg(repo, "copy exemption")
    events = []
    _spy_coordinator(repo, monkeypatch, events)
    _spy_cluster_seq(repo, monkeypatch, events)

    copied = repo.copy_notebook(notebook_id, new_owner_id="user-local")

    assert copied.id != notebook_id
    assert events == []


def test_migration_and_fixture_writes_bypass_the_coordinator(repo, monkeypatch):
    """The migration/recovery/seed exemption: fresh construction (migrations +
    startup seed) performs its writes without the online coordinator — no
    unified_kg_state row exists until a real mutation. The `_test_insert_object`
    fixture write is part of the same exempt family."""
    with repo._connect() as db:
        n = db.execute("SELECT COUNT(*) AS c FROM unified_kg_state").fetchone()["c"]
    assert n == 0

    notebook = repo.create_notebook(NotebookCreate(name="fixture writes"))
    events = []
    _spy_coordinator(repo, monkeypatch, events)
    repo._test_insert_object(notebook.id, "claim", {"name": "fixture row"})
    assert events == []


# Task 5 review contracts are appended so frozen pre-existing line coordinates
# above remain unchanged.
def _trace_transaction_connections(repo, monkeypatch, events):
    import sqlite3
    runtime = object.__getattribute__(repo, "_runtime")
    original = runtime.database.write

    @contextmanager
    def traced():
        with original() as db:
            assert isinstance(db, sqlite3.Connection)
            events.append(("write.begin", id(db)))
            yield db
        events.append(("write.commit", id(db)))

    monkeypatch.setattr(runtime.database, "write", traced)


def _delegating_store_spy(monkeypatch, owner, name, events, *, cursor=False):
    import sqlite3
    original = getattr(owner, name)

    def wrapped(db, *args, **kwargs):
        assert isinstance(db, sqlite3.Connection)
        result = original(db, *args, **kwargs)
        if cursor:
            assert isinstance(result, sqlite3.Cursor)
        events.append((name, id(db), args))
        return result

    monkeypatch.setattr(owner, name, wrapped)


def test_delete_delegates_in_write_then_commits_before_cache_invalidation(repo, monkeypatch):
    notebook = getattr(repo, "create_notebook")(NotebookCreate(name="delete delegation"))
    runtime = object.__getattribute__(repo, "_runtime")
    events = []
    _trace_transaction_connections(repo, monkeypatch, events)
    _delegating_store_spy(
        monkeypatch, runtime.knowledge, "delete_notebook_graph_rows", events
    )
    monkeypatch.setattr(
        runtime.kg_mutations, "invalidate_unified_cache",
        lambda notebook_id: events.append(("invalidate", notebook_id)),
    )

    counts = getattr(repo, "delete_notebook_kg")(notebook.id)

    assert [event[0] for event in events] == [
        "write.begin", "delete_notebook_graph_rows", "write.commit", "invalidate",
    ]
    assert events[0][1] == events[1][1] == events[2][1]
    assert counts["knowledge_objects"] == 0


def test_relink_store_spies_observe_read_projection_and_caller_write_connection(repo, monkeypatch):
    notebook_id, object_ids, _relations = _seed_kg(repo, "relink delegation")
    runtime = object.__getattribute__(repo, "_runtime")
    monkeypatch.setattr(
        kg_relink, "complete_isolated_edges",
        lambda nodes, edges: [{
            "source_object_id": object_ids[0], "target_object_id": object_ids[2],
            "edge_type": "mentions", "basis": "delegation",
        }],
    )
    events = []
    for reader in (
        "relink_orphan_source_ids",
        "relink_source_page",
        "relink_object_rows_for_source",
        "relink_relation_rows_for_objects",
        "relink_source_is_live",
    ):
        _delegating_store_spy(monkeypatch, runtime.knowledge, reader, events)
    # The whole-notebook reader is reference-only now; touching it here is the
    # regression this ordering test would otherwise hide.
    _delegating_store_spy(monkeypatch, runtime.knowledge, "relink_rows", events)
    _trace_transaction_connections(repo, monkeypatch, events)
    _delegating_store_spy(monkeypatch, runtime.knowledge, "insert_relation_chunk", events)
    monkeypatch.setattr(
        runtime.kg_mutations, "mark_unified_kg_dirty_in_tx",
        lambda connection, notebook_id: events.append(("dirty", id(connection))),
    )
    monkeypatch.setattr(
        runtime.kg_mutations, "invalidate_unified_cache",
        lambda notebook_id: events.append(("invalidate", notebook_id)),
    )

    out = getattr(repo, "relink_notebook_kg")(notebook_id)

    # _seed_kg stores with source_id None and creates no `sources` rows, so the
    # notebook has exactly one partition — the orphan '' one. Orphans are
    # discovered first (one query) and processed after the — here empty — source
    # keyset page, giving one work unit and one write. `relink_source_is_live` is
    # lazy: it only runs because this run actually has an edge to write. Reads all
    # happen OUTSIDE the write transaction. The dirty bump now rides the SAME
    # write transaction as the edge insert (P1 fix: a `finally`-keyed bump
    # cannot survive a kill -9 landing right after this source's commit), so it
    # lands BEFORE write.commit; invalidate stays the run-level `finally` call,
    # after the commit.
    assert [event[0] for event in events] == [
        "relink_orphan_source_ids",
        "relink_source_page",
        "relink_object_rows_for_source",
        "relink_relation_rows_for_objects",
        "relink_source_is_live",
        "write.begin", "insert_relation_chunk", "dirty", "write.commit",
        "invalidate",
    ]
    assert events[5][1] == events[6][1] == events[7][1] == events[8][1]
    assert out["edges_added"] == 1


def test_canonical_relation_stream_and_rewrite_delegate_without_materializing(repo, monkeypatch):
    notebook_id, _objects, _relations = _seed_kg(repo, "canonical delegation")
    runtime = object.__getattribute__(repo, "_runtime")
    events = []
    _delegating_store_spy(
        monkeypatch, runtime.unified_kg,
        "canonical_relation_seed_rows", events, cursor=True,
    )
    _trace_transaction_connections(repo, monkeypatch, events)
    _delegating_store_spy(
        monkeypatch, runtime.unified_kg, "replace_canonical_relations", events
    )

    count = getattr(repo, "rebuild_canonical_relations")(notebook_id, force=True)

    assert [event[0] for event in events] == [
        "canonical_relation_seed_rows", "write.begin",
        "replace_canonical_relations", "write.commit",
    ]
    assert events[1][1] == events[2][1] == events[3][1]
    assert count == 2
