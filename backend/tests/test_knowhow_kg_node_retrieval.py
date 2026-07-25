# backend/tests/test_knowhow_kg_node_retrieval.py
"""Knowhow KG-node retrieval (default-on, env-reversible through
``KNOWHOW_KG_NODE_RETRIEVAL_ENABLED``).

Proves the two retrieval seams that let a knowhow cell KO (``object_type =
列名``) surface in the KG-node scoring path (``_retrieve_scored``):

  * gate i — type widening (``_scored_types`` / ``_knowhow_object_types``):
    flag-off (or a non-knowhow notebook) is BYTE-IDENTICAL to the historical
    ``_KG_TYPES`` filter; flag-on + knowhow present unions the column-name
    types in.
  * gate 0 — the chunk-reverse-lookup sidecar (``_knowhow_ko_candidates`` →
    ``kg_node_bridge``): a query is scored against ONLY the hidden knowhow
    source's chunk vectors and each hit maps back to the projected cell-KO id,
    giving structural-only KOs (no ``knowledge_embeddings`` row) a semantic
    score.

Reuses ``test_knowhow_retrieval.py``'s real import -> deterministic
projection -> retrieval fixtures (FakeEmbedder dim=16, real column-name KOs).
Everything is driven through the CandidateRetrievalService instance that
actually owns these methods (``client._repo.retrieval.candidates``) — the
facade only delegates — so the tests exercise the implementation directly.
"""
from __future__ import annotations

import json
import time

from app.services.knowhow import textops
from app.services.knowhow.projection import _cell_ko_id
from app.services.retrieval import _payload_text
from app.services.retrieval_candidates import _KG_TYPES

# The real projection->retrieval fixtures. ``client``/``imported`` are pytest
# fixtures reused by importing them into this module's namespace; the helpers/
# constants are plain reuse.
from tests.test_knowhow_retrieval import (  # noqa: F401  (client/imported are fixtures)
    DATA_ROWS,
    HEADER,
    UNIQUE_TERM,
    _login,
    _mk_notebook,
    client,
    imported,
)

# The column whose 过冲 row cell carries UNIQUE_TERM (DATA_ROWS[0][3]).
FIX_COLUMN = "修复方法"
FIX_CELL_TEXT = DATA_ROWS[0][3]


def _candidates(client):
    """The real ``CandidateRetrievalService`` backing this app's repo — the
    layer that OWNS ``_retrieve_scored`` / ``_scored_types`` / ``_knowhow_*``
    (the facade just delegates to it). Driving it directly tests the real
    implementation and keeps these tests off the facade's surface scan."""
    return client._repo.retrieval.candidates


def _await_projection(cand, notebook_id: str, timeout: float = 5.0) -> None:
    """Wait until every column-name KO has landed. ``project_table`` flips each
    row's ``projection_status`` to 'synced' BEFORE its final KO write +
    ``kg_mutation_seq`` bump, so the ``imported`` fixture (which polls row
    status) can return a hair before the KOs exist — a window that widens
    enough to observe under parallel (xdist) test load. Poll the raw KO table
    (the write is one atomic batch) until all column types are present, then
    drop the seq-gated counts memo so ``_knowhow_object_types`` recomputes from
    the now-complete table regardless of the mutation-seq bump timing."""
    from app.repositories.sqlite import knowledge_counts_cache

    want = set(HEADER)
    got: set = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        with cand._connect() as db:
            got = {
                r["object_type"]
                for r in db.execute(
                    "SELECT DISTINCT object_type FROM knowledge_objects WHERE notebook_id=?",
                    (notebook_id,),
                ).fetchall()
            }
        if want <= got:
            knowledge_counts_cache.invalidate(notebook_id)
            return
        time.sleep(0.02)
    raise AssertionError(f"projection KOs never settled; missing {want - got}")


def _fix_ko_id(cand, notebook_id: str) -> str:
    """The projected 修复方法 KO id for the UNIQUE_TERM cell, read straight from
    the DB so assertions bind to the REAL projected id, not a recomputation."""
    _await_projection(cand, notebook_id)
    with cand._connect() as db:
        row = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? AND object_type=? "
            "AND payload LIKE ?",
            (notebook_id, FIX_COLUMN, f"%{UNIQUE_TERM}%"),
        ).fetchone()
    assert row is not None, "sanity: the UNIQUE_TERM 修复方法 KO must exist post-projection"
    return row["id"]


# --------------------------------------------------------------------------
# flag plumbing + gate i (type widening)
# --------------------------------------------------------------------------


def test_flag_defaults_on(client):
    assert client._repo.settings.knowhow_kg_node_retrieval_enabled is True


