"""knowhow-tables PR-2+3 Task 2: KnowhowProjector — the cell-level node model
(design doc §④/§①, docs/superpowers/specs/2026-07-15-knowhow-tables-design.md)
replacing PR-1's fixed case/procedure/tool vocabulary end to end. Real SQLite
(via a full SQLiteRepository, mirroring test_knowhow_store.py's fixture
convention) + a fake embedder that records call counts/texts (mirroring
test_ask_embed_cache.py's fixture convention).

``project_table`` is now the SOLE projection entry point (``project_row`` is
gone — see projection.py's module docstring for why cross-row merging forces
a whole-table view). The per-cell chunk-diff/embed-call-count/self-heal
machinery ported from PR-1 is UNCHANGED in spirit — only renamed call sites
(``project_row(table_id, row_id)`` -> ``project_table(table_id)``) and the
`concept` -> `row_title` vocabulary shift.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.knowhow.projection import KnowhowProjector, find_legacy_projected_table_ids
from app.services.sqlite_repository import SQLiteRepository


class _FakeEmbedder:
    """Records every embed_texts call (as a list of input texts) so tests can
    assert exactly which chunks got (re)embedded and how many batch calls
    fired. ``fail_next`` makes the NEXT call raise, to exercise the
    embedding-failure path without needing a real flaky network."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_next = False

    def embed_texts(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("boom")
        return [[0.1, 0.2, 0.3] for _ in texts]

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def repo_factory(tmp_path, monkeypatch):
    """Mirrors test_ask_embed_cache.py's repo_factory: four EMBED_* env vars
    make Settings.embedder_configured (a read-only property) true, since it
    cannot be set directly on an already-constructed Settings instance."""

    def _make():
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
        monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
        monkeypatch.setenv("EMBED_API_KEY", "test-key")
        monkeypatch.setenv("EMBED_MODEL", "test-model")
        return SQLiteRepository(Settings())

    return _make


@pytest.fixture
def repo(repo_factory):
    return repo_factory()


@pytest.fixture
def embedder(repo) -> _FakeEmbedder:
    emb = _FakeEmbedder()
    repo.embedder = emb
    assert repo.settings.embedder_configured  # confirm the real embed path, not an early-return no-op
    return emb


@pytest.fixture
def projector(repo) -> KnowhowProjector:
    rt = repo._runtime
    return KnowhowProjector(
        settings=repo.settings,
        database=rt.database,
        knowhow=rt.knowhow_store,
        sources=rt.source_store,
        chunks=rt.chunk_store,
        knowledge=rt.knowledge,
        embedding=rt.source_embedding,
        note_model_error=rt.models.note_model_error,
        invalidate_unified_cache=rt.kg_mutations.invalidate_unified_cache,
        mark_unified_dirty=rt.kg_mutations.mark_unified_kg_dirty,
        new_id=rt.seams.new_id,
        now=rt.seams.now,
    )


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id


COLUMNS = [
    {"name": "违例类型", "role": "anchor"},
    {"name": "现象识别", "role": "procedure"},
    {"name": "根因分析", "role": "procedure"},
    {"name": "修复方法", "role": "procedure"},
    {"name": "依赖工具", "role": "entity"},
]


@pytest.fixture
def table_id(repo, notebook_id) -> str:
    return repo._runtime.knowhow_store.create_knowhow_table(
        notebook_id, "时序修复", "desc", COLUMNS
    )


def _cols_by_name(repo, table_id: str) -> dict[str, str]:
    detail = repo._runtime.knowhow_store.get_knowhow_table(table_id)
    return {c["name"]: c["id"] for c in detail["columns"]}


def _row_element_ids(repo, row_id: str) -> set[str]:
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id FROM source_elements WHERE json_extract(metadata,'$.knowhow.row_id')=?",
            (row_id,),
        ).fetchall()
    return {r["id"] for r in rows}


def _row_chunk_ids(repo, row_id: str) -> set[str]:
    element_ids = _row_element_ids(repo, row_id)
    if not element_ids:
        return set()
    with repo._connect() as db:
        rows = db.execute("SELECT id, element_ids FROM chunks").fetchall()
    out = set()
    for r in rows:
        eids = json.loads(r["element_ids"] or "[]")
        if any(eid in element_ids for eid in eids):
            out.add(r["id"])
    return out


def _row_chunk_texts(repo, row_id: str) -> dict[str, str]:
    """{chunk_id: text} for this row's chunks. Chunk ids are stable per
    (row, column-position) slot regardless of content (an edit rewrites the
    row in place, it does not mint a new id) — tests that want to see WHICH
    cell's content changed compare this dict, not the id set."""
    element_ids = _row_element_ids(repo, row_id)
    if not element_ids:
        return {}
    with repo._connect() as db:
        rows = db.execute("SELECT id, text, element_ids FROM chunks").fetchall()
    out: dict[str, str] = {}
    for r in rows:
        eids = json.loads(r["element_ids"] or "[]")
        if any(eid in element_ids for eid in eids):
            out[r["id"]] = r["text"]
    return out


def _row_chunk_section_paths(repo, row_id: str) -> dict[str, str]:
    """{chunk_id: section_path} for this row's chunks — companion to
    _row_chunk_texts for tests that need to see the human-readable
    "table › title › column" breadcrumb move without the chunk's text
    changing (row-title-cell edits move every sibling's section_path)."""
    element_ids = _row_element_ids(repo, row_id)
    if not element_ids:
        return {}
    with repo._connect() as db:
        rows = db.execute("SELECT id, section_path, element_ids FROM chunks").fetchall()
    out: dict[str, str] = {}
    for r in rows:
        eids = json.loads(r["element_ids"] or "[]")
        if any(eid in element_ids for eid in eids):
            out[r["id"]] = r["section_path"]
    return out


def _chunk_fts_ids(repo, chunk_ids) -> set[str]:
    ids = list(chunk_ids)
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    with repo._connect() as db:
        rows = db.execute(
            f"SELECT chunk_id FROM chunks_fts WHERE chunk_id IN ({placeholders})", ids,
        ).fetchall()
    return {r["chunk_id"] for r in rows}


