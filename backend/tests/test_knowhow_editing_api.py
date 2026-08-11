# backend/tests/test_knowhow_editing_api.py
"""knowhow-tables PR-2+3 Task 3: editing API (table/column/row/cell CRUD),
the create-table wizard backend, and ProjectionScheduler (debounced,
single-flight background reprojection — every mutating endpoint routes
through it instead of a raw background_jobs.submit call).

Mirrors test_knowhow_api.py's fixture conventions (TestClient + register/
login; a separate `repo` fixture sharing the same on-disk DB for direct
`add_member`/`knowledge_objects` queries) and its poll-loop convention for
observing a background projection job settle.
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.services.knowhow.api import ProjectionScheduler


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    # Use the exact cached repository resolved by the API.  A second repository
    # pointed at the same SQLite file races route-owned projection timers and
    # has produced intermittent FK failures under CI load.
    from app.api.deps import repository
    return repository()


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


DEFAULT_COLUMNS = (("现象", "attribute"), ("方法", "procedure"), ("工具", "entity"))


def _create_table(
    client, headers, nb, *, title="判别表", columns=DEFAULT_COLUMNS, anchor_index=0,
):
    body: dict = {"title": title, "columns": [{"name": n, "kind": k} for n, k in columns]}
    if anchor_index is not None:
        body["anchor_index"] = anchor_index
    return client.post(f"/api/notebooks/{nb}/knowhow", headers=headers, json=body)


def _mk_table(client, headers, nb, **kwargs) -> dict:
    resp = _create_table(client, headers, nb, **kwargs)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _poll_all_rows_settled(client, headers, nb, table_id, timeout=5.0):
    """Mirrors test_knowhow_api.py's poll-loop: wait for every row's
    projection_status to leave pending/syncing (settle at synced/failed).
    Every mutation now goes through ProjectionScheduler's 0.5s debounce
    before the background job even starts, so the timeout has generous
    headroom above that."""
    deadline = time.time() + timeout
    detail = None
    while time.time() < deadline:
        detail = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=headers).json()
        rows = detail.get("rows", [])
        if rows and all(r["projection_status"] in ("synced", "failed") for r in rows):
            return detail
        time.sleep(0.05)
    return detail


def _ko_count(repo, hidden_source_id):
    if not hidden_source_id:
        return 0
    with repo._connect() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_objects WHERE source_id = ?",
            (hidden_source_id,),
        ).fetchone()["n"]


def _poll_ko_count_positive(repo, hidden_source_id, timeout=5.0):
    """Poll knowledge_objects count directly rather than row
    projection_status: a table-level change (e.g. an anchor_column_id patch)
    does NOT touch knowhow_rows.projection_status at all (only
    update_knowhow_cell does, as part of ITS OWN write transaction) — so
    immediately after such a PATCH, every row still reads 'synced' from the
    PRIOR projection, and a naive projection_status poll would return
    instantly, well before ProjectionScheduler's 0.5s-debounced follow-up
    run even starts. Polling the actual outcome (KO count) sidesteps that
    status-field timing gap entirely."""
    deadline = time.time() + timeout
    count = 0
    while time.time() < deadline:
        count = _ko_count(repo, hidden_source_id)
        if count > 0:
            return count
        time.sleep(0.05)
    return count


# ===========================================================================
# Create-table wizard: POST /notebooks/{nb}/knowhow
# ===========================================================================


def test_create_table_wizard_creates_empty_table_with_anchor(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001001")
    nb = _mk_notebook(client, owner_h)

    body = _mk_table(client, owner_h, nb)
    assert body["title"] == "判别表"
    assert body["rows"] == []
    assert [c["name"] for c in body["columns"]] == ["现象", "方法", "工具"]
    assert [c["kind"] for c in body["columns"]] == ["anchor", "procedure", "entity"]
    assert body["anchor_column_id"] == body["columns"][0]["id"]


def test_create_table_wizard_no_anchor_is_legal(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001002")
    nb = _mk_notebook(client, owner_h)

    body = _mk_table(client, owner_h, nb, anchor_index=None)
    assert body["anchor_column_id"] is None
    assert "anchor" not in [c["kind"] for c in body["columns"]]


def test_create_table_wizard_empty_title_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001003")
    nb = _mk_notebook(client, owner_h)

    resp = _create_table(client, owner_h, nb, title="   ")
    assert resp.status_code == 400, resp.text
    assert "表标题不能为空" in resp.json()["detail"]


def test_create_table_wizard_illegal_kind_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001004")
    nb = _mk_notebook(client, owner_h)

    resp = _create_table(
        client, owner_h, nb,
        columns=(("现象", "attribute"), ("方法", "not_a_kind")),
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert any("一" <= ch <= "鿿" for ch in detail)


def test_create_table_wizard_kind_anchor_value_rejected_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001008")
    nb = _mk_notebook(client, owner_h)

    resp = _create_table(client, owner_h, nb, columns=(("现象", "anchor"),), anchor_index=None)
    assert resp.status_code == 400, resp.text


def test_create_table_wizard_missing_kind_defaults_to_attribute(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001005")
    nb = _mk_notebook(client, owner_h)

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow", headers=owner_h,
        json={"title": "t", "columns": [{"name": "备注"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["columns"][0]["kind"] == "attribute"


def test_create_table_wizard_anchor_index_out_of_range_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001009")
    nb = _mk_notebook(client, owner_h)

    resp = _create_table(client, owner_h, nb, anchor_index=99)
    assert resp.status_code == 400, resp.text


def test_create_table_wizard_reader_cannot_create(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001006")
    nb = _mk_notebook(client, owner_h)
    bob_h = _login(client, "b00001006")
    bob_id = client.get("/api/me", headers=bob_h).json()["id"]
    repo.add_member(nb, bob_id)

    assert _create_table(client, bob_h, nb).status_code == 404


def test_create_table_wizard_stranger_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001007")
    nb = _mk_notebook(client, owner_h)
    stranger_h = _login(client, "c00001007")

    assert _create_table(client, stranger_h, nb).status_code == 404


# ===========================================================================
# PATCH /notebooks/{nb}/knowhow/{t}
# ===========================================================================


def test_patch_table_title_and_description(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001010")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=owner_h,
        json={"title": "新标题", "description": "新描述"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "新标题"
    assert body["description"] == "新描述"
    # Unset fields (anchor_column_id was omitted) are untouched.
    assert body["anchor_column_id"] == table["anchor_column_id"]


def test_patch_table_empty_title_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001011")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=owner_h,
        json={"title": "   "},
    )
    assert resp.status_code == 400, resp.text
    assert "表标题不能为空" in resp.json()["detail"]


def test_patch_table_anchor_column_id_explicit_null_clears(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001012")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    assert table["anchor_column_id"] is not None

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=owner_h,
        json={"anchor_column_id": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["anchor_column_id"] is None
    # The previously-anchor column demotes to attribute (store contract).
    assert resp.json()["columns"][0]["kind"] == "attribute"


def test_patch_table_anchor_column_id_moves_to_new_column(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001013")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    new_anchor = table["columns"][1]["id"]

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=owner_h,
        json={"anchor_column_id": new_anchor},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["anchor_column_id"] == new_anchor
    assert body["columns"][0]["kind"] == "attribute"  # demoted
    assert body["columns"][1]["kind"] == "anchor"


def test_patch_table_anchor_column_not_in_table_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001014")
    nb = _mk_notebook(client, owner_h)
    table1 = _mk_table(client, owner_h, nb, title="表一")
    table2 = _mk_table(client, owner_h, nb, title="表二")
    foreign_column_id = table2["columns"][0]["id"]

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table1['id']}", headers=owner_h,
        json={"anchor_column_id": foreign_column_id},
    )
    assert resp.status_code == 400, resp.text
    assert "不属于本表" in resp.json()["detail"]


def test_patch_table_cross_notebook_table_id_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001015")
    nb1 = _mk_notebook(client, owner_h, name="N1")
    nb2 = _mk_notebook(client, owner_h, name="N2")
    table = _mk_table(client, owner_h, nb1)

    resp = client.patch(
        f"/api/notebooks/{nb2}/knowhow/{table['id']}", headers=owner_h,
        json={"title": "x"},
    )
    assert resp.status_code == 404


def test_patch_table_unknown_table_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001016")
    nb = _mk_notebook(client, owner_h)

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/no-such-table", headers=owner_h, json={"title": "x"},
    )
    assert resp.status_code == 404


def test_patch_table_reader_404(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001017")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    bob_h = _login(client, "b00001017")
    bob_id = client.get("/api/me", headers=bob_h).json()["id"]
    repo.add_member(nb, bob_id)

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=bob_h, json={"title": "x"},
    )
    assert resp.status_code == 404


# ===========================================================================
# POST/PATCH/DELETE /notebooks/{nb}/knowhow/{t}/columns[/{col}]
# ===========================================================================


def test_add_column_success(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001020")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/columns", headers=owner_h,
        json={"name": "根因分析", "kind": "procedure"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "根因分析"
    assert body["kind"] == "procedure"
    assert body["position"] == 3  # appended after the 3 wizard columns


def test_add_column_explicit_position(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001021")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/columns", headers=owner_h,
        json={"name": "备注", "kind": "attribute", "position": 0},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["position"] == 0


def test_add_column_illegal_kind_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001022")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/columns", headers=owner_h,
        json={"name": "x", "kind": "bogus"},
    )
    assert resp.status_code == 400, resp.text


def test_add_column_kind_anchor_rejected_400(tmp_path, monkeypatch):
    """anchor is a table-level designation (set_knowhow_anchor_column only);
    add_knowhow_column's own store guard rejects it as a column kind."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001023")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/columns", headers=owner_h,
        json={"name": "x", "kind": "anchor"},
    )
    assert resp.status_code == 400, resp.text


