# backend/tests/test_knowhow_retrieval.py
"""Task 10 (knowhow-tables PR-1): end-to-end projection -> retrieval
integration test — the net for cross-task bugs. Proves the full chain:
import (Task 6 API) -> deterministic projection (Task 5) -> EXISTING
retrieval surfaces (chunk/FTS/vector search, KG type counts, the knowledge
graph, and ask_chunk) actually find knowhow content, then cleanly stop
finding it once the table is deleted.

See docs/superpowers/plans/2026-07-15-knowhow-tables-pr1.md Task 10.

Mirrors two existing conventions rather than inventing a third:
  - test_knowhow_api.py: TestClient + register/login + the deterministic
    background-job poll loop (``_poll_all_rows_settled``).
  - test_trackF_governance_promotion.py / test_kg_search_api.py: grab the
    app's own ``lru_cache``-backed repository singleton via
    ``app.api.knowhow_routes.repository()`` and set ``.embedder`` on THAT instance
    (not a separate ``SQLiteRepository(...)`` pointed at the same DB file) —
    required so the background projection job (which runs against the app's
    singleton) actually calls a fake, deterministic embedder instead of
    trying real network I/O, and so this file's own direct
    ``_retrieve_chunks``/``ask_chunk`` calls share that same fake embedder
    and in-process caches.
"""
from __future__ import annotations

import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.models.schemas import AskRequest
from app.services.embedding import FakeEmbedder
from tests.model_testkit import bind_embedding_client
from tests.model_testkit import bind_chat_client

EMBED_DIM = 16

# Time-series-fix-up domain column names, verbatim from the task brief
# ("列名用时序修复域：违例概念/现象识别方法/根因分析动作/修复方法/依赖工具") —
# kinds updated to the PR-2+3 behavior-kind vocabulary (anchor/procedure/
# entity/attribute; migration 17 remapped the legacy five-role instance
# vocabulary). PR-2+3 Task 3 flips the import WIRE: the row-title column is
# no longer a per-column value — it's the separate ANCHOR_INDEX below, sent
# as its own form field (mirrors Task 6's confirmed-mapping contract, updated
# for the kinds wire).
HEADER = ["违例概念", "现象识别方法", "根因分析动作", "修复方法", "依赖工具"]
KINDS = ["attribute", "procedure", "procedure", "procedure", "entity"]
ANCHOR_INDEX = 0
TABLE_TITLE = "时序修复表"

# A rare Latin/digit token embedded in ONE row's fix cell only — CJK-run
# tokenization turns Chinese text into character bi-grams but keeps a
# Latin/digit run as one whole token (app/services/retrieval.py docstring),
# so this behaves exactly like this codebase's other "XZQW9000"-style rare
# lexical probes (see test_chunk_fts_backfill_and_search /
# test_chunk_ann_unions_lexical in test_retrieval_service.py).
UNIQUE_TERM = "TSFIX7788"

DATA_ROWS = [
    ["过冲问题", "上升沿观测到明显过冲", "电源阻抗偏高导致振荡环",
     f"增加阻尼电阻并重新走线 {UNIQUE_TERM}", "示波器"],
    ["欠冲问题", "下降沿观测到明显欠冲", "寄生电感过大",
     "调整走线拓扑降低寄生电感", "万用表"],
    ["抖动问题", "眼图抖动超出规格", "时钟源相位噪声过高",
     "更换低噪声时钟源芯片", "示波器"],
]