def _chunk_embedding_vectors(repo, chunk_ids) -> dict[str, bytes]:
    """{chunk_id: raw vector blob} for whichever of chunk_ids currently have
    a chunk_embeddings row — existence (not just content) is the signal
    tests need: chunk_embeddings.chunk_id is ON DELETE CASCADE onto
    chunks(id), so a chunk row that gets deleted+reinserted (rather than
    updated in place) silently loses this row even if its id is reused."""
    ids = list(chunk_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with repo._connect() as db:
        rows = db.execute(
            f"SELECT chunk_id, vector FROM chunk_embeddings WHERE chunk_id IN ({placeholders})",
            ids,
        ).fetchall()
    return {r["chunk_id"]: r["vector"] for r in rows}


def _row_object_ids(repo, row_id: str) -> set[str]:
    """KO ids whose payload.rows LIST contains row_id (replaces PR-1's
    payload.row_id scalar lookup — a merged KO's payload.rows accumulates
    every contributing row, design doc §④ "evidence 累积各来源格")."""
    with repo._connect() as db:
        rows = db.execute(
            "SELECT DISTINCT ko.id FROM knowledge_objects ko, "
            "json_each(ko.payload, '$.rows') je WHERE je.value = ?",
            (row_id,),
        ).fetchall()
    return {r["id"] for r in rows}


def _table_object_type_counts(repo, table_id: str) -> dict[str, int]:
    with repo._connect() as db:
        rows = db.execute(
            "SELECT object_type, COUNT(*) AS n FROM knowledge_objects "
            "WHERE json_extract(payload,'$.table_id')=? GROUP BY object_type",
            (table_id,),
        ).fetchall()
    return {r["object_type"]: r["n"] for r in rows}


def _kos_by_type(repo, table_id: str, object_type: str) -> list[dict]:
    """Every KO of a given object_type (=column name) belonging to this
    table, with parsed payload/evidence — table-scoped via payload.table_id."""
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, payload, evidence FROM knowledge_objects "
            "WHERE object_type = ? AND json_extract(payload,'$.table_id') = ?",
            (object_type, table_id),
        ).fetchall()
    return [
        {"id": r["id"], "payload": json.loads(r["payload"]), "evidence": json.loads(r["evidence"])}
        for r in rows
    ]


def _ko_by_name(repo, table_id: str, object_type: str, name: str) -> dict:
    matches = [k for k in _kos_by_type(repo, table_id, object_type) if k["payload"]["name"] == name]
    assert len(matches) == 1, (name, matches)
    return matches[0]


def _edges_from(repo, source_ko_id: str) -> list[dict]:
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, target_object_id, edge_type FROM knowledge_relations "
            "WHERE source_object_id = ?", (source_ko_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _edges_to(repo, target_ko_id: str) -> list[dict]:
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, source_object_id, edge_type FROM knowledge_relations "
            "WHERE target_object_id = ?", (target_ko_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _row_projection_status(repo, table_id: str, row_id: str) -> str:
    detail = repo._runtime.knowhow_store.get_knowhow_table(table_id)
    row = next(r for r in detail["rows"] if r["id"] == row_id)
    return row["projection_status"]


def _insert_legacy_ko(repo, source_id: str, notebook_id: str, table_id: str,
                      object_type: str = "case", suffix: str = "1") -> str:
    """Simulate a PR-1-era KO surviving in storage (object_type one of the
    fixed legacy strings, id carrying knowhow's own ko-kh- prefix) — the
    exact shape find_legacy_projected_table_ids/reproject_legacy_tables must
    detect and replace."""
    ko_id = f"ko-kh-legacy{suffix}"
    now = "2020-01-01T00:00:00"
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id, notebook_id, object_type, status, owner, "
            "payload, evidence, source_candidate_id, source_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 'approved', '', ?, '[]', NULL, ?, ?, ?)",
            (ko_id, notebook_id, object_type,
             json.dumps({"table_id": table_id}), source_id, now, now),
        )
    return ko_id


# ---------------------------------------------------------------------------
# ensure_hidden_source
# ---------------------------------------------------------------------------


def test_ensure_hidden_source_is_idempotent(repo, projector, table_id):
    source_id_1 = projector.ensure_hidden_source(table_id)
    source_id_2 = projector.ensure_hidden_source(table_id)
    assert source_id_1 == source_id_2

    source = repo._runtime.source_store.get_source(source_id_1)
    assert source.type == "knowhow"
    assert source.title == "Knowhow 表：时序修复"
    assert source.parse_status == "parsed"


def test_hidden_source_excluded_from_source_listing(repo, projector, table_id, notebook_id):
    source_id = projector.ensure_hidden_source(table_id)

    listed_ids = {s.id for s in repo._runtime.source_store.list_sources(notebook_id)}
    assert source_id not in listed_ids

    page = repo._runtime.source_store.list_sources_page(notebook_id)
    assert source_id not in {item.id for item in page.items}


# ---------------------------------------------------------------------------
# project_table: idempotency + id prefixes
# ---------------------------------------------------------------------------


def test_project_table_twice_produces_identical_id_sets(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题",
        cols["现象识别"]: "- 观察上升沿过冲\n- 测量峰值电压",
        cols["根因分析"]: "1. 检查电源阻抗\n2. 排查寄生电感",
        cols["修复方法"]: "1. 增加阻尼电阻\n2. 调整走线",
        cols["依赖工具"]: "- 示波器\n- 万用表",
    })

    projector.project_table(table_id)
    elements_1 = _row_element_ids(repo, row_id)
    chunks_1 = _row_chunk_ids(repo, row_id)
    objects_1 = _row_object_ids(repo, row_id)

    projector.project_table(table_id)
    elements_2 = _row_element_ids(repo, row_id)
    chunks_2 = _row_chunk_ids(repo, row_id)
    objects_2 = _row_object_ids(repo, row_id)

    assert elements_1 and elements_1 == elements_2
    assert chunks_1 and chunks_1 == chunks_2
    assert objects_1 and objects_1 == objects_2


def test_derived_ids_use_the_contracted_prefixes(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "问题A", cols["现象识别"]: "现象说明", cols["依赖工具"]: "示波器",
    })
    projector.project_table(table_id)
    source_id = projector.ensure_hidden_source(table_id)

    with repo._connect() as db:
        object_ids = [r["id"] for r in db.execute(
            "SELECT id FROM knowledge_objects WHERE source_id=?", (source_id,)
        ).fetchall()]
        chunk_ids = [r["id"] for r in db.execute(
            "SELECT id FROM chunks WHERE source_id=?", (source_id,)
        ).fetchall()]
        relation_ids = [r["id"] for r in db.execute(
            "SELECT id FROM knowledge_relations WHERE source_id=?", (source_id,)
        ).fetchall()]
    element_ids = list(_row_element_ids(repo, row_id))

    assert object_ids and all(oid.startswith("ko-kh-") for oid in object_ids)
    assert element_ids and all(eid.startswith("el-kh-") for eid in element_ids)
    assert chunk_ids and all(cid.startswith("chunk-kh-") for cid in chunk_ids)
    assert relation_ids and all(rid.startswith("kr-kh-") for rid in relation_ids)


# ---------------------------------------------------------------------------
# project_table: incremental chunk diff + embedding call counts (PR-1
# machinery, preserved verbatim under the new whole-table entry point)
# ---------------------------------------------------------------------------