def test_add_column_duplicate_name_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001024")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/columns", headers=owner_h,
        json={"name": "现象", "kind": "attribute"},
    )
    assert resp.status_code == 400, resp.text


def test_patch_column_rename_and_change_kind(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001030")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    column_id = table["columns"][1]["id"]

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/columns/{column_id}", headers=owner_h,
        json={"name": "分析方法", "kind": "attribute"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "分析方法"
    assert body["kind"] == "attribute"


def test_patch_column_illegal_kind_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001031")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    column_id = table["columns"][1]["id"]

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/columns/{column_id}", headers=owner_h,
        json={"kind": "bogus"},
    )
    assert resp.status_code == 400, resp.text


def test_patch_column_not_in_table_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001032")
    nb = _mk_notebook(client, owner_h)
    table1 = _mk_table(client, owner_h, nb, title="表一")
    table2 = _mk_table(client, owner_h, nb, title="表二")
    foreign_column_id = table2["columns"][0]["id"]

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table1['id']}/columns/{foreign_column_id}",
        headers=owner_h, json={"name": "x"},
    )
    assert resp.status_code == 404


def test_delete_column_success(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001040")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    column_id = table["columns"][2]["id"]

    resp = client.delete(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/columns/{column_id}", headers=owner_h,
    )
    assert resp.status_code == 204, resp.text
    updated = client.get(f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=owner_h).json()
    assert column_id not in {c["id"] for c in updated["columns"]}


def test_delete_column_not_in_table_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001041")
    nb = _mk_notebook(client, owner_h)
    table1 = _mk_table(client, owner_h, nb, title="表一")
    table2 = _mk_table(client, owner_h, nb, title="表二")
    foreign_column_id = table2["columns"][0]["id"]

    resp = client.delete(
        f"/api/notebooks/{nb}/knowhow/{table1['id']}/columns/{foreign_column_id}",
        headers=owner_h,
    )
    assert resp.status_code == 404


# ===========================================================================
# POST/DELETE /notebooks/{nb}/knowhow/{t}/rows[/{row}]
# ===========================================================================


def test_add_row_success_with_cells(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001050")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    anchor_col = table["columns"][0]["id"]

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows", headers=owner_h,
        json={"cells": {anchor_col: "过冲问题"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cells"] == {anchor_col: "过冲问题"}
    assert body["position"] == 0
    assert body["projection_status"] in ("pending", "syncing", "synced")


def test_add_row_unknown_column_in_cells_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001051")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows", headers=owner_h,
        json={"cells": {"no-such-column": "x"}},
    )
    assert resp.status_code == 400, resp.text
    assert "不属于本表" in resp.json()["detail"]


def test_delete_row_success(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001060")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    row = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows", headers=owner_h, json={"cells": {}},
    ).json()

    resp = client.delete(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows/{row['id']}", headers=owner_h,
    )
    assert resp.status_code == 204, resp.text
    updated = client.get(f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=owner_h).json()
    assert updated["rows"] == []


def test_delete_row_not_in_table_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001061")
    nb = _mk_notebook(client, owner_h)
    table1 = _mk_table(client, owner_h, nb, title="表一")
    table2 = _mk_table(client, owner_h, nb, title="表二")
    foreign_row = client.post(
        f"/api/notebooks/{nb}/knowhow/{table2['id']}/rows", headers=owner_h, json={"cells": {}},
    ).json()

    resp = client.delete(
        f"/api/notebooks/{nb}/knowhow/{table1['id']}/rows/{foreign_row['id']}", headers=owner_h,
    )
    assert resp.status_code == 404


# ===========================================================================
# PATCH /notebooks/{nb}/knowhow/{t}/rows/{row}/cells/{col}
# ===========================================================================


def test_patch_cell_success(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001070")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    column_id = table["columns"][1]["id"]
    row = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows", headers=owner_h, json={"cells": {}},
    ).json()

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows/{row['id']}/cells/{column_id}",
        headers=owner_h, json={"content_md": "增加阻尼电阻"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "row_id": row["id"],
        "column_id": column_id,
        "content_md": "增加阻尼电阻",
        "projection_status": body["projection_status"],
    }
    assert body["projection_status"] in ("pending", "syncing", "synced")


def test_patch_cell_row_from_another_table_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001071")
    nb = _mk_notebook(client, owner_h)
    table1 = _mk_table(client, owner_h, nb, title="表一")
    table2 = _mk_table(client, owner_h, nb, title="表二")
    column1 = table1["columns"][0]["id"]
    foreign_row = client.post(
        f"/api/notebooks/{nb}/knowhow/{table2['id']}/rows", headers=owner_h, json={"cells": {}},
    ).json()

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table1['id']}/rows/{foreign_row['id']}/cells/{column1}",
        headers=owner_h, json={"content_md": "x"},
    )
    assert resp.status_code == 400, resp.text
    assert "格子定位不合法" in resp.json()["detail"]


def test_patch_cell_column_from_another_table_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001072")
    nb = _mk_notebook(client, owner_h)
    table1 = _mk_table(client, owner_h, nb, title="表一")
    table2 = _mk_table(client, owner_h, nb, title="表二")
    foreign_column = table2["columns"][0]["id"]
    row1 = client.post(
        f"/api/notebooks/{nb}/knowhow/{table1['id']}/rows", headers=owner_h, json={"cells": {}},
    ).json()

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table1['id']}/rows/{row1['id']}/cells/{foreign_column}",
        headers=owner_h, json={"content_md": "x"},
    )
    assert resp.status_code == 400, resp.text
    assert "格子定位不合法" in resp.json()["detail"]