def _xlsx_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app's own repository singleton (obtained via
    ``app.api.knowhow_routes.repository()``, an ``lru_cache``d no-arg function —
    calling it constructs-and-caches once per test, and ``TestClient(app)``'s
    ``Depends(repository)`` resolves to that SAME cached instance), with a
    fake embedder installed on it so the background projection job actually
    produces vectors — needed to later prove a vector really disappears on
    delete, not merely that one was never created."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    # Mirrors test_retrieval_service.py's repo fixture: blank any real LLM
    # keys a developer's shell might export, so ask_chunk's "no LLM
    # configured" deterministic fallback path is dependable in every
    # environment, not just a clean CI box.
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")

    from app.api.knowhow_routes import repository
    from app.main import app

    c = TestClient(app)
    c._repo = repository()  # type: ignore[attr-defined]
    bind_embedding_client(c._repo, FakeEmbedder(dim=EMBED_DIM))
    assert c._repo.configured("knowhow_embedding")
    return c


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _mk_notebook(client, headers, name="N"):
    return client.post("/api/notebooks", json={"name": name}, headers=headers).json()["id"]


def _columns_json():
    return json.dumps([{"name": n, "kind": k} for n, k in zip(HEADER, KINDS)])


def _import_table(client, headers, nb) -> str:
    data = _xlsx_bytes(HEADER, DATA_ROWS)
    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/import",
        headers=headers,
        files={"file": ("rules.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={
            "title": TABLE_TITLE,
            "columns_json": _columns_json(),
            "anchor_index": str(ANCHOR_INDEX),
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _poll_all_rows_settled(client, headers, nb, table_id, timeout=5.0):
    """Mirrors test_knowhow_api.py's poll-loop: wait for every row's
    projection_status to leave pending/syncing (settle at synced/failed)."""
    deadline = time.time() + timeout
    detail = None
    while time.time() < deadline:
        detail = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=headers).json()
        rows = detail.get("rows", [])
        if rows and all(r["projection_status"] in ("synced", "failed") for r in rows):
            return detail
        time.sleep(0.05)
    return detail


@pytest.fixture
def imported(client):
    """Import the 3x5 fixture table and wait for it to fully project.
    Returns everything the four scenarios need to key off of."""
    headers = _login(client, "a00000601")
    nb = _mk_notebook(client, headers)
    table_id = _import_table(client, headers, nb)

    detail = _poll_all_rows_settled(client, headers, nb, table_id)
    assert detail is not None and detail["rows"], detail
    assert all(r["projection_status"] == "synced" for r in detail["rows"]), detail

    cols_by_name = {c["name"]: c["id"] for c in detail["columns"]}
    concept_col = cols_by_name["违例概念"]
    row_id = next(
        r["id"] for r in detail["rows"] if r["cells"].get(concept_col) == "过冲问题"
    )
    return {
        "nb": nb,
        "headers": headers,
        "table_id": table_id,
        "hidden_source_id": detail["hidden_source_id"],
        "row_id": row_id,
    }


# ---------------------------------------------------------------------------
# Small direct-DB helpers, mirroring test_knowhow_projection.py's
# _row_element_ids/_row_chunk_texts/_row_chunk_section_paths convention.
# ---------------------------------------------------------------------------


def _row_element_ids(repo, row_id: str) -> set[str]:
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id FROM source_elements WHERE json_extract(metadata,'$.knowhow.row_id')=?",
            (row_id,),
        ).fetchall()
    return {r["id"] for r in rows}


def _row_chunks(repo, row_id: str) -> list[dict]:
    """[{id, text, section_path}] for every chunk this row owns."""
    element_ids = _row_element_ids(repo, row_id)
    if not element_ids:
        return []
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, text, section_path, element_ids FROM chunks"
        ).fetchall()
    out = []
    for r in rows:
        eids = json.loads(r["element_ids"] or "[]")
        if any(eid in element_ids for eid in eids):
            out.append({"id": r["id"], "text": r["text"], "section_path": r["section_path"]})
    return out


def _element_knowhow_meta(repo, element_id: str) -> dict:
    with repo._connect() as db:
        row = db.execute(
            "SELECT metadata FROM source_elements WHERE id=?", (element_id,)
        ).fetchone()
    assert row is not None, f"element {element_id} not found"
    return json.loads(row["metadata"])["knowhow"]


# ---------------------------------------------------------------------------
# Assertion 1: chunk table has cell-level chunks with a correctly-structured
# section_path ("表 › 概念 › 列名").
# ---------------------------------------------------------------------------