def test_single_cell_edit_rebuilds_only_that_chunk_and_embeds_once(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题",
        cols["现象识别"]: "- 观察上升沿过冲",
        cols["修复方法"]: "1. 增加阻尼电阻",
    })

    projector.project_table(table_id)
    assert embedder.call_count == 1
    chunks_before = _row_chunk_texts(repo, row_id)
    assert len(chunks_before) == 3  # anchor + identify + fix, one chunk each

    store.update_knowhow_cell(row_id, cols["修复方法"], "1. 更换更大电容")
    projector.project_table(table_id)

    assert embedder.call_count == 2
    # ONLY the changed cell's net text (chunk text is the cell's whole net
    # text, list marker intact — parse_steps is what strips markers, and
    # that only feeds the procedure KO's steps[], not the chunk/element text).
    assert embedder.calls[-1] == ["1. 更换更大电容"]

    chunks_after = _row_chunk_texts(repo, row_id)
    # Chunk ids are STABLE per (row, column-position) slot regardless of
    # content — editing a cell rewrites its chunk's content in place, it does
    # not mint a new id. So the id SET is unchanged; only one id's TEXT
    # differs (the edited cell's), proving the other two were left untouched.
    assert set(chunks_before) == set(chunks_after)
    changed_ids = {cid for cid in chunks_before if chunks_before[cid] != chunks_after[cid]}
    assert len(changed_ids) == 1
    assert chunks_after[next(iter(changed_ids))] == "1. 更换更大电容"


def test_reprojecting_unchanged_table_makes_zero_additional_embed_calls(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题",
        cols["修复方法"]: "1. 增加阻尼电阻",
    })

    projector.project_table(table_id)
    assert embedder.call_count == 1

    projector.project_table(table_id)
    assert embedder.call_count == 1  # nothing changed -> zero additional embed calls


def test_reproject_self_heals_a_missing_chunk_vector(repo, projector, table_id, embedder):
    """Review hardening (ported from PR-1): the carry-over window (structural
    transaction committed, vector re-persist did NOT happen — process death
    or replace_chunk_vectors raising in between) used to leave a
    text-unchanged chunk vector-less FOREVER: the next project_table saw
    ``old_specs == new_specs`` and skipped the cell entirely, and nothing
    else ever re-embeds a knowhow chunk. project_table must probe its
    text-unchanged chunks' vectors and re-embed exactly the missing ones —
    making a reproject a real recovery path, at zero embed calls (one
    id-keyed SELECT) in the all-present steady state (pinned by
    test_reprojecting_unchanged_table_makes_zero_additional_embed_calls
    above)."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题",
        cols["现象识别"]: "- 观察上升沿过冲",
        cols["修复方法"]: "1. 增加阻尼电阻",
    })

    projector.project_table(table_id)
    assert embedder.call_count == 1
    chunks = _row_chunk_texts(repo, row_id)
    assert len(chunks) == 3
    target_id = next(cid for cid, text in chunks.items() if text == "- 观察上升沿过冲")
    other_ids = set(chunks) - {target_id}

    # Simulate the interrupted gap: vector row gone, chunk row fully intact.
    with repo._connect() as db:
        db.execute("DELETE FROM chunk_embeddings WHERE chunk_id=?", (target_id,))
    assert _chunk_embedding_vectors(repo, {target_id}) == {}
    vectors_before = _chunk_embedding_vectors(repo, other_ids)
    assert set(vectors_before) == other_ids

    projector.project_table(table_id)  # NO cell edits

    # Exactly one extra embed call, carrying exactly the vector-less chunk's
    # current text — the two chunks that still had vectors were not re-sent.
    assert embedder.call_count == 2
    assert embedder.calls[-1] == ["- 观察上升沿过冲"]
    assert set(_chunk_embedding_vectors(repo, {target_id})) == {target_id}
    assert _chunk_embedding_vectors(repo, other_ids) == vectors_before
    assert _row_projection_status(repo, table_id, row_id) == "synced"


def test_anchor_cell_edit_moves_sibling_section_paths_without_reembedding(
    repo, projector, table_id, embedder
):
    """Review finding (ported from PR-1, now against the anchor/row-title
    cell): section_path bakes in the row's title (`f"{table} › {row_title} ›
    {column}"`), so editing ONLY the anchor cell changes every SIBLING
    chunk's section_path too even though their text never changed. The chunk
    row must still be rewritten (section_path has to move) but must NOT be
    re-embedded — and (chunk_embeddings.chunk_id is ON DELETE CASCADE onto
    chunks(id), and this chunk's id is stable for the same row/column/split)
    its existing embedding must survive, not get cascade-deleted by a
    delete+reinsert that nothing then re-embeds. This is also the exact
    machinery class that must carry the global "concept -> row_title"
    section_path formula change safely across a legacy-table reproject
    (design doc §① migration note) — a text-unchanged cell must never pay
    for a re-embed just because the SURROUNDING title-resolution logic
    changed, only because the section_path VALUE happened to move."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题",
        cols["现象识别"]: "- 观察上升沿过冲",
        cols["修复方法"]: "1. 增加阻尼电阻",
    })

    projector.project_table(table_id)
    assert embedder.call_count == 1
    chunks_before = _row_chunk_texts(repo, row_id)
    assert len(chunks_before) == 3
    paths_before = _row_chunk_section_paths(repo, row_id)
    vectors_before = _chunk_embedding_vectors(repo, set(chunks_before))
    assert set(vectors_before) == set(chunks_before)  # all 3 embedded on first projection

    anchor_chunk_id = next(cid for cid, text in chunks_before.items() if text == "过冲问题")

    store.update_knowhow_cell(row_id, cols["违例类型"], "过冲新问题")
    projector.project_table(table_id)

    # Exactly 1 new embed call, for exactly the anchor cell's OWN new text —
    # zero re-embeds for the two sibling cells whose text never changed.
    assert embedder.call_count == 2
    assert embedder.calls[-1] == ["过冲新问题"]

    chunks_after = _row_chunk_texts(repo, row_id)
    paths_after = _row_chunk_section_paths(repo, row_id)
    assert set(chunks_after) == set(chunks_before)  # same 3 stable chunk-id slots

    changed_text_ids = {cid for cid in chunks_before if chunks_before[cid] != chunks_after[cid]}
    assert changed_text_ids == {anchor_chunk_id}
    assert chunks_after[anchor_chunk_id] == "过冲新问题"
    sibling_ids = set(chunks_before) - {anchor_chunk_id}

    # ALL 3 chunks' section_path reflects the NEW title — including the two
    # siblings whose own text is untouched.
    for cid in chunks_after:
        assert "过冲新问题" in paths_after[cid]
        assert paths_after[cid] != paths_before[cid]

    # The siblings' embeddings are the SAME rows as before: still present
    # (not cascade-deleted) and byte-identical (reused, not recomputed).
    vectors_after = _chunk_embedding_vectors(repo, sibling_ids)
    assert set(vectors_after) == sibling_ids
    assert vectors_after == {cid: vectors_before[cid] for cid in sibling_ids}