def test_patch_cell_unknown_row_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001073")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    column_id = table["columns"][0]["id"]

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows/no-such-row/cells/{column_id}",
        headers=owner_h, json={"content_md": "x"},
    )
    assert resp.status_code == 400, resp.text


# ===========================================================================
# PATCH /notebooks/{nb}/knowhow/{t}/cells (followup A: batch cell write, ONE
# DB transaction — anchor-grouping spec §6「整组批量写单事务，不半改」.
# Replaces the frontend's best-effort Promise.all of N independent
# patch_knowhow_cell PATCHes for a merged/shared cell.)
# ===========================================================================


def test_patch_cells_batch_success(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001074")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    column_id = table["columns"][1]["id"]
    row_ids = [
        client.post(
            f"/api/notebooks/{nb}/knowhow/{table['id']}/rows", headers=owner_h, json={"cells": {}},
        ).json()["id"]
        for _ in range(3)
    ]

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/cells",
        headers=owner_h,
        json={"column_id": column_id, "row_ids": row_ids, "content_md": "统一改法"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {item["row_id"] for item in body} == set(row_ids)
    for item in body:
        assert item["column_id"] == column_id
        assert item["content_md"] == "统一改法"
        assert item["projection_status"] in ("pending", "syncing", "synced")

    # 落库确实全写了，不只是响应体里说写了。
    detail = client.get(f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=owner_h).json()
    written = {row["id"]: row["cells"].get(column_id) for row in detail["rows"]}
    for row_id in row_ids:
        assert written[row_id] == "统一改法"


def test_patch_cells_batch_one_bad_row_400_writes_nothing_not_even_the_good_rows(
    tmp_path, monkeypatch
):
    """事务原子性的端点级证据：批里混了一个不属于本表的 row_id，整请求 400，
    且批里其余合法的 row 一个字都没落库——不是"坏的那个不写、好的照写"的半改。"""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001075")
    nb = _mk_notebook(client, owner_h)
    table1 = _mk_table(client, owner_h, nb, title="表一")
    table2 = _mk_table(client, owner_h, nb, title="表二")
    column1 = table1["columns"][1]["id"]
    good_row_ids = [
        client.post(
            f"/api/notebooks/{nb}/knowhow/{table1['id']}/rows", headers=owner_h, json={"cells": {}},
        ).json()["id"]
        for _ in range(2)
    ]
    foreign_row = client.post(
        f"/api/notebooks/{nb}/knowhow/{table2['id']}/rows", headers=owner_h, json={"cells": {}},
    ).json()

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table1['id']}/cells",
        headers=owner_h,
        json={
            "column_id": column1,
            "row_ids": [*good_row_ids, foreign_row["id"]],
            "content_md": "不该落库",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "格子定位不合法" in resp.json()["detail"]

    detail = client.get(f"/api/notebooks/{nb}/knowhow/{table1['id']}", headers=owner_h).json()
    written = {row["id"]: row["cells"].get(column1, "") for row in detail["rows"]}
    for row_id in good_row_ids:
        assert written[row_id] == ""


def test_patch_cells_batch_unknown_row_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001076")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    column_id = table["columns"][0]["id"]
    row_id = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows", headers=owner_h, json={"cells": {}},
    ).json()["id"]

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/cells",
        headers=owner_h,
        json={"column_id": column_id, "row_ids": [row_id, "no-such-row"], "content_md": "x"},
    )
    assert resp.status_code == 400, resp.text


