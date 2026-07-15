# backend/tests/test_knowhow_api.py
"""knowhow-tables PR-1 Task 6: import/table read/delete/reproject HTTP API.

Mirrors test_notebook_assets.py's fixture convention (HTTP-level via
TestClient; register/login; a separate `repo` fixture sharing the same
on-disk DB for direct `add_member`/`create_knowhow_table` calls since there
is no HTTP "add member by id" endpoint) and test_kg_rebuild_relink_api.py's
poll-loop convention for observing a background daemon-thread job complete
(the import/reproject endpoints launch `KnowhowProjector.project_table` via
`background_jobs.submit`, fire-and-forget).
"""
from __future__ import annotations

import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


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
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    return TestClient(app)


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _mk_notebook(client, headers, name="N"):
    return client.post("/api/notebooks", json={"name": name}, headers=headers).json()["id"]


HEADER = ["违例类型", "现象识别", "根因分析", "修复方法", "依赖工具"]
# Per-column content KIND (PR-2+3 Task 3 wire — the row-title/anchor
# designation is no longer a per-column value; it's the SEPARATE
# ANCHOR_INDEX below). Column 0's own content kind is irrelevant when it's
# also the anchor (create_knowhow_table overwrites whichever position
# anchor_index names), but every position still needs SOME legal kind.
KINDS = ["attribute", "procedure", "procedure", "procedure", "entity"]
ANCHOR_INDEX = 0
DATA_ROWS = [
    ["过冲问题", "上升沿观察到过冲", "电源阻抗过高", "增加阻尼电阻", "示波器"],
    ["欠冲问题", "下降沿观察到欠冲", "寄生电感过大", "调整走线拓扑", "万用表"],
]


def _columns_json(header=HEADER, kinds=KINDS):
    return json.dumps([{"name": n, "kind": k} for n, k in zip(header, kinds)])


