"""Task 5 (knowhow-tables PR-1): KnowhowProjector — deterministic, zero-LLM
projection of knowhow-table rows into knowledge_objects/knowledge_relations/
chunks/source_elements, so existing ask/reasoning/KG retrieval covers knowhow
content for free. Real SQLite (via a full SQLiteRepository, mirroring
test_knowhow_store.py's fixture convention) + a fake embedder that records
call counts/texts (mirroring test_ask_embed_cache.py's fixture convention).
See docs/superpowers/plans/2026-07-15-knowhow-tables-pr1.md Task 5.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.knowhow.projection import KnowhowProjector
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
    {"name": "违例类型", "role": "concept"},
    {"name": "现象识别", "role": "identify"},
    {"name": "根因分析", "role": "root_cause"},
    {"name": "修复方法", "role": "fix"},
    {"name": "依赖工具", "role": "tool"},
]


@pytest.fixture
def table_id(repo, notebook_id) -> str:
    return repo._runtime.knowhow_store.create_knowhow_table(
        notebook_id, "时序修复", "desc", COLUMNS
    )


def _cols_by_role(repo, table_id: str) -> dict[str, str]:
    detail = repo._runtime.knowhow_store.get_knowhow_table(table_id)
    return {c["role"]: c["id"] for c in detail["columns"]}


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
    "table › concept › column" breadcrumb move without the chunk's text
    changing (concept-cell edits move every sibling's section_path)."""
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
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id FROM knowledge_objects WHERE json_extract(payload,'$.row_id')=?",
            (row_id,),
        ).fetchall()
    return {r["id"] for r in rows}


def _row_case_id(repo, row_id: str) -> "str | None":
    with repo._connect() as db:
        row = db.execute(
            "SELECT id FROM knowledge_objects WHERE object_type='case' "
            "AND json_extract(payload,'$.row_id')=?",
            (row_id,),
        ).fetchone()
    return row["id"] if row else None


def _edge_types_from(repo, source_object_id: str) -> list[str]:
    with repo._connect() as db:
        rows = db.execute(
            "SELECT edge_type FROM knowledge_relations WHERE source_object_id=?",
            (source_object_id,),
        ).fetchall()
    return sorted(r["edge_type"] for r in rows)


def _table_object_type_counts(repo, table_id: str) -> dict[str, int]:
    with repo._connect() as db:
        rows = db.execute(
            "SELECT object_type, COUNT(*) AS n FROM knowledge_objects "
            "WHERE json_extract(payload,'$.table_id')=? GROUP BY object_type",
            (table_id,),
        ).fetchall()
    return {r["object_type"]: r["n"] for r in rows}


def _row_projection_status(repo, table_id: str, row_id: str) -> str:
    detail = repo._runtime.knowhow_store.get_knowhow_table(table_id)
    row = next(r for r in detail["rows"] if r["id"] == row_id)
    return row["projection_status"]


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
# project_row: idempotency
# ---------------------------------------------------------------------------


def test_project_row_twice_produces_identical_id_sets(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题",
        cols["identify"]: "- 观察上升沿过冲\n- 测量峰值电压",
        cols["root_cause"]: "1. 检查电源阻抗\n2. 排查寄生电感",
        cols["fix"]: "1. 增加阻尼电阻\n2. 调整走线",
        cols["tool"]: "- 示波器\n- 万用表",
    })

    projector.project_row(table_id, row_id)
    elements_1 = _row_element_ids(repo, row_id)
    chunks_1 = _row_chunk_ids(repo, row_id)
    objects_1 = _row_object_ids(repo, row_id)

    projector.project_row(table_id, row_id)
    elements_2 = _row_element_ids(repo, row_id)
    chunks_2 = _row_chunk_ids(repo, row_id)
    objects_2 = _row_object_ids(repo, row_id)

    assert elements_1 and elements_1 == elements_2
    assert chunks_1 and chunks_1 == chunks_2
    assert objects_1 and objects_1 == objects_2