def test_patch_cells_batch_column_from_another_table_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001077")
    nb = _mk_notebook(client, owner_h)
    table1 = _mk_table(client, owner_h, nb, title="表一")
    table2 = _mk_table(client, owner_h, nb, title="表二")
    foreign_column = table2["columns"][0]["id"]
    row1_id = client.post(
        f"/api/notebooks/{nb}/knowhow/{table1['id']}/rows", headers=owner_h, json={"cells": {}},
    ).json()["id"]

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table1['id']}/cells",
        headers=owner_h,
        json={"column_id": foreign_column, "row_ids": [row1_id], "content_md": "x"},
    )
    assert resp.status_code == 400, resp.text
    assert "格子定位不合法" in resp.json()["detail"]


def test_patch_cells_batch_empty_row_ids_is_a_noop(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001078")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    column_id = table["columns"][0]["id"]

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/cells",
        headers=owner_h,
        json={"column_id": column_id, "row_ids": [], "content_md": "x"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# ===========================================================================
# Reader / stranger matrix across the whole editing surface
# ===========================================================================


def _tiny_xlsx_bytes() -> bytes:
    """Minimal one-column xlsx for the append permission checks below — the
    write guard (the require_notebook_access dependency) rejects before the
    handler ever parses the file, so the payload only needs to be
    structurally valid, not meaningful."""
    import io

    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["现象"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_readonly_member_gets_404_on_every_mutating_endpoint(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001080")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    column_id = table["columns"][1]["id"]
    row = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows", headers=owner_h, json={"cells": {}},
    ).json()

    bob_h = _login(client, "b00001080")
    bob_id = client.get("/api/me", headers=bob_h).json()["id"]
    repo.add_member(nb, bob_id)

    t = table["id"]
    assert client.patch(f"/api/notebooks/{nb}/knowhow/{t}", headers=bob_h, json={"title": "x"}).status_code == 404
    assert client.post(f"/api/notebooks/{nb}/knowhow/{t}/columns", headers=bob_h, json={"name": "x", "kind": "attribute"}).status_code == 404
    assert client.patch(f"/api/notebooks/{nb}/knowhow/{t}/columns/{column_id}", headers=bob_h, json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/notebooks/{nb}/knowhow/{t}/columns/{column_id}", headers=bob_h).status_code == 404
    assert client.post(f"/api/notebooks/{nb}/knowhow/{t}/rows", headers=bob_h, json={"cells": {}}).status_code == 404
    assert client.delete(f"/api/notebooks/{nb}/knowhow/{t}/rows/{row['id']}", headers=bob_h).status_code == 404
    assert client.patch(
        f"/api/notebooks/{nb}/knowhow/{t}/rows/{row['id']}/cells/{column_id}",
        headers=bob_h, json={"content_md": "x"},
    ).status_code == 404
    # Reads still succeed for the read-only member.
    assert client.get(f"/api/notebooks/{nb}/knowhow/{t}", headers=bob_h).status_code == 200

    # --- T15 permission-matrix extension (PR-2/3 safety net): T6/T8/T10 ---
    # endpoints were never added to this matrix when their own tasks landed.
    # Extending, never weakening, the existing assertions above.

    # T6 template download is a READ endpoint (require_notebook_read) — the
    # read-only member succeeds exactly like the table GET above.
    assert client.get(f"/api/notebooks/{nb}/knowhow/{t}/template", headers=bob_h).status_code == 200

    # T6 append is write-gated (require_notebook_access) regardless of mode —
    # the dependency runs before the handler even reads the `mode` field.
    for mode in ("preview", "commit"):
        resp = client.post(
            f"/api/notebooks/{nb}/knowhow/{t}/append", headers=bob_h,
            files={"file": ("x.xlsx", _tiny_xlsx_bytes(),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"mode": mode},
        )
        assert resp.status_code == 404, (mode, resp.text)

    # T8 optimize is write-gated too (an authoring affordance, not a read).
    assert client.post(
        f"/api/notebooks/{nb}/knowhow/{t}/rows/{row['id']}/cells/{column_id}/optimize",
        headers=bob_h,
    ).status_code == 404

    # T10 agent-surface code-attachment writes, reached via SESSION auth
    # (not an agent token) — a read-only member is exactly a "reader", so
    # the write=True/"knowhow:code" scope check 404s the same way every
    # other write does here (GET on this same URL stays 200 for a reader —
    # proven separately by test_knowhow_agent_api.py's own Group B coverage,
    # not re-asserted here to keep this matrix focused on WRITES).
    code_url = f"/api/agent/knowhow/rows/{row['id']}/cells/{column_id}/code"
    assert client.put(
        code_url, headers=bob_h, json={"code_text": "x", "language": ""}
    ).status_code == 404
    assert client.delete(code_url, headers=bob_h).status_code == 404


def test_stranger_gets_404_on_every_mutating_endpoint(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001081")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb)
    column_id = table["columns"][1]["id"]
    row = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows", headers=owner_h, json={"cells": {}},
    ).json()

    stranger_h = _login(client, "c00001081")
    t = table["id"]
    assert client.patch(f"/api/notebooks/{nb}/knowhow/{t}", headers=stranger_h, json={"title": "x"}).status_code == 404
    assert client.post(f"/api/notebooks/{nb}/knowhow/{t}/columns", headers=stranger_h, json={"name": "x", "kind": "attribute"}).status_code == 404
    assert client.delete(f"/api/notebooks/{nb}/knowhow/{t}/columns/{column_id}", headers=stranger_h).status_code == 404
    assert client.get(f"/api/notebooks/{nb}/knowhow/{t}", headers=stranger_h).status_code == 404

    # --- T15 permission-matrix extension (PR-2/3 safety net), mirroring the
    # read-only-member matrix above.
    assert client.get(f"/api/notebooks/{nb}/knowhow/{t}/template", headers=stranger_h).status_code == 404
    for mode in ("preview", "commit"):
        resp = client.post(
            f"/api/notebooks/{nb}/knowhow/{t}/append", headers=stranger_h,
            files={"file": ("x.xlsx", _tiny_xlsx_bytes(),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"mode": mode},
        )
        assert resp.status_code == 404, (mode, resp.text)
    assert client.post(
        f"/api/notebooks/{nb}/knowhow/{t}/rows/{row['id']}/cells/{column_id}/optimize",
        headers=stranger_h,
    ).status_code == 404
    code_url = f"/api/agent/knowhow/rows/{row['id']}/cells/{column_id}/code"
    assert client.put(
        code_url, headers=stranger_h, json={"code_text": "x", "language": ""}
    ).status_code == 404
    assert client.delete(code_url, headers=stranger_h).status_code == 404


# ===========================================================================
# Integration: scheduler actually drives KnowhowProjector via the real HTTP
# endpoints (poll-based — the fast, isolated coalescing assertion is below).
# ===========================================================================


def test_editing_a_table_with_no_anchor_column_stays_zero_ko(tmp_path, monkeypatch, repo):
    """Task brief TDD checklist: "无行标题表编辑后仍零 KO" — editing a
    record-shaped (anchorless) table through the new endpoints must still
    project zero knowledge_objects (only chunks), proving the scheduler
    correctly drives project_table for anchorless tables too, not just on
    initial import."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001090")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb, anchor_index=None)
    column_id = table["columns"][0]["id"]

    row = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows", headers=owner_h, json={"cells": {}},
    ).json()
    patch = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows/{row['id']}/cells/{column_id}",
        headers=owner_h, json={"content_md": "2026-07-15 出发"},
    )
    assert patch.status_code == 200, patch.text

    detail = _poll_all_rows_settled(client, owner_h, nb, table["id"])
    assert detail is not None
    assert all(r["projection_status"] == "synced" for r in detail["rows"]), detail
    assert _ko_count(repo, detail["hidden_source_id"]) == 0


def test_setting_anchor_column_schedules_reprojection_that_creates_kos(tmp_path, monkeypatch, repo):
    """Task brief TDD checklist: "anchor 变更触发重投影" — a table created
    WITHOUT an anchor (zero KOs once its one cell is filled and settled),
    then PATCHed to designate an anchor column, ends up with knowledge_objects
    > 0 once the scheduler's follow-up reprojection settles."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001091")
    nb = _mk_notebook(client, owner_h)
    table = _mk_table(client, owner_h, nb, anchor_index=None)
    column_id = table["columns"][0]["id"]

    row = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows", headers=owner_h, json={"cells": {}},
    ).json()
    client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows/{row['id']}/cells/{column_id}",
        headers=owner_h, json={"content_md": "过冲问题"},
    )
    before = _poll_all_rows_settled(client, owner_h, nb, table["id"])
    assert _ko_count(repo, before["hidden_source_id"]) == 0

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}", headers=owner_h,
        json={"anchor_column_id": column_id},
    )
    assert resp.status_code == 200, resp.text
    hidden_source_id = resp.json()["hidden_source_id"]

    assert _poll_ko_count_positive(repo, hidden_source_id) > 0


# ===========================================================================
# ProjectionScheduler: unit-level coalescing (fake project_fn, tiny debounce
# so the test runs fast and deterministically rather than waiting out the
# production 0.5s window).
# ===========================================================================


def test_scheduler_coalesces_rapid_schedule_calls_to_at_most_two_runs():
    """Task brief: "连续三次 PATCH 合并为≤2 次 project_table". Exercised here
    directly against ProjectionScheduler (constructed with a fake, counting
    project_fn, a synchronous fake submit, and a short debounce) rather than
    through three real HTTP round trips — deterministic and fast, and tests
    the scheduler's own coalescing contract in isolation from the HTTP layer
    (which is covered separately above via the poll-based integration
    tests)."""
    calls = []
    call_lock = threading.Lock()

    def fake_project(table_id):
        with call_lock:
            calls.append(table_id)

    def synchronous_submit(fn, *args, **kwargs):
        # Runs inline instead of spawning a real background thread — still
        # exercises the exact same _fire/_run state machine, just without
        # depending on OS thread scheduling for the assertion to be stable.
        fn(*args)

    scheduler = ProjectionScheduler(
        fake_project, debounce_seconds=0.05, submit=synchronous_submit,
    )

    scheduler.schedule("t1")
    scheduler.schedule("t1")
    scheduler.schedule("t1")
    time.sleep(0.3)  # comfortably past the 0.05s debounce window

    assert len(calls) <= 2
    assert len(calls) >= 1
    assert set(calls) == {"t1"}


def test_scheduler_running_flag_defers_concurrent_schedule_to_one_rerun():
    """A schedule() call that arrives WHILE a run is already in flight must
    not launch a second CONCURRENT run — it sets the rerun flag, and exactly
    one extra run happens after the in-flight one finishes."""
    calls = []
    call_lock = threading.Lock()
    release = threading.Event()

    def blocking_project(table_id):
        with call_lock:
            calls.append(table_id)
        # First call blocks until the test explicitly releases it, giving a
        # deterministic window to call schedule() again "while running".
        if len(calls) == 1:
            release.wait(timeout=2.0)

    def real_submit(fn, *args, **kwargs):
        threading.Thread(target=fn, args=args, daemon=True).start()

    scheduler = ProjectionScheduler(
        blocking_project, debounce_seconds=0.01, submit=real_submit,
    )

    scheduler.schedule("t1")
    time.sleep(0.1)  # let the debounce fire and the first run start + block
    with call_lock:
        assert len(calls) == 1

    scheduler.schedule("t1")  # arrives while running -> sets rerun, no 2nd run yet
    time.sleep(0.1)
    with call_lock:
        assert len(calls) == 1  # still just the one in-flight run

    release.set()  # let the first run finish -> its finally should fire the rerun
    deadline = time.time() + 2.0
    while time.time() < deadline:
        with call_lock:
            if len(calls) >= 2:
                break
        time.sleep(0.02)

    with call_lock:
        assert len(calls) == 2


def test_scheduler_different_tables_are_independent():
    calls = []
    call_lock = threading.Lock()

    def fake_project(table_id):
        with call_lock:
            calls.append(table_id)

    def synchronous_submit(fn, *args, **kwargs):
        fn(*args)

    scheduler = ProjectionScheduler(
        fake_project, debounce_seconds=0.02, submit=synchronous_submit,
    )
    scheduler.schedule("t1")
    scheduler.schedule("t2")
    time.sleep(0.2)

    assert set(calls) == {"t1", "t2"}