def test_project_table_sweeps_chunks_for_a_column_removed_from_the_table(
    repo, projector, table_id, embedder
):
    """Review finding (ported from PR-1): _write_chunks only ever walked the
    table's CURRENT columns, so a column removed from the table left its old
    chunk group (+chunks_fts +chunk_embeddings) behind forever, retrievable
    even though the column is gone. project_table alone must be self-healing
    for column deletes/reorders — mirroring _write_elements' existing
    delete-all-then-reinsert reconciliation (that one already deletes by
    row_id and reinserts only current columns, so it never had this bug)."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题",
        cols["现象识别"]: "- 观察上升沿过冲",
        cols["修复方法"]: "1. 增加阻尼电阻",
    })

    projector.project_table(table_id)
    source_id = projector.ensure_hidden_source(table_id)
    chunks_before = {
        c["id"]: c["text"] for c in repo._runtime.chunk_store.source_chunks(source_id)
    }
    assert len(chunks_before) == 3
    fix_chunk_id = next(cid for cid, text in chunks_before.items() if text == "1. 增加阻尼电阻")
    assert _chunk_fts_ids(repo, {fix_chunk_id}) == {fix_chunk_id}
    assert set(_chunk_embedding_vectors(repo, {fix_chunk_id})) == {fix_chunk_id}

    # Simulate "delete one column via the store" directly (column-delete API
    # lives in KnowhowStore, out of this task's scope) — same net effect from
    # get_knowhow_table's point of view (it simply stops returning the column).
    with repo._connect() as db:
        db.execute("DELETE FROM knowhow_columns WHERE id=?", (cols["修复方法"],))

    projector.project_table(table_id)

    chunks_after = {
        c["id"]: c["text"] for c in repo._runtime.chunk_store.source_chunks(source_id)
    }
    assert set(chunks_after.values()) == {"过冲问题", "- 观察上升沿过冲"}
    assert fix_chunk_id not in chunks_after
    assert len(chunks_after) == 2

    # The removed column's chunk row, its chunks_fts row, AND its
    # chunk_embeddings row are ALL gone — a real leak, not just filtered out
    # of some element-scoped view.
    assert _chunk_fts_ids(repo, {fix_chunk_id}) == set()
    assert _chunk_embedding_vectors(repo, {fix_chunk_id}) == {}

    # The remaining two cells' own chunks are untouched.
    assert set(chunks_after) == set(chunks_before) - {fix_chunk_id}


def test_overlong_cell_splits_into_multiple_chunk_parts(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    long_text = "\n\n".join(["甲" * 2500, "乙" * 2500, "丙" * 2500])
    assert len(long_text) > 4000
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "长文本问题", cols["修复方法"]: long_text,
    })

    projector.project_table(table_id)

    chunk_ids = _row_chunk_ids(repo, row_id)
    assert len(chunk_ids) == 4  # anchor(1) + fix(3 paragraph-bounded parts)


# ---------------------------------------------------------------------------
# project_table: cell-level node model (design doc §④ — object_type=column
# name, cross-row value merge, about edges)
# ---------------------------------------------------------------------------


def test_ko_object_type_equals_column_name_for_every_filled_cell(
    repo, projector, table_id, embedder
):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题",
        cols["现象识别"]: "上升沿过冲",
        cols["根因分析"]: "电源阻抗过高",
        cols["修复方法"]: "增加阻尼电阻",
        cols["依赖工具"]: "示波器",
    })

    projector.project_table(table_id)

    counts = _table_object_type_counts(repo, table_id)
    # Every column name is its own dynamic object_type — no fixed
    # case/procedure/tool vocabulary survives.
    assert counts == {
        "违例类型": 1, "现象识别": 1, "根因分析": 1, "修复方法": 1, "依赖工具": 1,
    }
    assert "case" not in counts and "procedure" not in counts and "tool" not in counts


def test_same_value_in_same_column_across_two_rows_merges_with_two_evidence_and_two_edges(
    repo, projector, table_id, embedder
):
    """Design doc §④ "同列同值跨行归并": two DIFFERENT rows (different
    anchors) whose 根因分析 cell holds the exact same text merge onto ONE
    knowledge object, accumulating both rows' evidence — and each row's own
    about-edge to ITS OWN row-title KO survives (2 distinct edges, since the
    two rows have different anchors)."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_a = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["根因分析"]: "电源阻抗过高",
    })
    row_b = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "欠冲问题", cols["根因分析"]: "电源阻抗过高",
    })

    projector.project_table(table_id)

    assert _table_object_type_counts(repo, table_id).get("根因分析") == 1
    ko = _ko_by_name(repo, table_id, "根因分析", "电源阻抗过高")
    assert set(ko["payload"]["rows"]) == {row_a, row_b}
    assert len(ko["evidence"]) == 2

    # This non-anchor KO is the SOURCE of its about-edges (design doc §④:
    # non-row-title cell KO --about--> row-title cell KO), so its edges are
    # found via _edges_from, not _edges_to (which would look for edges
    # pointing AT it — never the case for a non-anchor cell).
    edges = _edges_from(repo, ko["id"])
    assert len(edges) == 2
    assert {e["edge_type"] for e in edges} == {"about"}
    anchor_a = _ko_by_name(repo, table_id, "违例类型", "过冲问题")
    anchor_b = _ko_by_name(repo, table_id, "违例类型", "欠冲问题")
    assert {e["target_object_id"] for e in edges} == {anchor_a["id"], anchor_b["id"]}


def test_entity_cell_splits_into_multiple_kos_by_list_item(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["依赖工具"]: "- 示波器\n- 万用表",
    })

    projector.project_table(table_id)

    assert _table_object_type_counts(repo, table_id).get("依赖工具") == 2
    names = {k["payload"]["name"] for k in _kos_by_type(repo, table_id, "依赖工具")}
    assert names == {"示波器", "万用表"}