def test_cell_level_chunks_have_structured_section_path(client, imported):
    repo = client._repo
    hidden_source_id = imported["hidden_source_id"]
    row_id = imported["row_id"]

    with repo._connect() as db:
        all_chunks = db.execute(
            "SELECT id FROM chunks WHERE source_id=?", (hidden_source_id,)
        ).fetchall()
    # Every cell in the 3x5 grid is non-empty -> one chunk per cell, table-wide.
    assert len(all_chunks) == len(HEADER) * len(DATA_ROWS)

    row_chunks = _row_chunks(repo, row_id)
    assert len(row_chunks) == len(HEADER)  # one chunk per column, for THIS row

    seen_columns = set()
    for chunk in row_chunks:
        parts = chunk["section_path"].split(" › ")
        assert len(parts) == 3, chunk["section_path"]
        table_part, concept_part, column_part = parts
        assert table_part == TABLE_TITLE
        assert concept_part == "过冲问题"
        assert column_part in HEADER
        seen_columns.add(column_part)
    assert seen_columns == set(HEADER)

    fix_chunk = next(c for c in row_chunks if UNIQUE_TERM in c["text"])
    assert fix_chunk["section_path"] == f"{TABLE_TITLE} › 过冲问题 › 修复方法"


# ---------------------------------------------------------------------------
# Assertion 2: querying the fix cell's unique term hits the knowhow chunk via
# both the FTS primitive and the chunk-native retrieval surface, and the hit
# resolves back to {table_id, row_id} through element metadata.
# ---------------------------------------------------------------------------


def test_fts_and_retrieval_hit_knowhow_chunk_with_element_backtrace(client, imported):
    repo = client._repo
    nb = imported["nb"]
    table_id = imported["table_id"]
    row_id = imported["row_id"]

    # --- FTS path (chunks_fts, populated by ChunkStore.insert_rows at
    # projection time, no separate backfill call) ---
    with repo._connect() as db:
        fts_hits = repo._runtime.knowledge.chunk_fts_search(db, nb, UNIQUE_TERM, k=10)
    assert fts_hits, "FTS 未命中修复方法格独特词"
    fts_chunk_id = fts_hits[0]["chunk_id"]

    # --- chunk-native retrieval surface (the same primitive ask_chunk uses:
    # keyword+semantic fusion over this notebook's chunks) ---
    scored, _ids, _mat = repo._retrieve_chunks(nb, UNIQUE_TERM)
    assert scored, "检索路径 _retrieve_chunks 未命中"
    hit = next((c for c in scored if UNIQUE_TERM in c.text), None)
    assert hit is not None, [c.chunk_id for c in scored]
    assert hit.chunk_id == fts_chunk_id
    # Citation-facing source title resolves to the hidden source's title.
    assert hit.source_title == f"Knowhow 表：{TABLE_TITLE}"

    # --- element metadata backtrace: chunk -> element_ids[0] ->
    # source_elements.metadata.knowhow.{table_id,row_id} ---
    assert hit.element_ids
    meta = _element_knowhow_meta(repo, hit.element_ids[0])
    assert meta["table_id"] == table_id
    assert meta["row_id"] == row_id
    assert meta["column_name"] == "修复方法"

    # --- bonus: the actual QA-facing retrieval surface (ask_chunk) surfaces
    # a citation for this content too, with the same citation-facing title
    # and an element_id that resolves the same way (no LLM configured in
    # this fixture -> deterministic non-LLM fallback, same shape as
    # test_ask_chunk_deterministic_without_llm in test_retrieval_service.py).
    resp = repo.ask_chunk(nb, AskRequest(question=UNIQUE_TERM))
    assert resp.citations, "ask_chunk 未产出引用"
    cite = next((c for c in resp.citations if UNIQUE_TERM in c.quoted_span), None)
    assert cite is not None, [c.quoted_span for c in resp.citations]
    assert cite.label.startswith(f"Knowhow 表：{TABLE_TITLE}")
    assert cite.element_id == hit.element_ids[0]


