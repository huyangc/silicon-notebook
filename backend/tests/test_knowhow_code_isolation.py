# backend/tests/test_knowhow_code_isolation.py
"""knowhow-tables PR-2+3 Task 15: the code-isolation invariant (design doc
§⑥-4): "代码永不进 element/chunk/embedding/FTS/KG 投影，ask 上下文组装不含
它——在 notebook 内不可被检索，只能经格子定位查看（投影只读 content_md，
天然满足，另加投影不受附件影响的守护测试）".

Attaches a distinctively-marked code body to a real cell via the Agent-
surface PUT endpoint (`PUT /agent/knowhow/rows/{row}/cells/{col}/code`),
then proves the marker text is absent from EVERY machine-consumption
surface a real, freshly-run projection pass touches: source_elements
(text + metadata), chunks, chunks_fts, knowledge_objects/knowledge_relations
payloads, every embedder input recorded during a pass that GENUINELY embeds
(the code-bearing cell is edited to new content AFTER the code attach, so
the per-cell chunk diff cannot short-circuit the embed step — see
test_marker_never_reaches_any_embedder_call's own docstring for why a pass
over unchanged cells would make that assertion vacuous), and the
ask/chunk-retrieval surface for a query built from the marker itself. Also
covers the brief's two companion invariants: a cell EDIT after the code was
attached leaves the code attachment itself byte-for-byte untouched (only
its derived freshness status moves), and a table DELETE cascades away its
cell_code rows (the store-level cascade is already unit-tested in
test_knowhow_store.py::test_delete_table_cascades_cell_code_too — this
closes the loop through the REAL HTTP delete endpoint instead of a direct
store call, the same "safety net" angle every test in this file takes).

By code inspection, `KnowhowProjector.project_table`/`_write_elements`/
`_write_chunks`/`_accumulate_row_knowledge` (app/services/knowhow/
projection.py) read ONLY `row["cells"]` (i.e. `knowhow_cells.content_md`)
— the `knowhow_cell_code` table is never referenced anywhere in that
module. This file is the end-to-end regression test that keeps that true
across future edits, not a re-derivation of the invariant from first
principles.

Mirrors test_knowhow_retrieval.py's `client` fixture (the app's own
lru_cache'd repository singleton, obtained via ``app.api.routes.
repository()``) — required for the SAME reason that file documents: the
background projection job the ProjectionScheduler launches runs against
the app's singleton, so a fake embedder installed on a SEPARATELY
constructed ``SQLiteRepository`` would never see any ``embed_chunk_ids``
call at all (a silently vacuous "proof" of absence, not a real one).
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.models.schemas import AskRequest
from app.services.embedding import FakeEmbedder

EMBED_DIM = 16

# A code-shaped, unmistakably distinctive token — never legitimately present
# in any cell content_md, table/column name, or retrieval query fixture
# elsewhere in this file.
MARKER = "ZQKH99_SECRET_CODE_PAYLOAD_MUST_NOT_LEAK"


class RecordingEmbedder(FakeEmbedder):
    """Wraps FakeEmbedder to record every text ever handed to embed_texts/
    embed_query — enabling the embed-input claim: not merely "no vector-table
    row carries the marker" (already true, and uninteresting, once the marker
    never reaches a chunk row) but "no embedding call was ever GIVEN the
    marker as input text". That claim is only non-vacuous on a projection
    pass that actually CALLS the embedder — a pass over unchanged cells
    short-circuits at the per-cell chunk diff and embeds nothing — so the
    test asserting it first edits the code-bearing cell to force a real
    embed (see test_marker_never_reaches_any_embedder_call)."""

    def __init__(self, dim: int = EMBED_DIM) -> None:
        super().__init__(dim=dim)
        self.embedded_texts: list[str] = []

    def embed_texts(self, texts):
        self.embedded_texts.extend(texts)
        return super().embed_texts(texts)

    def embed_query(self, text):
        self.embedded_texts.append(text)
        return super().embed_query(text)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app's own repository singleton, with a recording fake embedder
    installed on it (see module docstring for why this must be the
    singleton, not a separately constructed SQLiteRepository)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")

    from app.api.routes import repository
    from app.main import app

    c = TestClient(app)
    c._repo = repository()  # type: ignore[attr-defined]
    c._recorder = RecordingEmbedder(dim=EMBED_DIM)  # type: ignore[attr-defined]
    c._repo.embedder = c._recorder
    assert c._repo.settings.embedder_configured
    return c


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _mk_notebook(client, headers, name="N"):
    return client.post("/api/notebooks", json={"name": name}, headers=headers).json()["id"]


DEFAULT_COLUMNS = (("现象", "attribute"), ("方法", "procedure"), ("工具", "entity"))


def _create_table(client, headers, nb, *, title="判别表", columns=DEFAULT_COLUMNS, anchor_index=0):
    body: dict = {"title": title, "columns": [{"name": n, "kind": k} for n, k in columns]}
    if anchor_index is not None:
        body["anchor_index"] = anchor_index
    resp = client.post(f"/api/notebooks/{nb}/knowhow", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _add_row(client, headers, nb, table_id, cells):
    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows", headers=headers, json={"cells": cells},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _poll_all_rows_settled(client, headers, nb, table_id, timeout=5.0):
    """Mirrors test_knowhow_api.py's poll-loop convention."""
    deadline = time.time() + timeout
    detail = None
    while time.time() < deadline:
        detail = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=headers).json()
        rows = detail.get("rows", [])
        if rows and all(r["projection_status"] in ("synced", "failed") for r in rows):
            return detail
        time.sleep(0.05)
    return detail