def test_derived_ids_use_the_contracted_prefixes(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "问题A", cols["identify"]: "现象说明", cols["tool"]: "示波器",
    })
    projector.project_row(table_id, row_id)
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
# project_row: incremental chunk diff + embedding call counts
# ---------------------------------------------------------------------------


def test_single_cell_edit_rebuilds_only_that_chunk_and_embeds_once(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题",
        cols["identify"]: "- 观察上升沿过冲",
        cols["fix"]: "1. 增加阻尼电阻",
    })

    projector.project_row(table_id, row_id)
    assert embedder.call_count == 1
    chunks_before = _row_chunk_texts(repo, row_id)
    assert len(chunks_before) == 3  # concept + identify + fix, one chunk each

    store.update_knowhow_cell(row_id, cols["fix"], "1. 更换更大电容")
    projector.project_row(table_id, row_id)

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


def test_reprojecting_unchanged_row_makes_zero_additional_embed_calls(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题",
        cols["fix"]: "1. 增加阻尼电阻",
    })

    projector.project_row(table_id, row_id)
    assert embedder.call_count == 1

    projector.project_row(table_id, row_id)
    assert embedder.call_count == 1  # nothing changed -> zero additional embed calls


def test_reproject_self_heals_a_missing_chunk_vector(repo, projector, table_id, embedder):
    """Review hardening: the carry-over window (structural transaction
    committed, vector re-persist did NOT happen — process death or
    replace_chunk_vectors raising in between) used to leave a text-unchanged
    chunk vector-less FOREVER: the next project_row saw
    ``old_specs == new_specs`` and skipped the cell entirely, and nothing
    else ever re-embeds a knowhow chunk. project_row must probe its
    text-unchanged chunks' vectors and re-embed exactly the missing ones —
    making a per-row retry a real recovery path, at zero embed calls (one
    id-keyed SELECT) in the all-present steady state (pinned by
    test_reprojecting_unchanged_row_makes_zero_additional_embed_calls
    above)."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题",
        cols["identify"]: "- 观察上升沿过冲",
        cols["fix"]: "1. 增加阻尼电阻",
    })

    projector.project_row(table_id, row_id)
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

    projector.project_row(table_id, row_id)  # NO cell edits

    # Exactly one extra embed call, carrying exactly the vector-less chunk's
    # current text — the two chunks that still had vectors were not re-sent.
    assert embedder.call_count == 2
    assert embedder.calls[-1] == ["- 观察上升沿过冲"]
    assert set(_chunk_embedding_vectors(repo, {target_id})) == {target_id}
    assert _chunk_embedding_vectors(repo, other_ids) == vectors_before
    assert _row_projection_status(repo, table_id, row_id) == "synced"


def test_concept_cell_edit_moves_sibling_section_paths_without_reembedding(
    repo, projector, table_id, embedder
):
    """Review finding: section_path bakes in the row's concept (`f"{table} ›
    {concept} › {column}"`), so editing ONLY the concept cell changes every
    SIBLING chunk's section_path too even though their text never changed.
    The chunk row must still be rewritten (section_path has to move) but must
    NOT be re-embedded — and (chunk_embeddings.chunk_id is ON DELETE CASCADE
    onto chunks(id), and this chunk's id is stable for the same row/column/
    split) its existing embedding must survive, not get cascade-deleted by a
    delete+reinsert that nothing then re-embeds."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题",
        cols["identify"]: "- 观察上升沿过冲",
        cols["fix"]: "1. 增加阻尼电阻",
    })

    projector.project_row(table_id, row_id)
    assert embedder.call_count == 1
    chunks_before = _row_chunk_texts(repo, row_id)
    assert len(chunks_before) == 3
    paths_before = _row_chunk_section_paths(repo, row_id)
    vectors_before = _chunk_embedding_vectors(repo, set(chunks_before))
    assert set(vectors_before) == set(chunks_before)  # all 3 embedded on first projection

    concept_chunk_id = next(cid for cid, text in chunks_before.items() if text == "过冲问题")

    store.update_knowhow_cell(row_id, cols["concept"], "过冲新问题")
    projector.project_row(table_id, row_id)

    # Exactly 1 new embed call, for exactly the concept cell's OWN new text —
    # zero re-embeds for the two sibling cells whose text never changed.
    assert embedder.call_count == 2
    assert embedder.calls[-1] == ["过冲新问题"]

    chunks_after = _row_chunk_texts(repo, row_id)
    paths_after = _row_chunk_section_paths(repo, row_id)
    assert set(chunks_after) == set(chunks_before)  # same 3 stable chunk-id slots

    changed_text_ids = {cid for cid in chunks_before if chunks_before[cid] != chunks_after[cid]}
    assert changed_text_ids == {concept_chunk_id}
    assert chunks_after[concept_chunk_id] == "过冲新问题"
    sibling_ids = set(chunks_before) - {concept_chunk_id}

    # ALL 3 chunks' section_path reflects the NEW concept — including the two
    # siblings whose own text is untouched.
    for cid in chunks_after:
        assert "过冲新问题" in paths_after[cid]
        assert paths_after[cid] != paths_before[cid]

    # The siblings' embeddings are the SAME rows as before: still present
    # (not cascade-deleted) and byte-identical (reused, not recomputed).
    vectors_after = _chunk_embedding_vectors(repo, sibling_ids)
    assert set(vectors_after) == sibling_ids
    assert vectors_after == {cid: vectors_before[cid] for cid in sibling_ids}