# ---------------------------------------------------------------------------
# Assertion 3: the hidden source never appears in GET /sources, but KO type
# counts and the knowledge graph include the projected cell-level objects
# (knowhow-tables PR-2+3 Task 2: object_type is now the CELL'S OWN COLUMN
# NAME — a dynamic type, not the PR-1 fixed case/procedure/tool vocabulary)
# and their `about` edges (every non-row-title cell --about--> the row-title
# cell — design doc §④, "边直写...统一用既有 about").
# ---------------------------------------------------------------------------


def test_hidden_source_excluded_but_ko_counts_and_graph_include_projection(client, imported):
    nb = imported["nb"]
    headers = imported["headers"]
    hidden_source_id = imported["hidden_source_id"]

    sources_resp = client.get(f"/api/notebooks/{nb}/sources", headers=headers)
    assert sources_resp.status_code == 200, sources_resp.text
    body = sources_resp.json()
    assert body["total_count"] == 0
    assert hidden_source_id not in {s["id"] for s in body["items"]}

    types_resp = client.get(f"/api/notebooks/{nb}/knowledge-types", headers=headers)
    assert types_resp.status_code == 200, types_resp.text
    counts = {t["object_type"]: t["count"] for t in types_resp.json()}
    # Every column is its OWN dynamic object_type now — no case/procedure/
    # tool bucket survives. 3 rows, every non-entity column holds a distinct
    # value per row -> 3 KOs each; the entity column's values ("示波器"/
    # "万用表"/"示波器") dedup by casefolded name across rows -> 2 KOs.
    assert counts.get("违例概念") == len(DATA_ROWS)
    assert counts.get("现象识别方法") == len(DATA_ROWS)
    assert counts.get("根因分析动作") == len(DATA_ROWS)
    assert counts.get("修复方法") == len(DATA_ROWS)
    assert counts.get("依赖工具") == 2
    assert not ({"case", "procedure", "tool"} & set(counts))

    graph_resp = client.get(f"/api/notebooks/{nb}/graph", headers=headers)
    assert graph_resp.status_code == 200, graph_resp.text
    graph = graph_resp.json()
    node_types = {n["object_type"] for n in graph["nodes"]}
    assert {"违例概念", "现象识别方法", "根因分析动作", "修复方法", "依赖工具"} <= node_types
    edge_relations = {e["relation"] for e in graph["edges"]}
    # Every knowhow-derived edge is now the SAME generic `about` relation
    # (design doc §④: "语义全在节点类型上，边只表达行结构") — the domain
    # semantics that used to distinguish identified_by/diagnosed_by/
    # fixed_by/requires_tool now live entirely on the node's object_type.
    assert edge_relations == {"about"}


# ---------------------------------------------------------------------------
# Assertion 4: deleting the table clears chunks/FTS/vectors/KOs, and the same
# unique-term query no longer hits anything.
# ---------------------------------------------------------------------------