def _project_table_directly(client, table_id) -> None:
    """Force a fresh, synchronous project_table pass — not the debounced
    background scheduler — so every assertion in this file proves the
    isolation invariant holds for a projection pass that runs WHILE the code
    attachment exists in the DB (not merely "it happened to run earlier,
    before the marker existed" as a side effect of call ordering). Mirrors
    knowhow_api.get_scheduler's own project_fn
    (``build_projector(repo).project_table(table_id)``), called directly and
    synchronously so the test needs no debounce-window poll."""
    from app.services.knowhow import api as knowhow_api
    knowhow_api.build_projector(client._repo).project_table(table_id)


def _all_source_element_blobs(repo) -> list[str]:
    with repo._connect() as db:
        rows = db.execute("SELECT text, metadata FROM source_elements").fetchall()
    return [r["text"] for r in rows] + [r["metadata"] for r in rows]


def _all_chunk_texts(repo) -> list[str]:
    with repo._connect() as db:
        rows = db.execute("SELECT text FROM chunks").fetchall()
    return [r["text"] for r in rows]


def _all_ko_blobs(repo) -> list[str]:
    with repo._connect() as db:
        rows = db.execute("SELECT payload, evidence FROM knowledge_objects").fetchall()
    return [r["payload"] for r in rows] + [r["evidence"] for r in rows]


def _all_relation_blobs(repo) -> list[str]:
    with repo._connect() as db:
        rows = db.execute("SELECT evidence FROM knowledge_relations").fetchall()
    return [r["evidence"] for r in rows]