def test_project_row_sweeps_chunks_for_a_column_removed_from_the_table(
    repo, projector, table_id, embedder
):
    """Review finding: _write_chunks only ever walked the table's CURRENT
    columns, so a column removed from the table left its old chunk group
    (+chunks_fts +chunk_embeddings) behind forever, retrievable even though
    the column is gone. project_row alone must be self-healing for column
    deletes/reorders — mirroring _write_elements' existing delete-all-then-
    reinsert reconciliation (that one already deletes by row_id and
    reinserts only current columns, so it never had this bug)."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题",
        cols["identify"]: "- 观察上升沿过冲",
        cols["fix"]: "1. 增加阻尼电阻",
    })

    projector.project_row(table_id, row_id)
    source_id = projector.ensure_hidden_source(table_id)
    chunks_before = {
        c["id"]: c["text"] for c in repo._runtime.chunk_store.source_chunks(source_id)
    }
    assert len(chunks_before) == 3
    fix_chunk_id = next(cid for cid, text in chunks_before.items() if text == "1. 增加阻尼电阻")
    assert _chunk_fts_ids(repo, {fix_chunk_id}) == {fix_chunk_id}
    assert set(_chunk_embedding_vectors(repo, {fix_chunk_id})) == {fix_chunk_id}

    # Simulate "delete one column via the store": KnowhowStore has no
    # column-delete API yet (that lands with Task 6's table-editing routes)
    # — drop the knowhow_columns row directly, the same net effect such an
    # endpoint would have from get_knowhow_table's point of view (it simply
    # stops returning the column).
    with repo._connect() as db:
        db.execute("DELETE FROM knowhow_columns WHERE id=?", (cols["fix"],))

    projector.project_row(table_id, row_id)

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
    cols = _cols_by_role(repo, table_id)
    long_text = "\n\n".join(["甲" * 2500, "乙" * 2500, "丙" * 2500])
    assert len(long_text) > 4000
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "长文本问题", cols["fix"]: long_text,
    })

    projector.project_row(table_id, row_id)

    chunk_ids = _row_chunk_ids(repo, row_id)
    assert len(chunk_ids) == 4  # concept(1) + fix(3 paragraph-bounded parts)


# ---------------------------------------------------------------------------
# project_row: KO/edge shape
# ---------------------------------------------------------------------------


def test_case_procedure_tool_counts_and_edge_types(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题",
        cols["identify"]: "- 观察上升沿过冲",
        cols["root_cause"]: "1. 检查电源阻抗",
        cols["fix"]: "1. 增加阻尼电阻",
        cols["tool"]: "- 示波器\n- 万用表",
    })

    projector.project_row(table_id, row_id)

    counts = _table_object_type_counts(repo, table_id)
    assert counts == {"case": 1, "procedure": 3, "tool": 2}

    case_id = _row_case_id(repo, row_id)
    assert case_id is not None
    assert _edge_types_from(repo, case_id) == [
        "diagnosed_by", "fixed_by", "identified_by", "requires_tool", "requires_tool",
    ]


def test_tool_deduped_within_table_across_rows(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_a = store.add_knowhow_row(table_id, {
        cols["concept"]: "问题A", cols["tool"]: "示波器",
    })
    row_b = store.add_knowhow_row(table_id, {
        cols["concept"]: "问题B", cols["tool"]: "Oscilloscope",  # different casing/spelling on purpose
    })
    projector.project_row(table_id, row_a)
    projector.project_row(table_id, row_b)

    # "示波器" is a distinct name from "Oscilloscope" (no cross-language dedup —
    # dedup is casefold on the SAME literal name), so this table has 2 tools.
    assert _table_object_type_counts(repo, table_id).get("tool") == 2

    row_c = store.add_knowhow_row(table_id, {
        cols["concept"]: "问题C", cols["tool"]: "OSCILLOSCOPE",  # same name, different case
    })
    projector.project_row(table_id, row_c)
    # Same normalized name as row_b's tool -> dedup to the SAME tool object,
    # table-wide count stays at 2 (not 3).
    assert _table_object_type_counts(repo, table_id).get("tool") == 2


def test_row_with_no_nonempty_cells_projects_no_case_ko(repo, projector, table_id, embedder):
    """Belt-and-braces with grid_parser's all-empty-row drop: a row whose only
    cell is whitespace has no net content, so project_row must write NO case KO
    (not a phantom empty-fields case) — and still settle 'synced'."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {cols["concept"]: "   "})

    projector.project_row(table_id, row_id)

    assert _row_object_ids(repo, row_id) == set()
    assert _row_projection_status(repo, table_id, row_id) == "synced"