def test_delete_table_clears_chunk_fts_vector_and_ko_so_query_no_longer_hits(client, imported):
    repo = client._repo
    nb = imported["nb"]
    headers = imported["headers"]
    table_id = imported["table_id"]
    hidden_source_id = imported["hidden_source_id"]

    # Capture the concrete ids BEFORE delete: chunk_embeddings.chunk_id has no
    # source_id column of its own (only an FK to chunks(id)), so a post-delete
    # check must key off ids captured while the chunk rows still existed —
    # otherwise a WHERE-source_id-via-subquery check would be vacuously true.
    with repo._connect() as db:
        chunk_rows_before = db.execute(
            "SELECT id FROM chunks WHERE source_id=?", (hidden_source_id,)
        ).fetchall()
    chunk_ids = [r["id"] for r in chunk_rows_before]
    assert chunk_ids, "sanity: table must have produced chunks before delete"
    placeholders = ",".join("?" for _ in chunk_ids)

    with repo._connect() as db:
        vec_before = db.execute(
            f"SELECT COUNT(*) c FROM chunk_embeddings WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        ).fetchone()["c"]
        fts_before = db.execute(
            f"SELECT COUNT(*) c FROM chunks_fts WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        ).fetchone()["c"]
        ko_before = db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects WHERE source_id=?", (hidden_source_id,)
        ).fetchone()["c"]
    # Sanity: everything is genuinely present pre-delete (fake embedder is
    # configured, so every chunk really got embedded) — the negative
    # assertions below only prove something if there was something to lose.
    assert vec_before == len(chunk_ids)
    assert fts_before == len(chunk_ids)
    assert ko_before > 0
    pre_fts_hits = repo._runtime.knowledge.chunk_fts_search
    with repo._connect() as db:
        assert pre_fts_hits(db, nb, UNIQUE_TERM, k=10)

    delete_resp = client.delete(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=headers)
    assert delete_resp.status_code == 204, delete_resp.text

    with repo._connect() as db:
        chunk_after = db.execute(
            f"SELECT COUNT(*) c FROM chunks WHERE id IN ({placeholders})", chunk_ids
        ).fetchone()["c"]
        vec_after = db.execute(
            f"SELECT COUNT(*) c FROM chunk_embeddings WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        ).fetchone()["c"]
        fts_after = db.execute(
            f"SELECT COUNT(*) c FROM chunks_fts WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        ).fetchone()["c"]
        ko_after = db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects WHERE source_id=?", (hidden_source_id,)
        ).fetchone()["c"]
    assert chunk_after == 0
    assert vec_after == 0
    assert fts_after == 0
    assert ko_after == 0

    # The knowledge-types view has nothing left to show for this table either
    # (dynamic per-column types, not the old fixed case/procedure/tool names).
    types_resp = client.get(f"/api/notebooks/{nb}/knowledge-types", headers=headers)
    counts = {t["object_type"]: t["count"] for t in types_resp.json()}
    assert counts.get("违例概念", 0) == 0
    assert counts.get("现象识别方法", 0) == 0
    assert counts.get("根因分析动作", 0) == 0
    assert counts.get("修复方法", 0) == 0
    assert counts.get("依赖工具", 0) == 0

    # Same unique-term query, same surfaces as assertion 2 — nothing hits now.
    with repo._connect() as db:
        assert repo._runtime.knowledge.chunk_fts_search(db, nb, UNIQUE_TERM, k=10) == []
    scored, _ids, _mat = repo._retrieve_chunks(nb, UNIQUE_TERM)
    assert not any(UNIQUE_TERM in c.text for c in scored)

    resp = repo.ask_chunk(nb, AskRequest(question=UNIQUE_TERM))
    assert not any(UNIQUE_TERM in c.quoted_span for c in resp.citations)


# ---------------------------------------------------------------------------
# Assertion 5 (final-review blocker): a KG rebuild must NOT touch the knowhow
# projection — it must neither wipe the projected case/procedure/tool objects
# and their edges, nor re-extract the hidden source with the LLM (the feature's
# zero-LLM invariant). Two seams enforce this: delete_notebook_graph_rows (the
# wipe) preserves knowhow-sourced rows, and source_build_rows (the extraction
# target picker) excludes the hidden source.
# ---------------------------------------------------------------------------


def _seed_real_source_with_kg(repo, nb):
    """Insert a normal (non-knowhow) source plus one KG object and one relation
    for it, so a rebuild wipe has a genuine extraction-derived target to remove
    (and re-target) — the control group that proves the wipe still fires for
    everything that ISN'T knowhow."""
    src_id = "src-real-doc-1"
    repo._runtime.source_store.insert_source(
        source_id=src_id, notebook_id=nb, title="真实文档", source_type="pdf",
        status="active", parse_status="parsed", file_name="", file_path="",
        file_size=0, file_hash="", summary="", doc_type="",
    )
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id, notebook_id, object_type, status, "
            "owner, payload, evidence, source_candidate_id, source_id, created_at, updated_at) "
            "VALUES (?,?,?,?,'','{}','[]',NULL,?,?,?)",
            ("ko-real-1", nb, "concept", "approved", src_id, "2026-01-01", "2026-01-01"),
        )
        db.execute(
            "INSERT INTO knowledge_relations (id, notebook_id, source_id, "
            "source_object_id, target_object_id, edge_type, evidence, created_at) "
            "VALUES (?,?,?,?,?,?,'[]',?)",
            ("rel-real-1", nb, src_id, "ko-real-1", "ko-real-1", "relates_to", "2026-01-01"),
        )
        db.execute(   # 有 elements = 已成功 parse(rebuild 后 build 才会把它当抽取目标)
            "INSERT INTO source_elements (id, source_id, element_type, location_label, "
            "text, metadata, created_at) VALUES (?,?,'paragraph','p1','body','{}',?)",
            ("el-real-1", src_id, "2026-01-01"),
        )
    return src_id


def _knowhow_kg_ids(repo, hidden_source_id):
    with repo._connect() as db:
        kos = {r["id"] for r in db.execute(
            "SELECT id FROM knowledge_objects WHERE source_id=?", (hidden_source_id,)
        ).fetchall()}
        rels = {r["id"] for r in db.execute(
            "SELECT id FROM knowledge_relations WHERE source_id=?", (hidden_source_id,)
        ).fetchall()}
    return kos, rels


def test_kg_delete_wipe_preserves_knowhow_projection_but_wipes_real_kg(client, imported):
    repo = client._repo
    nb = imported["nb"]
    hidden_source_id = imported["hidden_source_id"]

    kh_kos_before, kh_rels_before = _knowhow_kg_ids(repo, hidden_source_id)
    assert kh_kos_before and kh_rels_before  # sanity: projection produced KOs+edges
    _seed_real_source_with_kg(repo, nb)

    # The wipe phase of a rebuild (delete_notebook_kg -> delete_notebook_graph_rows).
    repo.delete_notebook_kg(nb)

    kh_kos_after, kh_rels_after = _knowhow_kg_ids(repo, hidden_source_id)
    # Knowhow projection survived the wipe unchanged (same id sets)...
    assert kh_kos_after == kh_kos_before
    assert kh_rels_after == kh_rels_before
    # ...while the genuine extraction-derived KG for the real source is gone.
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects WHERE id='ko-real-1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM knowledge_relations WHERE id='rel-real-1'"
        ).fetchone()["c"] == 0