def _import_xlsx(client, headers, nb, *, header=HEADER, rows=None, title="时序修复表",
                 columns_json=None, anchor_index=ANCHOR_INDEX):
    if rows is None:
        rows = DATA_ROWS
    data = _xlsx_bytes(header, rows)
    form = {"title": title, "columns_json": columns_json or _columns_json(header)}
    if anchor_index is not None:
        form["anchor_index"] = str(anchor_index)
    return client.post(
        f"/api/notebooks/{nb}/knowhow/import",
        headers=headers,
        files={"file": ("rules.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data=form,
    )


def _poll_all_rows_settled(client, headers, nb, table_id, timeout=5.0):
    """Poll GET table detail until every row's projection_status leaves
    pending/syncing (settles at synced or failed), or the timeout elapses.
    Mirrors test_kg_rebuild_relink_api.py's deadline poll-loop convention."""
    deadline = time.time() + timeout
    detail = None
    while time.time() < deadline:
        detail = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=headers).json()
        rows = detail.get("rows", [])
        if rows and all(r["projection_status"] in ("synced", "failed") for r in rows):
            return detail
        time.sleep(0.05)
    return detail


# ---------------------------------------------------------------------------
# POST /notebooks/{nb}/knowhow/import/preview
# ---------------------------------------------------------------------------


def test_preview_xlsx_guesses_roles_and_returns_rows_preview(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000501")
    nb = _mk_notebook(client, owner_h)

    data = _xlsx_bytes(HEADER, DATA_ROWS)
    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/import/preview",
        headers=owner_h,
        files={"file": ("rules.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [c["name"] for c in body["columns"]] == HEADER
    # PR-2+3 Task 3 wire: guess_kinds' own output surfaces directly —
    # guessed_kind per column (违例类型 has no CONTENT-kind keyword hit, so
    # it's 'attribute') + a separate anchor_suggestion index (违例类型 hits
    # the anchor-NAME keyword 类型, at index 0).
    assert [c["guessed_kind"] for c in body["columns"]] == [
        "attribute", "procedure", "procedure", "procedure", "entity",
    ]
    assert body["anchor_suggestion"] == 0
    assert body["rows_preview"] == DATA_ROWS
    assert body["total_rows"] == len(DATA_ROWS)


def test_preview_grid_parse_error_passthrough_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000502")
    nb = _mk_notebook(client, owner_h)

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/import/preview",
        headers=owner_h,
        files={"file": ("rules.txt", b"not a grid", "text/plain")},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert any("一" <= ch <= "鿿" for ch in detail)  # friendly Chinese, not a raw dump


# ---------------------------------------------------------------------------
# POST /notebooks/{nb}/knowhow/import
# ---------------------------------------------------------------------------


def test_import_creates_table_with_full_detail_rows_and_cells(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000503")
    nb = _mk_notebook(client, owner_h)

    resp = _import_xlsx(client, owner_h, nb)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "时序修复表"
    assert [c["name"] for c in body["columns"]] == HEADER
    assert [c["kind"] for c in body["columns"]] == ["anchor"] + KINDS[1:]
    assert body["anchor_column_id"] == body["columns"][0]["id"]
    assert len(body["rows"]) == len(DATA_ROWS)

    name_to_col = {c["name"]: c["id"] for c in body["columns"]}
    for row, expected in zip(body["rows"], DATA_ROWS):
        assert row["cells"][name_to_col[HEADER[0]]] == expected[0]
        assert row["cells"][name_to_col[HEADER[4]]] == expected[4]
        assert row["projection_status"] in ("pending", "syncing", "synced")


def test_import_column_count_mismatch_returns_friendly_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000504")
    nb = _mk_notebook(client, owner_h)

    short_columns = json.dumps([{"name": n, "kind": k} for n, k in zip(HEADER[:3], KINDS[:3])])
    resp = _import_xlsx(client, owner_h, nb, columns_json=short_columns)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert any("一" <= ch <= "鿿" for ch in detail)


def test_import_illegal_kind_value_returns_friendly_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000505")
    nb = _mk_notebook(client, owner_h)

    bad_kinds = ["attribute", "procedure", "procedure", "procedure", "not_a_real_kind"]
    resp = _import_xlsx(client, owner_h, nb, columns_json=_columns_json(kinds=bad_kinds))
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert any("一" <= ch <= "鿿" for ch in detail)


def test_import_kind_anchor_value_rejected_400(tmp_path, monkeypatch):
    """PR-2+3 Task 3: 'anchor' is never a legal per-column KIND on the
    import wire — the row-title designation is expressed ONLY via the
    separate anchor_index form field. A client sending kind='anchor'
    directly (the old PR-1 per-column wire shape) gets the same friendly
    400 as any other illegal kind string, not silent acceptance."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000523")
    nb = _mk_notebook(client, owner_h)

    anchor_as_kind = ["anchor", "procedure", "procedure", "procedure", "entity"]
    resp = _import_xlsx(client, owner_h, nb, columns_json=_columns_json(kinds=anchor_as_kind))
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert any("一" <= ch <= "鿿" for ch in detail)


def test_import_missing_kind_defaults_to_attribute(tmp_path, monkeypatch):
    """PR-2+3 Task 3 (T1 review fold-in): a column entry that omits "kind"
    entirely defaults to 'attribute' rather than erroring — mirrors PR-1's
    own role-omission leniency, just with the new default value name."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000524")
    nb = _mk_notebook(client, owner_h)

    columns_json = json.dumps(
        [{"name": HEADER[0]}] + [{"name": n, "kind": k} for n, k in zip(HEADER[1:], KINDS[1:])]
    )
    resp = _import_xlsx(client, owner_h, nb, columns_json=columns_json, anchor_index=None)
    assert resp.status_code == 200, resp.text
    assert resp.json()["columns"][0]["kind"] == "attribute"


def test_import_without_anchor_column_succeeds(tmp_path, monkeypatch):
    """PR-2+3 Task 1: the PR-1 "exactly one concept column" rule is relaxed to
    at-most-one anchor — a record-shaped table with NO anchor column imports
    fine (it will only ever participate in retrieval, never the KG)."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000506")
    nb = _mk_notebook(client, owner_h)

    no_anchor_kinds = ["attribute", "procedure", "procedure", "procedure", "entity"]
    resp = _import_xlsx(
        client, owner_h, nb, columns_json=_columns_json(kinds=no_anchor_kinds), anchor_index=None,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [c["kind"] for c in body["columns"]] == no_anchor_kinds
    assert body["anchor_column_id"] is None


def test_import_empty_title_returns_friendly_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000508")
    nb = _mk_notebook(client, owner_h)

    # Valid grid + roles, but a whitespace-only title: create_knowhow_table's
    # ValueError must surface through routes.py's existing 400 idiom.
    resp = _import_xlsx(client, owner_h, nb, title="   ")
    assert resp.status_code == 400, resp.text
    assert "表标题不能为空" in resp.json()["detail"]


def test_import_grid_parse_error_passthrough_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000507")
    nb = _mk_notebook(client, owner_h)

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/import",
        headers=owner_h,
        files={"file": ("rules.txt", b"not a grid", "text/plain")},
        data={"title": "t", "columns_json": "[]"},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert any("一" <= ch <= "鿿" for ch in detail)


# ---------------------------------------------------------------------------
# Projection: background job eventually settles rows to synced + writes
# chunks/knowledge_objects that ask/reasoning retrieval already covers.
# ---------------------------------------------------------------------------


def test_projection_completes_synced_with_chunks_and_knowledge_objects(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000508")
    nb = _mk_notebook(client, owner_h)

    resp = _import_xlsx(client, owner_h, nb)
    table_id = resp.json()["id"]

    detail = _poll_all_rows_settled(client, owner_h, nb, table_id)
    assert detail is not None
    assert all(r["projection_status"] == "synced" for r in detail["rows"]), detail

    hidden_source_id = detail["hidden_source_id"]
    assert hidden_source_id

    with repo._connect() as db:
        chunk_count = db.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_id = ?", (hidden_source_id,)
        ).fetchone()["n"]
        object_count = db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_objects WHERE source_id = ?", (hidden_source_id,)
        ).fetchone()["n"]
    assert chunk_count > 0
    assert object_count > 0


# ---------------------------------------------------------------------------
# GET /notebooks/{nb}/knowhow, GET /notebooks/{nb}/knowhow/{table_id}
# ---------------------------------------------------------------------------


def test_list_and_get_table_detail(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000509")
    nb = _mk_notebook(client, owner_h)
    table_id = _import_xlsx(client, owner_h, nb).json()["id"]

    listed = client.get(f"/api/notebooks/{nb}/knowhow", headers=owner_h)
    assert listed.status_code == 200
    summaries = listed.json()
    assert len(summaries) == 1
    assert summaries[0]["id"] == table_id
    assert summaries[0]["row_count"] == len(DATA_ROWS)

    got = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h)
    assert got.status_code == 200
    assert got.json()["id"] == table_id


def test_get_unknown_table_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000510")
    nb = _mk_notebook(client, owner_h)
    assert client.get(f"/api/notebooks/{nb}/knowhow/no-such-table", headers=owner_h).status_code == 404


def test_cross_notebook_table_id_returns_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000511")
    nb1 = _mk_notebook(client, owner_h, name="N1")
    nb2 = _mk_notebook(client, owner_h, name="N2")
    table_id = _import_xlsx(client, owner_h, nb1).json()["id"]

    cross = client.get(f"/api/notebooks/{nb2}/knowhow/{table_id}", headers=owner_h)
    assert cross.status_code == 404


def test_cross_notebook_table_id_denies_delete_and_reproject_too(tmp_path, monkeypatch):
    """The owner has full write access to nb2 itself, but table_id belongs to
    nb1 — DELETE/reproject share get_table_in_notebook with the GET check
    above, so this proves the same 404 boundary holds for the write paths,
    not just read."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000521")
    nb1 = _mk_notebook(client, owner_h, name="N1")
    nb2 = _mk_notebook(client, owner_h, name="N2")
    table_id = _import_xlsx(client, owner_h, nb1).json()["id"]

    assert client.post(f"/api/notebooks/{nb2}/knowhow/{table_id}/reproject", headers=owner_h).status_code == 404
    assert client.delete(f"/api/notebooks/{nb2}/knowhow/{table_id}", headers=owner_h).status_code == 404
    # Still there under its real notebook, untouched by the failed attempts.
    assert client.get(f"/api/notebooks/{nb1}/knowhow/{table_id}", headers=owner_h).status_code == 200


# ---------------------------------------------------------------------------
# Access control: read-only member can GET, cannot POST/DELETE; stranger 404s.
# ---------------------------------------------------------------------------


def test_readonly_member_can_get_cannot_write(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000512")
    nb = _mk_notebook(client, owner_h)
    table_id = _import_xlsx(client, owner_h, nb).json()["id"]

    bob_h = _login(client, "b00000513")
    bob_id = client.get("/api/me", headers=bob_h).json()["id"]
    repo.add_member(nb, bob_id)

    assert client.get(f"/api/notebooks/{nb}/knowhow", headers=bob_h).status_code == 200
    assert client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=bob_h).status_code == 200

    data = _xlsx_bytes(HEADER, DATA_ROWS)
    preview = client.post(
        f"/api/notebooks/{nb}/knowhow/import/preview", headers=bob_h,
        files={"file": ("x.xlsx", data, "application/octet-stream")},
    )
    assert preview.status_code == 404

    assert _import_xlsx(client, bob_h, nb).status_code == 404
    assert client.post(f"/api/notebooks/{nb}/knowhow/{table_id}/reproject", headers=bob_h).status_code == 404
    assert client.delete(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=bob_h).status_code == 404


def test_stranger_gets_404_for_all_endpoints(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000514")
    nb = _mk_notebook(client, owner_h)
    table_id = _import_xlsx(client, owner_h, nb).json()["id"]

    stranger_h = _login(client, "c00000515")
    assert client.get(f"/api/notebooks/{nb}/knowhow", headers=stranger_h).status_code == 404
    assert client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=stranger_h).status_code == 404
    data = _xlsx_bytes(HEADER, DATA_ROWS)
    assert client.post(
        f"/api/notebooks/{nb}/knowhow/import/preview", headers=stranger_h,
        files={"file": ("x.xlsx", data, "application/octet-stream")},
    ).status_code == 404
    assert _import_xlsx(client, stranger_h, nb).status_code == 404
    assert client.post(f"/api/notebooks/{nb}/knowhow/{table_id}/reproject", headers=stranger_h).status_code == 404
    assert client.delete(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=stranger_h).status_code == 404


# ---------------------------------------------------------------------------
# DELETE /notebooks/{nb}/knowhow/{table_id}
# ---------------------------------------------------------------------------


def test_delete_never_projected_table_is_a_safe_noop_for_projection(tmp_path, monkeypatch, repo):
    """hidden_source_id is null when a table was created but never projected —
    the delete route must still succeed (task brief binding decision)."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000516")
    nb = _mk_notebook(client, owner_h)
    # Direct STORE-level call (bypasses the HTTP import wire entirely) — the
    # store's own create_knowhow_table contract is unchanged by the Task 3
    # wire flip: it still takes {name, role} dicts and still allows 'anchor'
    # inline as one of them (see knowhow_store.py's own documented exception).
    store_roles = ["anchor"] + KINDS[1:]
    columns = [{"name": n, "role": r} for n, r in zip(HEADER, store_roles)]
    table_id = repo.create_knowhow_table(nb, "空表", "", columns)

    resp = client.delete(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h)
    assert resp.status_code == 204, resp.text
    assert client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h).status_code == 404


def test_delete_cascades_projection_artifacts(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000517")
    nb = _mk_notebook(client, owner_h)
    table_id = _import_xlsx(client, owner_h, nb).json()["id"]
    detail = _poll_all_rows_settled(client, owner_h, nb, table_id)
    hidden_source_id = detail["hidden_source_id"]
    assert hidden_source_id

    resp = client.delete(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h)
    assert resp.status_code == 204, resp.text

    assert client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h).status_code == 404
    assert client.get(f"/api/notebooks/{nb}/knowhow", headers=owner_h).json() == []

    with repo._connect() as db:
        chunk_count = db.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_id = ?", (hidden_source_id,)
        ).fetchone()["n"]
        object_count = db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_objects WHERE source_id = ?", (hidden_source_id,)
        ).fetchone()["n"]
        source_row = db.execute(
            "SELECT id FROM sources WHERE id = ?", (hidden_source_id,)
        ).fetchone()
    assert chunk_count == 0
    assert object_count == 0
    assert source_row is None


def test_delete_unknown_table_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000518")
    nb = _mk_notebook(client, owner_h)
    assert client.delete(f"/api/notebooks/{nb}/knowhow/no-such-table", headers=owner_h).status_code == 404


# ---------------------------------------------------------------------------
# POST /notebooks/{nb}/knowhow/{table_id}/reproject
# ---------------------------------------------------------------------------


def test_reproject_responds_immediately_and_eventually_resyncs(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000519")
    nb = _mk_notebook(client, owner_h)
    table_id = _import_xlsx(client, owner_h, nb).json()["id"]
    _poll_all_rows_settled(client, owner_h, nb, table_id)

    resp = client.post(f"/api/notebooks/{nb}/knowhow/{table_id}/reproject", headers=owner_h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["table_id"] == table_id

    detail = _poll_all_rows_settled(client, owner_h, nb, table_id)
    assert all(r["projection_status"] == "synced" for r in detail["rows"]), detail


def test_reproject_unknown_table_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000520")
    nb = _mk_notebook(client, owner_h)
    assert client.post(f"/api/notebooks/{nb}/knowhow/no-such-table/reproject", headers=owner_h).status_code == 404