def test_flag_env_override_can_disable(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("KNOWHOW_KG_NODE_RETRIEVAL_ENABLED", "false")
    assert Settings().knowhow_kg_node_retrieval_enabled is False


def test_knowhow_object_types_lists_column_names(client, imported):
    cand = _candidates(client)
    _await_projection(cand, imported["nb"])
    # Every column of the projected table is its own dynamic object_type.
    assert set(cand._knowhow_object_types(imported["nb"])) == set(HEADER)


def test_scored_types_byte_identical_when_flag_off(client, imported):
    """FLAG-OFF INVARIANT (test-enforced): type_list is byte-identical to the
    historical ``[t for t in (types or _KG_TYPES) if t in _KG_TYPES]`` line —
    even on a knowhow notebook, column-name types are dropped."""
    cand = _candidates(client)
    nb = imported["nb"]
    previous = cand.settings.knowhow_kg_node_retrieval_enabled
    cand.settings.knowhow_kg_node_retrieval_enabled = False
    try:
        assert cand._scored_types(nb, None) == list(_KG_TYPES)
        assert cand._scored_types(nb, ["procedure", FIX_COLUMN]) == ["procedure"]
        assert cand._scored_types(nb, [FIX_COLUMN]) == []
    finally:
        cand.settings.knowhow_kg_node_retrieval_enabled = previous


def test_scored_types_widens_when_flag_on(client, imported):
    cand = _candidates(client)
    nb = imported["nb"]
    _await_projection(cand, nb)
    previous = cand.settings.knowhow_kg_node_retrieval_enabled
    cand.settings.knowhow_kg_node_retrieval_enabled = True
    try:
        types = cand._scored_types(nb, None)
        assert set(_KG_TYPES) <= set(types)     # the 4 KG types still there
        assert set(HEADER) <= set(types)        # every column-name type unioned in
        # a caller can now explicitly reach a column-name type
        assert cand._scored_types(nb, [FIX_COLUMN]) == [FIX_COLUMN]
    finally:
        cand.settings.knowhow_kg_node_retrieval_enabled = previous


def test_non_knowhow_notebook_unaffected_when_flag_on(client):
    """Flag-on but no knowhow content ⇒ no widening (type_list unchanged),
    the empty-tuple short-circuit keeps a plain notebook a pure no-op."""
    headers = _login(client, "a00000602")
    nb = _mk_notebook(client, headers)
    cand = _candidates(client)
    previous = cand.settings.knowhow_kg_node_retrieval_enabled
    cand.settings.knowhow_kg_node_retrieval_enabled = True
    try:
        assert cand._knowhow_object_types(nb) == ()
        assert cand._scored_types(nb, None) == list(_KG_TYPES)
        assert cand._scored_types(nb, ["claim"]) == ["claim"]
    finally:
        cand.settings.knowhow_kg_node_retrieval_enabled = previous


def test_unrelated_custom_schema_type_is_not_treated_as_knowhow(client):
    """Default-on widening is source-scoped, not "every non-built-in type"."""
    from app.models.schemas import NotebookCreate

    cand = _candidates(client)
    nb = client._repo.create_notebook(NotebookCreate(name="custom-schema"))
    client._repo.store_kg(nb.id, None, [{
        "local_id": "X1",
        "object_type": "custom_metric",
        "payload": {"name": "custom only"},
        "evidence": [],
    }], [])

    assert cand.settings.knowhow_kg_node_retrieval_enabled is True
    assert cand._knowhow_object_types(nb.id) == ()
    assert cand._scored_types(nb.id, None) == list(_KG_TYPES)
    assert cand._scored_types(nb.id, ["custom_metric"]) == []


# --------------------------------------------------------------------------
# gate 0 bridge (chunk-reverse-lookup sidecar)
# --------------------------------------------------------------------------


def test_bridge_maps_query_hit_to_projected_ko_id(client, imported):
    cand = _candidates(client)
    nb = imported["nb"]
    expected = _fix_ko_id(cand, nb)
    # The bridge must reproduce the projector's id byte-for-byte from the cell
    # element's own text + metadata.
    assert expected == _cell_ko_id(
        imported["table_id"], FIX_COLUMN, textops.value_key(FIX_CELL_TEXT))

    query_vector = cand._embed_query(UNIQUE_TERM)
    assert query_vector is not None
    with cand._connect() as db:
        kh = cand._knowhow_ko_candidates(db, nb, UNIQUE_TERM, query_vector)
    assert expected in kh, sorted(kh)
    assert isinstance(kh[expected], float)
    # every key is a knowhow cell-KO id (the structural-only projection prefix)
    assert kh and all(k.startswith("ko-kh-") for k in kh)


def test_bridge_empty_without_query_vector(client, imported):
    cand = _candidates(client)
    with cand._connect() as db:
        assert cand._knowhow_ko_candidates(db, imported["nb"], UNIQUE_TERM, None) == {}


def test_bridge_caches_scoped_corpus_and_reloads_after_kg_invalidation(
    client, imported, monkeypatch,
):
    """Many reasoning subqueries reuse one matrix; a KG mutation evicts it."""
    cand = _candidates(client)
    nb = imported["nb"]
    _await_projection(cand, nb)
    cand._vector_cache.invalidate(f"{nb}:knowhow_bridge")
    calls = 0
    original = cand.chunks.knowhow_chunk_rows

    def counted(db, notebook_id):
        nonlocal calls
        calls += 1
        return original(db, notebook_id)

    monkeypatch.setattr(cand.chunks, "knowhow_chunk_rows", counted)
    query_vector = cand._embed_query(UNIQUE_TERM)
    with cand._connect() as db:
        first = cand._knowhow_ko_candidates(db, nb, UNIQUE_TERM, query_vector)
        second = cand._knowhow_ko_candidates(db, nb, UNIQUE_TERM, query_vector)
    assert first == second
    assert calls == 1

    cand.snapshots.invalidate_kg(nb)
    with cand._connect() as db:
        third = cand._knowhow_ko_candidates(db, nb, UNIQUE_TERM, query_vector)
    assert third == first
    assert calls == 2


def test_bridge_reloads_after_vector_only_backfill(client, imported, monkeypatch):
    """H4 repair changes embeddings without a KG-sequence bump."""
    cand = _candidates(client)
    nb = imported["nb"]
    _await_projection(cand, nb)
    cand._vector_cache.invalidate(f"{nb}:knowhow_bridge")
    with cand._connect() as db:
        vector_row = db.execute(
            "SELECT ce.chunk_id,ce.vector,ce.created_at FROM chunk_embeddings ce "
            "JOIN chunks c ON c.id=ce.chunk_id "
            "JOIN knowhow_tables kt ON kt.hidden_source_id=c.source_id "
            "WHERE kt.notebook_id=? LIMIT 1",
            (nb,),
        ).fetchone()
    assert vector_row is not None
    with cand.database.write() as db:
        db.execute(
            "DELETE FROM chunk_embeddings WHERE chunk_id=?",
            (vector_row["chunk_id"],),
        )

    calls = 0
    original = cand.chunks.knowhow_chunk_rows

    def counted(db, notebook_id):
        nonlocal calls
        calls += 1
        return original(db, notebook_id)

    monkeypatch.setattr(cand.chunks, "knowhow_chunk_rows", counted)
    query_vector = cand._embed_query(UNIQUE_TERM)
    with cand._connect() as db:
        cand._knowhow_ko_candidates(db, nb, UNIQUE_TERM, query_vector)
    assert calls == 1

    # Mirror vector-only backfill: insert the missing vector without touching
    # unified_kg_state or calling invalidate_kg.
    with cand.database.write() as db:
        db.execute(
            "INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)",
            (vector_row["chunk_id"], nb, vector_row["vector"], vector_row["created_at"]),
        )
    with cand._connect() as db:
        cand._knowhow_ko_candidates(db, nb, UNIQUE_TERM, query_vector)
    assert calls == 2


# --------------------------------------------------------------------------
# gate 0 + gate i together, in _retrieve_scored
# --------------------------------------------------------------------------


def test_retrieve_scored_surfaces_knowhow_ko_full_scan(client, imported):
    """Full-scan path (the default for a small/copyable knowhow notebook):
    flag-on ⇒ the column-name KO surfaces; flag-off ⇒ it does not."""
    cand = _candidates(client)
    nb = imported["nb"]
    expected = _fix_ko_id(cand, nb)

    on = cand._retrieve_scored(nb, UNIQUE_TERM)
    previous = cand.settings.knowhow_kg_node_retrieval_enabled
    cand.settings.knowhow_kg_node_retrieval_enabled = False
    try:
        off = cand._retrieve_scored(nb, UNIQUE_TERM)
    finally:
        cand.settings.knowhow_kg_node_retrieval_enabled = previous
    hit = next((h for h in on if h.object_id == expected), None)
    assert hit is not None, [(h.object_type, h.object_id) for h in on]
    assert hit.object_type == FIX_COLUMN
    assert UNIQUE_TERM in _payload_text(hit.payload)
    assert not any(h.object_type in set(HEADER) for h in off)
    assert expected not in {h.object_id for h in off}


def test_retrieve_scored_surfaces_knowhow_ko_bounded_fts_fallback(client, imported, monkeypatch):
    """Bounded path (cand_sims is not None): force the not-copyable FTS-fallback
    branch. Knowhow KOs are structural-only (never in kg_objects_fts), so seed a
    base KG object into kg_objects_fts to make the lexical candidate set
    non-empty — gate 0 then unions the knowhow KO into cand_sims so it is
    fetched + scored. flag-off on the same bounded path must NOT surface it."""
    cand = _candidates(client)
    nb = imported["nb"]
    expected = _fix_ko_id(cand, nb)

    base_oid = "ko-base-fts-probe"
    with cand.database.write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id, notebook_id, object_type, status, "
            "owner, payload, evidence, source_candidate_id, source_id, created_at, updated_at) "
            "VALUES (?,?,?,?,'',?,'[]',NULL,'',?,?)",
            (base_oid, nb, "concept", "approved",
             json.dumps({"name": f"{UNIQUE_TERM} baseline"}, ensure_ascii=False),
             "2026-01-01", "2026-01-01"),
        )
        db.execute(
            "INSERT INTO kg_objects_fts(object_id, notebook_id, name) VALUES (?,?,?)",
            (base_oid, nb, f"{UNIQUE_TERM} baseline"),
        )
    # Force the not-copyable branch so _retrieve_scored takes the bounded
    # FTS-fallback path (cand_sims from lexical hits) rather than a full scan.
    monkeypatch.setattr(cand, "notebook_copy_stats",
                        lambda notebook_id: {"copyable": False})

    on = cand._retrieve_scored(nb, UNIQUE_TERM)
    ids_on = {h.object_id for h in on}
    assert base_oid in ids_on, "sanity: the bounded FTS-fallback path must have fired"
    assert expected in ids_on, [(h.object_type, h.object_id) for h in on]

    previous = cand.settings.knowhow_kg_node_retrieval_enabled
    cand.settings.knowhow_kg_node_retrieval_enabled = False
    try:
        off = cand._retrieve_scored(nb, UNIQUE_TERM)  # monkeypatch still active
    finally:
        cand.settings.knowhow_kg_node_retrieval_enabled = previous
    ids_off = {h.object_id for h in off}
    assert base_oid in ids_off                     # base still there (same bounded path)
    assert expected not in ids_off                 # knowhow KO gone with flag off