def test_kg_rebuild_never_extracts_hidden_knowhow_source(client, imported):
    from unittest.mock import MagicMock

    repo = client._repo
    nb = imported["nb"]
    hidden_source_id = imported["hidden_source_id"]
    real_src = _seed_real_source_with_kg(repo, nb)
    kh_kos_before, _ = _knowhow_kg_ids(repo, hidden_source_id)

    # Drive the real rebuild path (delete + build) with the extraction call
    # faked to a recorder — proves which sources the picker actually targets,
    # without a live LLM/embedder for the "extraction" itself.
    targeted: list[str] = []
    repo._run_extraction = lambda sid: targeted.append(sid)
    bind_chat_client(repo, "kg_extract", MagicMock(configured=True))

    repo.rebuild_notebook_kg(nb)

    # The hidden knowhow source was NEVER an extraction target; the real source was.
    assert hidden_source_id not in targeted
    assert real_src in targeted
    # No extraction_runs row was opened against the hidden source, and its
    # projected KG objects survived the rebuild's wipe intact.
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM extraction_runs WHERE source_id=?", (hidden_source_id,)
        ).fetchone()["c"] == 0
    kh_kos_after, _ = _knowhow_kg_ids(repo, hidden_source_id)
    assert kh_kos_after == kh_kos_before