@pytest.fixture
def seeded(client):
    """A table with an anchor + procedure + entity column, one row with
    real content in every cell, fully projected via the real
    ProjectionScheduler — then a code attachment carrying MARKER on the
    procedure cell (the natural "an external Agent implemented this
    method" case design doc §⑥-4 describes)."""
    headers = _login(client, "a00000701")
    nb = _mk_notebook(client, headers)
    table = _create_table(client, headers, nb)
    anchor_id, procedure_id, entity_id = (c["id"] for c in table["columns"])
    row = _add_row(client, headers, nb, table["id"], {
        anchor_id: "过冲问题",
        procedure_id: "1. 看眼图\n2. 测阻抗",
        entity_id: "示波器",
    })
    detail = _poll_all_rows_settled(client, headers, nb, table["id"])
    assert detail is not None and detail["rows"], detail
    assert all(r["projection_status"] == "synced" for r in detail["rows"]), detail

    code_url = f"/api/agent/knowhow/rows/{row['id']}/cells/{procedure_id}/code"
    put_resp = client.put(
        code_url, headers=headers,
        json={"code_text": f"def fix():\n    return '{MARKER}'\n", "language": "python"},
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["status"] == "implemented"
    assert MARKER in put_resp.json()["code_text"]

    return {
        "headers": headers, "nb": nb, "table_id": table["id"],
        "hidden_source_id": detail["hidden_source_id"],
        "row_id": row["id"], "procedure_column_id": procedure_id,
        "code_url": code_url,
    }


# ---------------------------------------------------------------------------
# The core isolation invariant: elements / chunks / KG / relations.
# ---------------------------------------------------------------------------


def test_marker_absent_from_elements_chunks_ko_and_relations_after_projection(client, seeded):
    repo = client._repo
    table_id = seeded["table_id"]

    # Force a fresh projection pass with the code attachment already in
    # place — project_table only ever reads knowhow_cells.content_md, never
    # knowhow_cell_code, so a marker planted ONLY in the code attachment
    # must not surface anywhere this rebuild touches.
    _project_table_directly(client, table_id)

    element_blobs = _all_source_element_blobs(repo)
    chunk_texts = _all_chunk_texts(repo)
    ko_blobs = _all_ko_blobs(repo)
    relation_blobs = _all_relation_blobs(repo)

    # Sanity: real content genuinely made it through projection (otherwise
    # the negative assertions below would be vacuously true).
    assert any("过冲问题" in blob for blob in element_blobs)
    assert any("看眼图" in text for text in chunk_texts)
    assert ko_blobs

    assert not any(MARKER in blob for blob in element_blobs)
    assert not any(MARKER in text for text in chunk_texts)
    assert not any(MARKER in blob for blob in ko_blobs)
    assert not any(MARKER in blob for blob in relation_blobs)


def test_marker_never_reaches_any_embedder_call(client, seeded):
    """The embed-input claim: no embed_texts/embed_query call is ever GIVEN
    the marker. Crucially, this must be asserted over a pass that GENUINELY
    embeds — a forced pass over UNCHANGED cells short-circuits at the
    per-cell chunk diff (``old_specs == new_specs -> continue``) and never
    calls the embedder at all, so asserting over such a pass would only
    inherit the recorder's marker-free state from the seeded fixture's
    original pre-attachment projection (vacuous — caught in review). So:
    with the marker-bearing code attached, PATCH the code-bearing cell to
    NEW (non-marker) content, scope the recorder to what follows, force a
    synchronous pass, and prove BOTH that a real embed call happened on
    THIS pass (the new content is in the recorder) AND that no embed input
    ever carried the marker."""
    headers = seeded["headers"]
    nb = seeded["nb"]
    table_id = seeded["table_id"]
    row_id = seeded["row_id"]
    col_id = seeded["procedure_column_id"]

    patch_resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows/{row_id}/cells/{col_id}",
        headers=headers, json={"content_md": "1. 改用新的观测方法\n2. 复测阻抗曲线"},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    # Sanity: the marker-bearing attachment is still present for the pass below.
    assert MARKER in client.get(seeded["code_url"], headers=headers).json()["code_text"]

    # Scope the recorder to ONLY the forced pass below — the seeded fixture's
    # own initial projection already recorded (marker-free) texts, and
    # inheriting those would prove nothing about a pass that runs WHILE the
    # code attachment exists.
    client._recorder.embedded_texts.clear()

    _project_table_directly(client, table_id)

    # A genuine embed call happened on THIS pass (the edited cell's new text)...
    assert any("改用新的观测方法" in text for text in client._recorder.embedded_texts), (
        client._recorder.embedded_texts
    )
    # ...and no embed input ever carried the code attachment's marker.
    assert not any(MARKER in text for text in client._recorder.embedded_texts)


def test_marker_absent_from_fts_and_chunk_retrieval_and_ask_context(client, seeded):
    """The 'ask 上下文组装不含它' clause. This codebase has no separate
    offline context-assembly entry point distinct from the chunk-retrieval
    primitive ask_chunk itself uses (`_retrieve_chunks`) and ask_chunk's own
    deterministic no-LLM-configured fallback (which still builds real
    citations from that SAME retrieved set) — so driving both, with a query
    built from the marker itself, is the full extent of that surface this
    codebase has to offer."""
    repo = client._repo
    nb = seeded["nb"]
    _project_table_directly(client, seeded["table_id"])

    # FTS: a direct search for the marker returns zero rows (chunks_fts is
    # populated exclusively from chunks.text at insert_rows time — nothing
    # to find if the marker never became a chunk).
    with repo._connect() as db:
        assert repo._runtime.knowledge.chunk_fts_search(db, nb, MARKER, k=10) == []

    # The same chunk-candidate primitive ask_chunk itself uses.
    scored, _ids, _mat = repo._retrieve_chunks(nb, MARKER)
    assert not any(MARKER in c.text for c in scored)

    resp = repo.ask_chunk(nb, AskRequest(question=MARKER))
    assert not any(MARKER in c.quoted_span for c in resp.citations)
    assert MARKER not in (resp.answer or "")


# ---------------------------------------------------------------------------
# Companion invariants: cell edit survives the code attachment; table
# delete cascades it away.
# ---------------------------------------------------------------------------


def test_cell_edit_after_code_attach_leaves_code_untouched_and_still_isolated(client, seeded):
    """Task brief: "cell EDIT after code attach → code survives untouched".
    Editing the CODE-BEARING cell's own content (not deleting the
    attachment) must: (a) leave the stored code_text byte-for-byte
    identical; (b) flip its derived freshness to 'stale' (the existing T10
    contract, re-proven here end-to-end); and (c) still never leak the
    marker into the freshly reprojected elements/chunks for that same
    cell."""
    headers = seeded["headers"]
    row_id = seeded["row_id"]
    col_id = seeded["procedure_column_id"]
    nb = seeded["nb"]
    table_id = seeded["table_id"]
    code_url = seeded["code_url"]

    before = client.get(code_url, headers=headers).json()
    assert before["status"] == "implemented"
    assert MARKER in before["code_text"]

    patch_resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows/{row_id}/cells/{col_id}",
        headers=headers, json={"content_md": "3. 全新识别步骤，与之前完全不同"},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    _project_table_directly(client, table_id)

    after = client.get(code_url, headers=headers).json()
    assert after["code_text"] == before["code_text"]  # untouched byte-for-byte
    assert after["language"] == before["language"]
    assert after["status"] == "stale"  # the cell moved on since the attachment

    repo = client._repo
    assert not any(MARKER in blob for blob in _all_source_element_blobs(repo))
    assert not any(MARKER in text for text in _all_chunk_texts(repo))
    assert not any(MARKER in blob for blob in _all_ko_blobs(repo))


def test_table_delete_cascades_cell_code_rows(client, seeded):
    """Task brief: "表 DELETE → code 行级联删除" — exercised through the
    REAL HTTP delete endpoint (not a direct store call:
    test_knowhow_store.py::test_delete_table_cascades_cell_code_too already
    covers the store-level FK cascade in isolation; this closes the loop at
    the facade/API level, the same "safety net" angle every other test in
    this file takes)."""
    repo = client._repo
    headers = seeded["headers"]
    nb = seeded["nb"]
    table_id = seeded["table_id"]
    row_id = seeded["row_id"]

    with repo._connect() as db:
        before = db.execute(
            "SELECT COUNT(*) AS n FROM knowhow_cell_code WHERE row_id = ?", (row_id,)
        ).fetchone()["n"]
    assert before == 1

    delete_resp = client.delete(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=headers)
    assert delete_resp.status_code == 204, delete_resp.text

    with repo._connect() as db:
        after = db.execute(
            "SELECT COUNT(*) AS n FROM knowhow_cell_code WHERE row_id = ?", (row_id,)
        ).fetchone()["n"]
    assert after == 0