# --------------------------------------------------------------------------
# flag-off byte-identity of the widened _rrf_scored loop (review hardening)
# --------------------------------------------------------------------------
def test_rrf_tie_break_ordering_follows_kg_types_not_caller_order(client):
    """Regression guard for the widened ``_rrf_scored`` loop. Gate i lets the
    reasoning planner hand ``_retrieve_scored`` a ``types`` list in the LLM's
    order, and RRF resolves a genuine score tie by ``docs`` insertion order =
    loop order. The loop MUST iterate the fixed ``_KG_TYPES`` sequence first (not
    the caller's order) so tie-breaks stay byte-identical to the pre-feature
    code — reordering ``types`` must not reorder equally-scored hits. Two
    identical-payload doc KOs of different fixed types force that tie; both
    reordered calls must agree and be ``_KG_TYPES``-ordered."""
    from app.models.schemas import NotebookCreate

    cand = _candidates(client)
    nb = client._repo.create_notebook(NotebookCreate(name="rrf-tie-order"))
    payload = {"name": "时序收敛要点", "section_path": "1"}  # identical ⇒ identical score
    client._repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim", "payload": dict(payload), "evidence": []},
        {"local_id": "K1", "object_type": "concept", "payload": dict(payload), "evidence": []},
    ], [])
    previous_flag = cand.settings.knowhow_kg_node_retrieval_enabled
    prev = cand.settings.retrieval_rrf_enabled
    cand.settings.knowhow_kg_node_retrieval_enabled = False
    cand.settings.retrieval_rrf_enabled = True
    try:
        fwd = [h.object_type for h in cand._retrieve_scored(
            nb.id, "时序收敛要点", types=["claim", "concept"])]
        rev = [h.object_type for h in cand._retrieve_scored(
            nb.id, "时序收敛要点", types=["concept", "claim"])]
    finally:
        cand.settings.retrieval_rrf_enabled = prev
        cand.settings.knowhow_kg_node_retrieval_enabled = previous_flag
    # Pre-fix (`for t in kg_objs`) leaked caller order → fwd=[claim,concept],
    # rev=[concept,claim]. Post-fix both are _KG_TYPES-ordered (claim<concept).
    assert fwd == rev, f"tie-break order leaked caller `types` order: {fwd} vs {rev}"
    assert fwd == ["claim", "concept"], fwd