def test_entity_value_merges_across_rows(repo, projector, table_id, embedder):
    """Design doc §④ Innovus example ("entity 拆分跨行归并"): the same tool
    name cited by two DIFFERENT rows' 依赖工具 cell collapses to ONE node
    with an about-edge to EACH row's own row-title KO."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_a = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["依赖工具"]: "Innovus",
    })
    row_b = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "欠冲问题", cols["依赖工具"]: "Innovus",
    })

    projector.project_table(table_id)

    assert _table_object_type_counts(repo, table_id).get("依赖工具") == 1
    ko = _ko_by_name(repo, table_id, "依赖工具", "Innovus")
    assert set(ko["payload"]["rows"]) == {row_a, row_b}
    # A non-anchor KO is the SOURCE of its about-edges, not the target.
    assert len(_edges_from(repo, ko["id"])) == 2


def test_about_edge_direction_is_cell_ko_to_row_title_ko(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: "增加阻尼电阻",
    })

    projector.project_table(table_id)

    anchor_ko = _ko_by_name(repo, table_id, "违例类型", "过冲问题")
    fix_ko = _ko_by_name(repo, table_id, "修复方法", "增加阻尼电阻")
    assert _edges_from(repo, fix_ko["id"]) and _edges_from(repo, fix_ko["id"])[0]["target_object_id"] == anchor_ko["id"]
    assert _edges_from(repo, anchor_ko["id"]) == []  # nothing points FROM the row-title KO


def test_row_title_cell_gets_no_self_edge(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {cols["违例类型"]: "过冲问题"})

    projector.project_table(table_id)

    anchor_ko = _ko_by_name(repo, table_id, "违例类型", "过冲问题")
    assert _edges_from(repo, anchor_ko["id"]) == []
    assert _edges_to(repo, anchor_ko["id"]) == []


def test_about_edges_dedup_when_two_rows_share_both_anchor_and_value(
    repo, projector, table_id, embedder
):
    """Two rows with the SAME anchor value (merging their row-title KOs too)
    AND the same cited tool must not attempt to insert the identical
    (src, about, dst) edge id twice — it collapses to ONE relation row."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["依赖工具"]: "示波器",
    })
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["依赖工具"]: "示波器",
    })

    projector.project_table(table_id)  # must not raise (duplicate relation id)

    anchor_ko = _ko_by_name(repo, table_id, "违例类型", "过冲问题")
    tool_ko = _ko_by_name(repo, table_id, "依赖工具", "示波器")
    assert set(anchor_ko["payload"]["rows"])  # merged from both rows
    assert len(anchor_ko["payload"]["rows"]) == 2
    edges = _edges_to(repo, anchor_ko["id"])
    assert len(edges) == 1
    assert edges[0]["source_object_id"] == tool_ko["id"]


def test_procedure_cell_payload_carries_parsed_steps(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题",
        cols["现象识别"]: "1. 观察上升沿\n2. 测量峰值",
    })

    projector.project_table(table_id)

    ko = _kos_by_type(repo, table_id, "现象识别")[0]
    assert ko["payload"]["steps"] == ["观察上升沿", "测量峰值"]


def test_non_procedure_cell_payload_has_no_steps_key(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["依赖工具"]: "示波器",
    })

    projector.project_table(table_id)

    anchor_ko = _ko_by_name(repo, table_id, "违例类型", "过冲问题")
    tool_ko = _ko_by_name(repo, table_id, "依赖工具", "示波器")
    assert "steps" not in anchor_ko["payload"]
    assert "steps" not in tool_ko["payload"]


def test_evidence_location_label_uses_each_contributing_rows_own_title(
    repo, projector, table_id, embedder
):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["根因分析"]: "共同根因",
    })
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "欠冲问题", cols["根因分析"]: "共同根因",
    })

    projector.project_table(table_id)

    ko = _ko_by_name(repo, table_id, "根因分析", "共同根因")
    labels = {e["location_label"] for e in ko["evidence"]}
    assert labels == {"过冲问题 › 根因分析", "欠冲问题 › 根因分析"}


# ---------------------------------------------------------------------------
# project_table: no-anchor ("记录型") tables project zero KOs/edges
# ---------------------------------------------------------------------------


def test_table_without_anchor_column_projects_zero_kos_but_chunks_exist(
    repo, notebook_id, projector, embedder
):
    store = repo._runtime.knowhow_store
    columns = [
        {"name": "日期", "role": "attribute"},
        {"name": "记录", "role": "attribute"},
    ]
    record_table_id = store.create_knowhow_table(notebook_id, "巡检日志", "", columns)
    cols = _cols_by_name(repo, record_table_id)
    store.add_knowhow_row(record_table_id, {
        cols["日期"]: "2026-01-01", cols["记录"]: "一切正常",
    })

    projector.project_table(record_table_id)

    source_id = projector.ensure_hidden_source(record_table_id)
    with repo._connect() as db:
        object_count = db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects WHERE source_id=?", (source_id,)
        ).fetchone()["c"]
        relation_count = db.execute(
            "SELECT COUNT(*) c FROM knowledge_relations WHERE source_id=?", (source_id,)
        ).fetchone()["c"]
        chunk_count = db.execute(
            "SELECT COUNT(*) c FROM chunks WHERE source_id=?", (source_id,)
        ).fetchone()["c"]
    assert object_count == 0
    assert relation_count == 0
    assert chunk_count == 2  # both cells still become chunks — retrieval-only


