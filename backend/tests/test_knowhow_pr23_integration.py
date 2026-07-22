# backend/tests/test_knowhow_pr23_integration.py
"""knowhow-tables PR-2+3 Task 15: end-to-end integration net across the
whole editing + Agent surface (design doc, all of §④/§⑥) — the cross-task
safety net for seams individual task test files exercise in isolation with
narrower fixtures (synthetic wire dicts, fake stores, or the PR-1 import
path only). Every scenario here drives the REAL HTTP editing API
(T3: wizard/PATCH/rows) through the REAL debounced ProjectionScheduler,
never `import_table`'s own bulk orchestration — test_knowhow_retrieval.py
already covers the import-driven path exhaustively; this file specifically
proves the newer EDITING surface produces the identical downstream shape.

Six scenarios (task brief lettering preserved in each test's docstring):
  (a) edit cell via PATCH -> scheduler -> retrieval reflects new text.
  (b) code_status flips implemented -> stale on a cell edit via real
      endpoints (same write as (a), different consumer).
  (c) dynamic types appear in /knowledge-types + /graph with `about` edges,
      produced by the EDITING surface (not import).
  (d) discrimination set e2e, including the no-anchor-table 400.
  (e) citation enrichment e2e: `citations_from` against a REAL projected
      element (not test_knowhow_citation.py's fake sources) ->
      Citation.knowhow populated.
  (f) anchor set -> unset transition: KOs/edges torn down, chunks survive,
      discrimination now 400s.

Mirrors test_knowhow_retrieval.py's `client` fixture (repository singleton
+ fake embedder) and test_knowhow_agent_api.py's wizard/row helper
conventions.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.models.schemas import AskRequest, Evidence
from app.services.embedding import FakeEmbedder
from app.services.retrieval import RetrievedKnowledge
from tests.model_testkit import bind_embedding_client

EMBED_DIM = 16


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app's own repository singleton with a fake embedder installed —
    needed so a background projection job (driven by the REAL
    ProjectionScheduler, which the editing HTTP endpoints call) produces
    real vectors, and so this file's own direct `_retrieve_chunks`/
    `ask_chunk` calls share that same fake embedder and in-process caches
    (mirrors test_knowhow_retrieval.py's own fixture rationale exactly)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
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
    """Mirrors test_knowhow_api.py's/test_knowhow_editing_api.py's own
    poll-loop convention: wait for every row's projection_status to leave
    pending/syncing (settle at synced/failed)."""
    deadline = time.time() + timeout
    detail = None
    while time.time() < deadline:
        detail = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=headers).json()
        rows = detail.get("rows", [])
        if rows and all(r["projection_status"] in ("synced", "failed") for r in rows):
            return detail
        time.sleep(0.05)
    return detail


def _ko_count(repo, hidden_source_id) -> int:
    if not hidden_source_id:
        return 0
    with repo._connect() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_objects WHERE source_id = ?",
            (hidden_source_id,),
        ).fetchone()["n"]


def _chunk_count(repo, hidden_source_id) -> int:
    if not hidden_source_id:
        return 0
    with repo._connect() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_id = ?", (hidden_source_id,),
        ).fetchone()["n"]


@pytest.fixture
def edited_env(client):
    """Wizard-created (NOT imported) table + one row with real content in
    every cell, fully projected via the REAL ProjectionScheduler. Shared
    setup for scenarios (a)/(b)/(d)/(e), each of which independently mutates
    or reads from it without interfering with the others (fresh DB per
    test function)."""
    headers = _login(client, "a00000801")
    nb = _mk_notebook(client, headers)
    table = _create_table(client, headers, nb)
    anchor_id, procedure_id, entity_id = (c["id"] for c in table["columns"])
    row = _add_row(client, headers, nb, table["id"], {
        anchor_id: "过冲问题",
        procedure_id: "先做A",
        entity_id: "示波器",
    })
    detail = _poll_all_rows_settled(client, headers, nb, table["id"])
    assert detail is not None and detail["rows"], detail
    assert all(r["projection_status"] == "synced" for r in detail["rows"]), detail
    return {
        "headers": headers, "nb": nb, "table": table, "row": row,
        "anchor_id": anchor_id, "procedure_id": procedure_id, "entity_id": entity_id,
        "hidden_source_id": detail["hidden_source_id"],
    }


# ---------------------------------------------------------------------------
# (a) edit cell via PATCH -> scheduler -> retrieval reflects new text.
# ---------------------------------------------------------------------------


def test_a_patch_cell_scheduler_reflects_new_text_in_retrieval(client, edited_env):
    headers = edited_env["headers"]
    nb = edited_env["nb"]
    table_id = edited_env["table"]["id"]
    row_id = edited_env["row"]["id"]
    procedure_id = edited_env["procedure_id"]
    repo = client._repo

    OLD_TERM, NEW_TERM = "先做A", "改用全新做法WXQZ1234"
    with repo._connect() as db:
        before_texts = [r["text"] for r in db.execute("SELECT text FROM chunks").fetchall()]
    assert any(OLD_TERM in t for t in before_texts)

    patch_resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows/{row_id}/cells/{procedure_id}",
        headers=headers, json={"content_md": NEW_TERM},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    # The scheduler is debounced (0.5s) — poll the actual settled outcome,
    # not merely that the PATCH's own response returned.
    detail = _poll_all_rows_settled(client, headers, nb, table_id)
    assert all(r["projection_status"] == "synced" for r in detail["rows"]), detail

    with repo._connect() as db:
        after_texts = [r["text"] for r in db.execute("SELECT text FROM chunks").fetchall()]
    assert any(NEW_TERM in t for t in after_texts)
    assert not any(OLD_TERM in t for t in after_texts)

    scored, _ids, _mat = repo._retrieve_chunks(nb, NEW_TERM)
    assert any(NEW_TERM in c.text for c in scored), [c.text for c in scored]


# ---------------------------------------------------------------------------
# (b) code_status flips implemented -> stale on cell edit via real endpoints.
# ---------------------------------------------------------------------------


def test_b_patch_cell_flips_code_status_stale_via_real_endpoints(client, edited_env):
    headers = edited_env["headers"]
    nb = edited_env["nb"]
    table_id = edited_env["table"]["id"]
    row_id = edited_env["row"]["id"]
    procedure_id = edited_env["procedure_id"]
    code_url = f"/api/agent/knowhow/rows/{row_id}/cells/{procedure_id}/code"

    put_resp = client.put(
        code_url, headers=headers, json={"code_text": "print('ok')", "language": "python"},
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["status"] == "implemented"

    patch_resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows/{row_id}/cells/{procedure_id}",
        headers=headers, json={"content_md": "彻底换了一种新做法"},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    get_resp = client.get(code_url, headers=headers)
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["status"] == "stale"
    # The code body itself is untouched by the cell edit (only status moves).
    assert get_resp.json()["code_text"] == "print('ok')"


# ---------------------------------------------------------------------------
# (c) dynamic types + /graph `about` edges, produced by the EDITING surface.
# ---------------------------------------------------------------------------


def test_c_editing_surface_produces_dynamic_types_and_about_edges(client):
    """Distinct from test_knowhow_retrieval.py's own import-driven version
    of this same assertion: here the table is built via the WIZARD + rows
    API (T3's ProjectionScheduler-driven path), not import_table's bulk
    orchestration — proving both call sites of `project_table` produce the
    identical dynamic-type/`about`-edge graph shape design doc §④
    describes."""
    headers = _login(client, "a00000802")
    nb = _mk_notebook(client, headers)
    table = _create_table(client, headers, nb)
    anchor_id, procedure_id, entity_id = (c["id"] for c in table["columns"])
    _add_row(client, headers, nb, table["id"], {
        anchor_id: "过冲问题", procedure_id: "先做A", entity_id: "示波器",
    })
    _add_row(client, headers, nb, table["id"], {
        anchor_id: "欠冲问题", procedure_id: "先做B", entity_id: "示波器",
    })
    detail = _poll_all_rows_settled(client, headers, nb, table["id"])
    assert all(r["projection_status"] == "synced" for r in detail["rows"]), detail

    types_resp = client.get(f"/api/notebooks/{nb}/knowledge-types", headers=headers)
    assert types_resp.status_code == 200, types_resp.text
    counts = {t["object_type"]: t["count"] for t in types_resp.json()}
    assert counts.get("现象") == 2   # anchor column: 2 distinct row-title values
    assert counts.get("方法") == 2   # procedure column: 2 distinct values
    assert counts.get("工具") == 1   # entity column: same tool both rows -> merges
    assert not ({"case", "procedure", "tool"} & set(counts))  # no PR-1 fixed vocabulary

    graph_resp = client.get(f"/api/notebooks/{nb}/graph", headers=headers)
    assert graph_resp.status_code == 200, graph_resp.text
    graph = graph_resp.json()
    node_types = {n["object_type"] for n in graph["nodes"]}
    assert {"现象", "方法", "工具"} <= node_types
    assert {e["relation"] for e in graph["edges"]} == {"about"}


# ---------------------------------------------------------------------------
# (d) discrimination set e2e, incl. no-anchor-table 400.
# ---------------------------------------------------------------------------


def test_d_discrimination_e2e_and_no_anchor_table_400(client, edited_env):
    headers = edited_env["headers"]
    nb = edited_env["nb"]
    table = edited_env["table"]
    row = edited_env["row"]
    procedure_id = edited_env["procedure_id"]

    resp = client.get(
        f"/api/agent/knowhow/tables/{table['id']}/discrimination", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["row_id"] == row["id"]
    assert body["rows"][0]["title"] == "过冲问题"
    assert [m["column_id"] for m in body["rows"][0]["methods"]] == [procedure_id]
    assert body["rows"][0]["methods"][0]["code_status"] == "none"

    # A second, anchorless ("记录型") table in the SAME notebook -> 400,
    # friendly Chinese message (design doc §⑥-2).
    anchorless = _create_table(client, headers, nb, title="记录表", anchor_index=None)
    resp2 = client.get(
        f"/api/agent/knowhow/tables/{anchorless['id']}/discrimination", headers=headers
    )
    assert resp2.status_code == 400
    assert resp2.json()["detail"] == "该表未设置行标题列，不支持判别集"


# ---------------------------------------------------------------------------
# (e) citation enrichment e2e: real projected element -> Citation.knowhow.
# ---------------------------------------------------------------------------


def test_e_citation_enrichment_e2e_with_real_projected_element(client, edited_env):
    """Distinct from test_knowhow_citation.py's own pure-unit coverage
    (fake `_SpySources`): this drives the REAL facade
    (`repo._citations_from`, one hop onto
    `EvidenceContextService.citations_from`) against a REAL element_id a
    real projection pass wrote to `source_elements`, proving the actual
    `SourceStore.evidence_elements` DB round-trip resolves
    `metadata.knowhow` correctly end-to-end, not just the pure-function
    parsing logic in isolation."""
    repo = client._repo
    nb = edited_env["nb"]
    table = edited_env["table"]
    row = edited_env["row"]
    anchor_id = edited_env["anchor_id"]
    hidden_source_id = edited_env["hidden_source_id"]

    with repo._connect() as db:
        element_row = db.execute(
            "SELECT id FROM source_elements WHERE "
            "json_extract(metadata,'$.knowhow.row_id')=? AND "
            "json_extract(metadata,'$.knowhow.column_id')=?",
            (row["id"], anchor_id),
        ).fetchone()
    assert element_row is not None
    element_id = element_row["id"]

    evidence = Evidence(
        source_id=hidden_source_id, source_title=table["title"],
        element_id=element_id, element_type="knowhow_cell",
        location_label="loc", quoted_span="过冲问题", confidence=1.0,
    )
    hit = RetrievedKnowledge(
        object_id="o1", object_type="claim", payload={}, evidence=[evidence], tier="personal",
    )

    citations = repo._citations_from([hit], {element_id}, "KG evidence", notebook_id=nb)

    assert len(citations) == 1
    assert citations[0].knowhow is not None
    assert citations[0].knowhow.table_id == table["id"]
    assert citations[0].knowhow.row_id == row["id"]


# ---------------------------------------------------------------------------
# (f) anchor set -> unset: KOs/edges torn down, chunks survive, 400s.
# ---------------------------------------------------------------------------


def test_f_anchor_unset_tears_down_kos_keeps_chunks_and_discrimination_400s(client):
    headers = _login(client, "a00000805")
    nb = _mk_notebook(client, headers)
    table = _create_table(client, headers, nb)  # anchor_index=0 by default
    anchor_id, procedure_id, entity_id = (c["id"] for c in table["columns"])
    _add_row(client, headers, nb, table["id"], {
        anchor_id: "过冲问题", procedure_id: "先做A", entity_id: "示波器",
    })
    detail = _poll_all_rows_settled(client, headers, nb, table["id"])
    assert all(r["projection_status"] == "synced" for r in detail["rows"]), detail
    hidden_source_id = detail["hidden_source_id"]
    repo = client._repo

    assert _ko_count(repo, hidden_source_id) > 0
    chunks_before = _chunk_count(repo, hidden_source_id)
    assert chunks_before > 0

    # Discrimination works while the table still has an anchor.
    assert client.get(
        f"/api/agent/knowhow/tables/{table['id']}/discrimination", headers=headers
    ).status_code == 200

    # Clear the anchor designation -> the table becomes "记录型" (anchorless).
    patch_resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=headers,
        json={"anchor_column_id": None},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    # A table-level anchor patch does NOT touch knowhow_rows.projection_status
    # (only update_knowhow_cell does — see test_knowhow_editing_api.py's own
    # _poll_ko_count_positive docstring for the identical trap) — poll the
    # actual outcome (KO count reaching zero), not row status.
    deadline = time.time() + 5.0
    while time.time() < deadline and _ko_count(repo, hidden_source_id) > 0:
        time.sleep(0.05)

    assert _ko_count(repo, hidden_source_id) == 0  # every KO/edge torn down
    assert _chunk_count(repo, hidden_source_id) == chunks_before  # chunks untouched

    resp = client.get(
        f"/api/agent/knowhow/tables/{table['id']}/discrimination", headers=headers
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "该表未设置行标题列，不支持判别集"