def test_clearing_all_cells_removes_the_previously_projected_case_ko(
    repo, projector, table_id, embedder
):
    """The delete-then-skip path: a row that HAD content, then had every cell
    cleared, must end with zero row-scoped KOs — its prior case/procedure
    objects swept and no new phantom empty case written in their place."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题", cols["fix"]: "1. 增加阻尼电阻",
    })
    projector.project_row(table_id, row_id)
    assert _row_object_ids(repo, row_id)  # sanity: content projected a case + procedure

    store.update_knowhow_cell(row_id, cols["concept"], "")
    store.update_knowhow_cell(row_id, cols["fix"], "")
    projector.project_row(table_id, row_id)

    assert _row_object_ids(repo, row_id) == set()


# ---------------------------------------------------------------------------
# project_table: full-rebuild escape hatch
# ---------------------------------------------------------------------------


def test_added_row_with_no_mutation_seq_bump_is_still_projected_by_project_table(
    repo, projector, table_id, embedder
):
    """Controller-adjudicated pin: add_knowhow_row deliberately does NOT bump
    mutation_seq (only update_knowhow_cell does) — project_table must
    discover rows by full enumeration, never by a seq-delta gate, or a
    freshly-added row would be silently skipped."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    seq_before = store.get_knowhow_table(table_id)["mutation_seq"]
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "新行", cols["fix"]: "1. 处理方法",
    })
    seq_after_add = store.get_knowhow_table(table_id)["mutation_seq"]
    assert seq_after_add == seq_before  # confirms the store-level precondition this test pins

    projector.project_table(table_id)

    assert _row_projection_status(repo, table_id, row_id) == "synced"
    assert _row_object_ids(repo, row_id)