def test_anchor_removed_after_being_set_clears_all_kos_on_reproject(
    repo, projector, table_id, embedder
):
    """design doc §① transitional case: a table that HAD an anchor column,
    then had it cleared (set_knowhow_anchor_column(table_id, None) — T1
    store method), must end with ZERO KOs/edges on the next projection —
    the "从有到无的转换清理" the brief calls out explicitly."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: "增加阻尼电阻",
    })
    projector.project_table(table_id)
    assert _table_object_type_counts(repo, table_id)  # sanity: something was projected

    store.set_knowhow_anchor_column(table_id, None)
    projector.project_table(table_id)

    assert _table_object_type_counts(repo, table_id) == {}
    source_id = projector.ensure_hidden_source(table_id)
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM knowledge_relations WHERE source_id=?", (source_id,)
        ).fetchone()["c"] == 0
        # chunks are untouched by the anchor removal — still a valid
        # retrieval-only table.
        assert db.execute(
            "SELECT COUNT(*) c FROM chunks WHERE source_id=?", (source_id,)
        ).fetchone()["c"] > 0


def test_row_with_blank_anchor_cell_contributes_no_kos_but_chunks_still_written(
    repo, projector, table_id, embedder
):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["修复方法"]: "增加阻尼电阻",  # anchor column ("违例类型") left blank
    })

    projector.project_table(table_id)

    assert _row_object_ids(repo, row_id) == set()
    assert _table_object_type_counts(repo, table_id) == {}
    assert _row_chunk_ids(repo, row_id)  # the fix cell still became a chunk
    assert _row_projection_status(repo, table_id, row_id) == "synced"


def test_row_with_no_nonempty_cells_projects_no_kos(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {cols["违例类型"]: "   "})

    projector.project_table(table_id)

    assert _row_object_ids(repo, row_id) == set()
    assert _row_projection_status(repo, table_id, row_id) == "synced"


def test_clearing_all_cells_removes_previously_projected_kos(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: "增加阻尼电阻",
    })
    projector.project_table(table_id)
    assert _row_object_ids(repo, row_id)  # sanity: content projected KOs

    store.update_knowhow_cell(row_id, cols["违例类型"], "")
    store.update_knowhow_cell(row_id, cols["修复方法"], "")
    projector.project_table(table_id)

    assert _row_object_ids(repo, row_id) == set()


def test_project_table_sweeps_orphaned_value_no_longer_referenced_by_any_row(
    repo, projector, table_id, embedder
):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "问题A", cols["依赖工具"]: "示波器",
    })
    projector.project_table(table_id)
    assert _table_object_type_counts(repo, table_id).get("依赖工具") == 1

    store.update_knowhow_cell(row_id, cols["依赖工具"], "")  # clear the only reference
    projector.project_table(table_id)

    # Every project_table call fully rebuilds the KO/edge model from current
    # content — an unreferenced value never survives to the next pass.
    assert _table_object_type_counts(repo, table_id).get("依赖工具", 0) == 0


# ---------------------------------------------------------------------------
# project_table: synthesized section_path (design doc §① compose_row_title)
# ---------------------------------------------------------------------------


def test_section_path_uses_anchor_value_when_anchor_cell_has_content(
    repo, projector, table_id, embedder
):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: "增加阻尼电阻",
    })

    projector.project_table(table_id)

    paths = set(_row_chunk_section_paths(repo, row_id).values())
    assert paths == {"时序修复 › 过冲问题 › 违例类型", "时序修复 › 过冲问题 › 修复方法"}


def test_section_path_uses_composed_title_when_anchor_cell_is_blank(
    repo, projector, table_id, embedder
):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["现象识别"]: "上升沿过冲", cols["修复方法"]: "增加阻尼电阻",
    })

    projector.project_table(table_id)

    paths = _row_chunk_section_paths(repo, row_id)
    fix_path = next(p for cid, p in paths.items()
                    if _row_chunk_texts(repo, row_id)[cid] == "增加阻尼电阻")
    assert fix_path == "时序修复 › 上升沿过冲 · 增加阻尼电阻 › 修复方法"


def test_section_path_uses_composed_title_when_no_anchor_column(
    repo, notebook_id, projector, embedder
):
    store = repo._runtime.knowhow_store
    columns = [
        {"name": "日期", "role": "attribute"},
        {"name": "记录", "role": "attribute"},
    ]
    record_table_id = store.create_knowhow_table(notebook_id, "巡检日志", "", columns)
    cols = _cols_by_name(repo, record_table_id)
    row_id = store.add_knowhow_row(record_table_id, {
        cols["日期"]: "2026-01-01", cols["记录"]: "一切正常",
    })

    projector.project_table(record_table_id)

    paths = set(_row_chunk_section_paths(repo, row_id).values())
    assert paths == {
        "巡检日志 › 2026-01-01 · 一切正常 › 日期",
        "巡检日志 › 2026-01-01 · 一切正常 › 记录",
    }


def test_resolve_row_title_falls_back_to_positional_label_when_every_cell_blank():
    """Direct unit pin for the one _resolve_row_title branch that is
    unobservable end to end (an all-blank row projects zero chunks/elements
    to inspect a section_path on at all — see projection.py's docstring)."""
    from app.services.knowhow.projection import _resolve_row_title

    columns = [{"id": "c1", "name": "A", "position": 0}, {"id": "c2", "name": "B", "position": 1}]
    cell_nets = {"c1": "", "c2": "   "}
    assert _resolve_row_title(None, columns, cell_nets, 4) == "行5"
    assert _resolve_row_title(columns[0], columns, cell_nets, 4) == "行5"


# ---------------------------------------------------------------------------
# project_table: embedding failure (best-effort, never raises)
# ---------------------------------------------------------------------------


def test_embedding_failure_marks_row_failed_without_raising(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: "增加阻尼电阻",
    })
    embedder.fail_next = True

    projector.project_table(table_id)  # must not raise

    assert _row_projection_status(repo, table_id, row_id) == "failed"
    # Structural artifacts are still written even though embedding failed —
    # only the vector write is best-effort/failure-tolerant.
    assert _row_object_ids(repo, row_id)


def test_embedding_failure_emits_through_model_error_channel(repo, projector, table_id, embedder):
    calls = []
    projector.note_model_error = (
        lambda stage, model, exc: calls.append((stage, model, type(exc).__name__))
    )
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: "增加阻尼电阻",
    })
    embedder.fail_next = True

    projector.project_table(table_id)

    assert len(calls) == 1
    stage, _model, exc_type = calls[0]
    assert stage == "knowhow_embed"
    assert exc_type == "RuntimeError"


def test_embed_false_skips_embedding_entirely(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: "增加阻尼电阻",
    })

    projector.project_table(table_id, embed=False)

    assert embedder.call_count == 0
    assert _row_projection_status(repo, table_id, row_id) == "synced"
    assert _row_object_ids(repo, row_id)  # structural writes still happened


# ---------------------------------------------------------------------------
# structural-write failure (distinct from embedding failure above: this one
# MUST raise — a structural-write bug is a programming error, not a flaky
# network call, and must not be silently swallowed). Also proves the KO/edge
# rebuild is atomic across the WHOLE table: a mid-pass failure leaves the
# prior successful projection's KG untouched, rather than half torn down.
# ---------------------------------------------------------------------------


def test_structural_write_failure_marks_row_failed_and_reraises_leaving_prior_kg_untouched(
    repo, projector, table_id, embedder, monkeypatch
):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "问题A", cols["修复方法"]: "方法A",
    })
    projector.project_table(table_id)  # a clean prior pass — establishes a baseline KG
    prior_kos = _table_object_type_counts(repo, table_id)
    assert prior_kos  # sanity: something was projected

    row_b = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "问题B", cols["修复方法"]: "方法B",
    })

    real_insert_elements = repo._runtime.source_store.insert_elements

    def _boom(db, source_id, writes, *, created_at):
        if any(w.metadata.get("knowhow", {}).get("row_id") == row_b for w in writes):
            raise RuntimeError("boom-structural")
        return real_insert_elements(db, source_id, writes, created_at=created_at)

    monkeypatch.setattr(repo._runtime.source_store, "insert_elements", _boom)

    with pytest.raises(RuntimeError, match="boom-structural"):
        projector.project_table(table_id)

    assert _row_projection_status(repo, table_id, row_b) == "failed"
    # The KO/edge rebuild never reached its final step this pass (the loop
    # raised first) — the prior, still-consistent KG survives untouched.
    assert _table_object_type_counts(repo, table_id) == prior_kos


def test_unconfigured_embedder_is_not_a_failure(tmp_path, monkeypatch):
    """No embedder configured at all is a normal, non-failure state for this
    app (mirrors every other embed call site's early-return) — distinct from
    "configured but the call raised". Deliberately does NOT go through the
    repo/table_id fixtures above (repo_factory bakes in the four EMBED_* env
    vars unconditionally so embedder_configured is always true there) —
    builds its own plain repo instead, with no EMBED_* vars set at all."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    assert not repo.settings.embedder_configured

    notebook_id = repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id
    table_id = repo._runtime.knowhow_store.create_knowhow_table(
        notebook_id, "时序修复", "desc", COLUMNS
    )
    rt = repo._runtime
    projector = KnowhowProjector(
        settings=repo.settings,
        database=rt.database,
        knowhow=rt.knowhow_store,
        sources=rt.source_store,
        chunks=rt.chunk_store,
        knowledge=rt.knowledge,
        embedding=rt.source_embedding,
        note_model_error=rt.models.note_model_error,
        invalidate_unified_cache=rt.kg_mutations.invalidate_unified_cache,
        mark_unified_dirty=rt.kg_mutations.mark_unified_kg_dirty,
        new_id=rt.seams.new_id,
        now=rt.seams.now,
    )
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: "增加阻尼电阻",
    })

    projector.project_table(table_id)

    assert _row_projection_status(repo, table_id, row_id) == "synced"


# ---------------------------------------------------------------------------
# delete_table_projection
# ---------------------------------------------------------------------------


def test_delete_table_projection_clears_all_artifacts(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: "增加阻尼电阻", cols["依赖工具"]: "示波器",
    })
    projector.project_table(table_id)
    source_id = projector.ensure_hidden_source(table_id)
    notebook_id = repo._runtime.source_store.get_source(source_id).notebook_id

    projector.delete_table_projection(source_id)

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM sources WHERE id=?", (source_id,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM source_elements WHERE source_id=?", (source_id,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM chunks WHERE source_id=?", (source_id,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM chunks_fts WHERE notebook_id=?", (notebook_id,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects WHERE source_id=?", (source_id,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM knowledge_relations WHERE source_id=?", (source_id,)
        ).fetchone()["c"] == 0


def test_delete_table_projection_is_a_noop_for_none_or_missing_id(repo, projector):
    projector.delete_table_projection(None)  # must not raise
    projector.delete_table_projection("")  # must not raise
    projector.delete_table_projection("src-does-not-exist")  # must not raise


# ---------------------------------------------------------------------------
# legacy-model startup migration bridge (find_legacy_projected_table_ids +
# KnowhowProjector.reproject_legacy_tables — wired into app.services.
# startup_warmup post-readiness, design doc §① compatibility migration note)
# ---------------------------------------------------------------------------


def test_find_legacy_projected_table_ids_detects_legacy_object_types(repo, projector, table_id):
    source_id = projector.ensure_hidden_source(table_id)
    notebook_id = repo._runtime.knowhow_store.get_knowhow_table(table_id)["notebook_id"]
    _insert_legacy_ko(repo, source_id, notebook_id, table_id, object_type="procedure")

    with repo._connect() as db:
        found = find_legacy_projected_table_ids(db)
    assert found == [table_id]


def test_find_legacy_projected_table_ids_ignores_dynamic_type_kos(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {cols["违例类型"]: "过冲问题"})
    projector.project_table(table_id)  # normal cell-level projection only

    with repo._connect() as db:
        assert find_legacy_projected_table_ids(db) == []


def test_find_legacy_projected_table_ids_ignores_non_knowhow_id_prefix(repo, table_id):
    """A 'case'-typed object from some OTHER pipeline (not knowhow's own
    ko-kh- id prefix) must not false-positive this migration scan."""
    notebook_id = repo._runtime.knowhow_store.get_knowhow_table(table_id)["notebook_id"]
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id, notebook_id, object_type, status, owner, "
            "payload, evidence, source_candidate_id, source_id, created_at, updated_at) "
            "VALUES (?,?,?,?,'',?,?,NULL,?,?,?)",
            ("ko-not-knowhow-1", notebook_id, "case", "approved",
             json.dumps({"table_id": table_id}), "[]", "src-x", "2020-01-01", "2020-01-01"),
        )
    with repo._connect() as db:
        assert find_legacy_projected_table_ids(db) == []


def test_find_legacy_projected_table_ids_empty_db_returns_empty_list(repo):
    with repo._connect() as db:
        assert find_legacy_projected_table_ids(db) == []


def test_reproject_legacy_tables_returns_empty_and_submits_nothing_when_none_found(
    repo, projector, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        "app.services.knowhow.projection.background_jobs.submit",
        lambda fn, *a, **kw: calls.append((fn, a, kw)),
    )
    assert projector.reproject_legacy_tables() == []
    assert calls == []


def test_reproject_legacy_tables_dispatches_via_background_jobs_submit_not_inline(
    repo, projector, table_id, monkeypatch
):
    source_id = projector.ensure_hidden_source(table_id)
    notebook_id = repo._runtime.knowhow_store.get_knowhow_table(table_id)["notebook_id"]
    legacy_id = _insert_legacy_ko(repo, source_id, notebook_id, table_id)

    calls = []
    monkeypatch.setattr(
        "app.services.knowhow.projection.background_jobs.submit",
        lambda fn, *a, **kw: calls.append((fn, a, kw)),
    )

    result = projector.reproject_legacy_tables()

    assert result == [table_id]
    assert len(calls) == 1
    fn, args, kwargs = calls[0]
    assert fn == projector.project_table
    assert args == (table_id,)
    assert kwargs.get("name") == f"knowhow-legacy-reproject-{table_id}"
    # The spy never actually RAN project_table -> the legacy KO is still
    # there, proving dispatch really goes through background_jobs.submit
    # (not an inline/synchronous call that would have already replaced it).
    with repo._connect() as db:
        assert db.execute(
            "SELECT 1 FROM knowledge_objects WHERE id=?", (legacy_id,)
        ).fetchone() is not None


def test_reproject_legacy_tables_end_to_end_replaces_legacy_kos_with_dynamic_types(
    repo, projector, table_id, embedder, monkeypatch
):
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: "增加阻尼电阻",
    })
    source_id = projector.ensure_hidden_source(table_id)
    notebook_id = store.get_knowhow_table(table_id)["notebook_id"]
    legacy_id = _insert_legacy_ko(repo, source_id, notebook_id, table_id)

    def _run_synchronously(fn, *args, **kwargs):
        # Mirrors the REAL background_jobs.submit(fn, *args, name=,
        # notify_pending=, **kwargs): those two are submit's OWN named
        # params, never forwarded to fn — a fake that forwarded them
        # verbatim would call fn(table_id, name=...) and blow up on an
        # unexpected kwarg fn never asked for.
        kwargs.pop("name", None)
        kwargs.pop("notify_pending", None)
        return fn(*args, **kwargs)

    monkeypatch.setattr(
        "app.services.knowhow.projection.background_jobs.submit",
        _run_synchronously,
    )

    result = projector.reproject_legacy_tables()

    assert result == [table_id]
    with repo._connect() as db:
        assert db.execute(
            "SELECT 1 FROM knowledge_objects WHERE id=?", (legacy_id,)
        ).fetchone() is None  # legacy KO gone — project_table tore it down
    counts = _table_object_type_counts(repo, table_id)
    assert counts.get("case", 0) == 0
    assert counts.get("违例类型") == 1  # dynamic type in its place
    assert counts.get("修复方法") == 1


# ---------------------------------------------------------------------------
# get_scheduler registry hygiene (PR-2+3 收尾 fix)
# ---------------------------------------------------------------------------


def test_get_scheduler_entry_does_not_pin_repo(repo_factory):
    """The _SCHEDULERS WeakKeyDictionary's VALUE must not strongly reference
    its KEY: get_scheduler's project_fn holds only a ``weakref.ref(repo)``
    (see the _SCHEDULERS comment block in knowhow/api.py) — a plain closure
    over ``repo`` would pin every entry alive forever, silently defeating the
    weak keying.  Mirrors test_scale_artifact_runtime's retention-test shape
    (weakref + del + gc.collect + death assertion)."""
    import gc
    import weakref

    from app.services.knowhow import api as knowhow_api

    repo = repo_factory()
    scheduler = knowhow_api.get_scheduler(repo)
    assert knowhow_api.get_scheduler(repo) is scheduler  # registry hit
    repo_ref = weakref.ref(repo)
    del repo
    gc.collect()

    assert repo_ref() is None  # the scheduler's project_fn must not pin it
    assert len(knowhow_api._SCHEDULERS) == 0  # entry died with its key
    # A late-firing debounced run against the collected repo is a no-op, not
    # an explosion: project_fn resolves its weakref per run.
    scheduler._project_fn("table-anything")


# ---------------------------------------------------------------------------
# P1-2 (PR review, cross-notebook move/copy): a concurrent teardown landing
# mid-project_table must not let the terminal KO/relation write commit
# orphans against a table/hidden source a racing move (or the plain DELETE
# route — same exposure, same fix) already tore down. knowledge_objects.
# source_id carries no FK to sources, so nothing else stops that write from
# landing; the subsequent bump_knowhow_mutation_seq raises KeyError once the
# table row is gone, but only AFTER the orphans below are already committed
# — that secondary symptom is not what this test is about.
# ---------------------------------------------------------------------------


def test_project_table_skips_terminal_write_when_table_deleted_mid_pass(
    repo, projector, table_id, embedder, monkeypatch
):
    """Deterministic interleaving reproducing the race: every row this pass
    has finished its own structural+embed work (in production this window is
    typically "parked in embedding" — a network-bound call several rows in)
    when a concurrent move lands, mirroring move_table's own ordering exactly
    (teardown-before-row-delete) — it just happens to land BEFORE this
    already-in-flight project_table pass reaches ITS OWN terminal write,
    instead of strictly before or after the whole call.

    Anchor-only row (no other column filled) deliberately produces a KO with
    ZERO edges (test_row_title_cell_gets_no_self_edge's own shape) so this
    isolates the exact exposure the review flagged: knowledge_objects.
    source_id carries no FK, so insert_object_chunk commits the orphan
    cleanly inside the transaction; a row that also produced an edge would
    instead hit knowledge_relations.source_id's REFERENCES sources(id) FK
    and roll the whole transaction back (see the edges variant below) — a
    different, also-real symptom, but not the silent-ghost-data one."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {cols["违例类型"]: "过冲问题"})
    hidden_source_id = projector.ensure_hidden_source(table_id)

    real_set_status = store.set_knowhow_row_projection

    def _concurrent_move_after_last_row(row_id, status):
        real_set_status(row_id, status)
        if status in ("synced", "failed"):  # the per-row loop's terminal call
            projector.delete_table_projection(hidden_source_id)
            repo._runtime.knowhow_store.delete_knowhow_table(table_id)

    monkeypatch.setattr(
        store, "set_knowhow_row_projection", _concurrent_move_after_last_row
    )

    try:
        projector.project_table(table_id)
    except KeyError:
        pass  # pre-fix: bump_knowhow_mutation_seq on the now-gone table_id

    with repo._connect() as db:
        orphaned_kos = db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects WHERE source_id=?",
            (hidden_source_id,),
        ).fetchone()["c"]
    assert orphaned_kos == 0
    # The guard must SKIP the write, not resurrect anything the concurrent
    # move already deleted.
    with pytest.raises(KeyError):
        repo._runtime.knowhow_store.get_knowhow_table(table_id)