def test_project_table_sweeps_orphaned_tool_no_longer_referenced_by_any_row(
    repo, projector, table_id, embedder
):
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "问题A", cols["tool"]: "示波器",
    })
    projector.project_row(table_id, row_id)
    assert _table_object_type_counts(repo, table_id).get("tool") == 1

    store.update_knowhow_cell(row_id, cols["tool"], "")  # clear the only reference
    projector.project_row(table_id, row_id)
    # project_row alone is row-scoped: it does NOT clean up the now-orphaned
    # tool object (by design — see delete_objects_by_source_and_row).
    assert _table_object_type_counts(repo, table_id).get("tool") == 1

    projector.project_table(table_id)
    # project_table's full rebuild wipes + recomputes from scratch, so an
    # unreferenced tool does not survive.
    assert _table_object_type_counts(repo, table_id).get("tool", 0) == 0


# ---------------------------------------------------------------------------
# delete_table_projection
# ---------------------------------------------------------------------------


def test_delete_table_projection_clears_all_artifacts(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题", cols["fix"]: "1. 增加阻尼电阻", cols["tool"]: "示波器",
    })
    projector.project_row(table_id, row_id)
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
# embedding failure
# ---------------------------------------------------------------------------


def test_embedding_failure_marks_row_failed_without_raising(repo, projector, table_id, embedder):
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题", cols["fix"]: "1. 增加阻尼电阻",
    })
    embedder.fail_next = True

    projector.project_row(table_id, row_id)  # must not raise

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
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题", cols["fix"]: "1. 增加阻尼电阻",
    })
    embedder.fail_next = True

    projector.project_row(table_id, row_id)

    assert len(calls) == 1
    stage, _model, exc_type = calls[0]
    assert stage == "knowhow_embed"
    assert exc_type == "RuntimeError"


# ---------------------------------------------------------------------------
# structural-write failure (distinct from embedding failure above: this one
# MUST raise — a structural-write bug is a programming error, not a flaky
# network call, and must not be silently swallowed)
# ---------------------------------------------------------------------------


def test_structural_write_failure_marks_row_failed_and_reraises(
    repo, projector, table_id, embedder, monkeypatch
):
    """Review finding: project_row sets 'syncing' then runs the structural
    database.write() block (elements/chunks/KOs); an exception raised inside
    that block used to propagate straight out with the row never leaving
    'syncing' (no code path ever flips it again). The row must land on
    'failed' (an honest terminal state) and the exception must still
    propagate — unlike the embed-failure path above, this is NOT a
    best-effort/no-raise case."""
    store = repo._runtime.knowhow_store
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题", cols["fix"]: "1. 增加阻尼电阻",
    })

    def _boom(*args, **kwargs):
        raise RuntimeError("boom-structural")

    # insert_relation_chunk is the LAST store call inside _write_knowledge —
    # patching it lets everything else in the structural block run first,
    # so this also proves the whole transaction rolls back on failure (no
    # half-written case KO survives).
    monkeypatch.setattr(projector.knowledge, "insert_relation_chunk", _boom)

    with pytest.raises(RuntimeError, match="boom-structural"):
        projector.project_row(table_id, row_id)

    assert _row_projection_status(repo, table_id, row_id) == "failed"
    assert not _row_object_ids(repo, row_id)  # transaction rolled back, not half-applied


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
    cols = _cols_by_role(repo, table_id)
    row_id = store.add_knowhow_row(table_id, {
        cols["concept"]: "过冲问题", cols["fix"]: "1. 增加阻尼电阻",
    })

    projector.project_row(table_id, row_id)

    assert _row_projection_status(repo, table_id, row_id) == "synced"