def test_project_table_does_not_crash_with_integrity_error_when_source_deleted_mid_pass(
    repo, projector, table_id, embedder, monkeypatch
):
    """Edges variant of the same race: with an about-edge in play, pre-fix
    the terminal write's insert_object_chunk succeeds (no FK) but the
    following insert_relation_chunk hits knowledge_relations.source_id's FK
    against the now-deleted hidden source and raises — an uncaught
    sqlite3.IntegrityError escaping project_table entirely (the whole
    terminal transaction rolls back, so no orphan here, but a background
    projection job blowing up on a raw IntegrityError is still a real,
    distinct symptom of the same missing existence check). The guard must
    turn this into a clean, silent no-op skip instead."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: "增加阻尼电阻",
    })
    hidden_source_id = projector.ensure_hidden_source(table_id)

    real_set_status = store.set_knowhow_row_projection

    def _concurrent_move_after_last_row(row_id, status):
        real_set_status(row_id, status)
        if status in ("synced", "failed"):
            projector.delete_table_projection(hidden_source_id)
            repo._runtime.knowhow_store.delete_knowhow_table(table_id)

    monkeypatch.setattr(
        store, "set_knowhow_row_projection", _concurrent_move_after_last_row
    )

    projector.project_table(table_id)  # must not raise

    with repo._connect() as db:
        orphaned_kos = db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects WHERE source_id=?",
            (hidden_source_id,),
        ).fetchone()["c"]
        orphaned_relations = db.execute(
            "SELECT COUNT(*) c FROM knowledge_relations WHERE source_id=?",
            (hidden_source_id,),
        ).fetchone()["c"]
    assert orphaned_kos == 0
    assert orphaned_relations == 0
    with pytest.raises(KeyError):
        repo._runtime.knowhow_store.get_knowhow_table(table_id)


def test_project_table_proceeds_normally_when_nothing_concurrent_happens(
    repo, projector, table_id, embedder
):
    """Companion happy-path guard: the new existence re-check must not turn
    project_table into a no-op on the ordinary, uncontended pass — the KOs
    for a still-live table must still land."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_name(repo, table_id)
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: "增加阻尼电阻",
    })

    projector.project_table(table_id)

    counts = _table_object_type_counts(repo, table_id)
    assert counts.get("违例类型") == 1
    assert counts.get("修复方法") == 1
    assert repo._runtime.knowhow_store.get_knowhow_table(table_id)["mutation_seq"] >= 1
