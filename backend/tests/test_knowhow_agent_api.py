# backend/tests/test_knowhow_agent_api.py
"""knowhow-tables PR-2+3 Task 10: Agent HTTP surface (design doc §⑥) — table
list / discrimination set / row detail / cell-level code attachments,
reachable by EITHER a signed-in session or an Agent Bearer token carrying the
``knowledge:read`` (reads) / ``knowhow:code`` (code writes) scope.

Three groups:
  - Group A: pure unit tests for ``app.services.knowhow.api``'s new pure
    functions (no HTTP, no DB) — code_status three-state derivation,
    discrimination/row-detail shape, no-anchor ValueError, title synthesis.
  - Group B: HTTP session-based tests (``TestClient`` + register/login +
    create-table-wizard conventions, mirrors test_knowhow_optimize.py's own
    Group B) — owner/reader/stranger auth matrix, discrimination 400, code
    CRUD + freshness after a cell edit.
  - Group C: HTTP Agent-Bearer-token tests (mirrors test_memory_mcp.py's
    ``mcp_env`` token-issuing conventions, driven over plain HTTP instead of
    the MCP transport) — scope hit/missing, notebook not in the token's
    allowlist, expired/revoked/malformed token.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.services.knowhow import api as knowhow_api
from app.services.knowhow import textops


# ===========================================================================
# Group A: pure unit tests (no HTTP, no DB)
# ===========================================================================


def _wire_table(*, anchor_column_id=None, columns=(), rows=()):
    return {
        "id": "kht-1", "title": "判别表", "description": "",
        "anchor_column_id": anchor_column_id,
        "columns": list(columns), "rows": list(rows),
    }


def test_cell_content_hash_matches_strip_images_then_strip_then_sha256():
    text = "见下图 ![说明](asset://x) 完成  \n"
    expected = hashlib.sha256(
        textops.strip_images(text).strip().encode("utf-8")
    ).hexdigest()
    assert knowhow_api.cell_content_hash(text) == expected


def test_cell_content_hash_stable_across_trailing_whitespace_only_edits():
    assert knowhow_api.cell_content_hash("先做A\n") == knowhow_api.cell_content_hash("先做A")


def test_build_discrimination_set_raises_without_anchor_column():
    table = _wire_table(anchor_column_id=None, columns=[
        {"id": "c1", "name": "方法", "kind": "procedure", "position": 0},
    ], rows=[])

    with pytest.raises(ValueError, match="行标题列"):
        knowhow_api.build_discrimination_set(table, [])


def test_build_discrimination_set_methods_are_procedure_columns_in_order():
    columns = [
        {"id": "anchor", "name": "现象", "kind": "anchor", "position": 0},
        {"id": "m1", "name": "识别", "kind": "procedure", "position": 1},
        {"id": "tool", "name": "工具", "kind": "entity", "position": 2},
        {"id": "m2", "name": "修复", "kind": "procedure", "position": 3},
    ]
    rows = [{
        "id": "r1", "position": 0,
        "cells": {
            "anchor": "过冲振铃", "m1": "1. 看眼图\n2. 测阻抗",
            "tool": "示波器", "m2": "加端接电阻",
        },
    }]
    table = _wire_table(anchor_column_id="anchor", columns=columns, rows=rows)

    result = knowhow_api.build_discrimination_set(table, [])

    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["row_id"] == "r1"
    assert row["title"] == "过冲振铃"
    assert [m["column_id"] for m in row["methods"]] == ["m1", "m2"]
    assert row["methods"][0]["column_name"] == "识别"
    assert row["methods"][0]["text"] == "1. 看眼图\n2. 测阻抗"
    assert all(m["code_status"] == "none" for m in row["methods"])


def test_build_discrimination_set_code_status_reflects_attachment_freshness():
    columns = [
        {"id": "anchor", "name": "现象", "kind": "anchor", "position": 0},
        {"id": "m1", "name": "识别", "kind": "procedure", "position": 1},
    ]
    rows = [{"id": "r1", "position": 0, "cells": {"anchor": "X", "m1": "先做A"}}]
    table = _wire_table(anchor_column_id="anchor", columns=columns, rows=rows)
    fresh_hash = knowhow_api.cell_content_hash("先做A")

    implemented = knowhow_api.build_discrimination_set(
        table, [{"row_id": "r1", "column_id": "m1", "cell_content_hash": fresh_hash}]
    )
    assert implemented["rows"][0]["methods"][0]["code_status"] == "implemented"

    stale = knowhow_api.build_discrimination_set(
        table, [{"row_id": "r1", "column_id": "m1", "cell_content_hash": "deadbeef"}]
    )
    assert stale["rows"][0]["methods"][0]["code_status"] == "stale"


def test_build_row_detail_includes_steps_for_procedure_and_items_for_entity():
    columns = [
        {"id": "anchor", "name": "现象", "kind": "anchor", "position": 0},
        {"id": "m1", "name": "识别", "kind": "procedure", "position": 1},
        {"id": "t1", "name": "工具", "kind": "entity", "position": 2},
        {"id": "a1", "name": "备注", "kind": "attribute", "position": 3},
    ]
    rows = [{
        "id": "r1", "position": 0,
        "cells": {
            "anchor": "过冲振铃", "m1": "1. 看眼图\n2. 测阻抗",
            "t1": "示波器\n万用表", "a1": "普通文本",
        },
    }]
    table = _wire_table(anchor_column_id="anchor", columns=columns, rows=rows)

    detail = knowhow_api.build_row_detail(table, "r1", [])

    assert detail["title"] == "过冲振铃"
    by_id = {cell["column_id"]: cell for cell in detail["cells"]}
    assert by_id["m1"]["steps"] == ["看眼图", "测阻抗"]
    assert by_id["t1"]["items"] == ["示波器", "万用表"]
    assert "steps" not in by_id["a1"]
    assert "items" not in by_id["a1"]
    assert detail["code"] == []


def test_build_row_detail_only_lists_code_for_this_row_and_marks_stale():
    columns = [
        {"id": "anchor", "name": "现象", "kind": "anchor", "position": 0},
        {"id": "m1", "name": "识别", "kind": "procedure", "position": 1},
    ]
    rows = [{"id": "r1", "position": 0, "cells": {"anchor": "X", "m1": "先做A"}}]
    table = _wire_table(anchor_column_id="anchor", columns=columns, rows=rows)
    attachments = [
        {
            "row_id": "r1", "column_id": "m1", "code_text": "print(1)",
            "language": "python", "cell_content_hash": "deadbeef",
            "updated_at": "2026-01-01T00:00:00Z", "updated_by": "alice",
        },
        # A different row's attachment must never leak into r1's code list.
        {
            "row_id": "other-row", "column_id": "m1", "code_text": "print(2)",
            "language": "python", "cell_content_hash": "irrelevant",
            "updated_at": "2026-01-01T00:00:00Z", "updated_by": "bob",
        },
    ]

    detail = knowhow_api.build_row_detail(table, "r1", attachments)

    assert len(detail["code"]) == 1
    assert detail["code"][0]["column_id"] == "m1"
    assert detail["code"][0]["status"] == "stale"
    assert detail["code"][0]["updated_by"] == "alice"


def test_build_row_detail_raises_key_error_for_unknown_row():
    table = _wire_table(anchor_column_id=None, columns=[], rows=[])

    with pytest.raises(KeyError):
        knowhow_api.build_row_detail(table, "missing", [])


def test_build_row_detail_synthesizes_title_when_anchor_cell_blank():
    columns = [
        {"id": "anchor", "name": "现象", "kind": "anchor", "position": 0},
        {"id": "a1", "name": "备注", "kind": "attribute", "position": 1},
    ]
    rows = [{"id": "r1", "position": 3, "cells": {"anchor": "", "a1": "备用标题"}}]
    table = _wire_table(anchor_column_id="anchor", columns=columns, rows=rows)

    detail = knowhow_api.build_row_detail(table, "r1", [])

    assert detail["title"] == "备用标题"


def test_build_row_detail_falls_back_to_positional_title_when_all_cells_blank():
    columns = [{"id": "anchor", "name": "现象", "kind": "anchor", "position": 0}]
    rows = [{"id": "r1", "position": 4, "cells": {}}]
    table = _wire_table(anchor_column_id="anchor", columns=columns, rows=rows)

    detail = knowhow_api.build_row_detail(table, "r1", [])

    assert detail["title"] == "行5"


def test_cell_code_view_three_states():
    cells = {"c1": "先做A"}
    fresh_hash = knowhow_api.cell_content_hash("先做A")

    assert knowhow_api.cell_code_view(cells, "c1", None) == {
        "code_text": None, "language": None, "status": "none", "updated_at": None,
    }

    implemented = knowhow_api.cell_code_view(cells, "c1", {
        "code_text": "print(1)", "language": "python",
        "cell_content_hash": fresh_hash, "updated_at": "2026-01-01T00:00:00Z",
    })
    assert implemented == {
        "code_text": "print(1)", "language": "python",
        "status": "implemented", "updated_at": "2026-01-01T00:00:00Z",
    }

    stale = knowhow_api.cell_code_view(cells, "c1", {
        "code_text": "print(1)", "language": "python",
        "cell_content_hash": "deadbeef", "updated_at": "2026-01-01T00:00:00Z",
    })
    assert stale["status"] == "stale"


# ===========================================================================
# Group B/C shared HTTP fixtures
# ===========================================================================


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    return TestClient(app)


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _user_id(client, headers):
    return client.get("/api/me", headers=headers).json()["id"]


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


def _app_repo():
    from app.api.deps import repository
    return repository()


def _seed(client, username):
    """Owner + table (anchor@0=现象, procedure@1=方法, entity@2=工具) + one
    row with content in every column. Returns (owner_headers, nb, table,
    row, procedure_column_id)."""
    owner_h = _login(client, username)
    nb = _mk_notebook(client, owner_h)
    table = _create_table(client, owner_h, nb)
    anchor_id, procedure_id, entity_id = (c["id"] for c in table["columns"])
    row = _add_row(client, owner_h, nb, table["id"], {
        anchor_id: "过冲振铃",
        procedure_id: "1. 看眼图\n2. 测阻抗",
        entity_id: "示波器",
    })
    return owner_h, nb, table, row, procedure_id


def _agent_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _issue_token(owner_id, scopes, default_nb, notebook_ids=None, *, expires_at=None, name="TestAgent"):
    repo = _app_repo()
    profile = repo.create_agent_profile(owner_id, name, "")
    return repo.issue_agent_token(
        owner_id, profile.id, scopes, default_nb, notebook_ids or [default_nb], expires_at,
    )


# ===========================================================================
# Group B: session-based HTTP tests
# ===========================================================================


def test_agent_tables_list_session_owner(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002001")

    resp = client.get(f"/api/agent/knowhow/tables?notebook_id={nb}", headers=owner_h)

    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    assert items[0]["id"] == table["id"]
    assert items[0]["row_count"] == 1
    assert items[0]["anchor_column_id"] == table["columns"][0]["id"]
    assert {c["kind"] for c in items[0]["columns"]} == {"anchor", "procedure", "entity"}


def test_agent_tables_list_reader_can_read(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002002")
    bob_h = _login(client, "b00002002")
    _app_repo().add_member(nb, _user_id(client, bob_h))

    resp = client.get(f"/api/agent/knowhow/tables?notebook_id={nb}", headers=bob_h)

    assert resp.status_code == 200


def test_agent_tables_list_stranger_gets_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002003")
    stranger_h = _login(client, "c00002003")

    resp = client.get(f"/api/agent/knowhow/tables?notebook_id={nb}", headers=stranger_h)

    assert resp.status_code == 404


def test_discrimination_shape_and_methods_are_procedure_only(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002010")

    resp = client.get(
        f"/api/agent/knowhow/tables/{table['id']}/discrimination", headers=owner_h
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["rows"]) == 1
    row_out = body["rows"][0]
    assert row_out["row_id"] == row["id"]
    assert row_out["title"] == "过冲振铃"
    assert len(row_out["methods"]) == 1
    assert row_out["methods"][0]["column_id"] == col
    assert row_out["methods"][0]["code_status"] == "none"


def test_discrimination_no_anchor_table_returns_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00002011")
    nb = _mk_notebook(client, owner_h)
    table = _create_table(client, owner_h, nb, anchor_index=None)

    resp = client.get(
        f"/api/agent/knowhow/tables/{table['id']}/discrimination", headers=owner_h
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "该表未设置行标题列，不支持判别集"


def test_discrimination_unknown_table_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00002012")
    _mk_notebook(client, owner_h)

    resp = client.get(
        "/api/agent/knowhow/tables/no-such-table/discrimination", headers=owner_h
    )

    assert resp.status_code == 404


def test_discrimination_stranger_gets_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002013")
    stranger_h = _login(client, "c00002013")

    resp = client.get(
        f"/api/agent/knowhow/tables/{table['id']}/discrimination", headers=stranger_h
    )

    assert resp.status_code == 404


def test_row_detail_shape(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002020")

    resp = client.get(f"/api/agent/knowhow/rows/{row['id']}", headers=owner_h)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "过冲振铃"
    by_id = {cell["column_id"]: cell for cell in body["cells"]}
    assert by_id[col]["steps"] == ["看眼图", "测阻抗"]
    entity_col = table["columns"][2]["id"]
    assert by_id[entity_col]["items"] == ["示波器"]
    assert body["code"] == []


def test_row_detail_unknown_row_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002021")

    resp = client.get("/api/agent/knowhow/rows/no-such-row", headers=owner_h)

    assert resp.status_code == 404


def test_row_detail_reader_can_read_but_stranger_cannot(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002022")
    bob_h = _login(client, "b00002022")
    _app_repo().add_member(nb, _user_id(client, bob_h))
    stranger_h = _login(client, "c00002022")

    assert client.get(f"/api/agent/knowhow/rows/{row['id']}", headers=bob_h).status_code == 200
    assert (
        client.get(f"/api/agent/knowhow/rows/{row['id']}", headers=stranger_h).status_code
        == 404
    )


def test_code_status_none_then_implemented_then_stale_after_edit_then_deleted(
    tmp_path, monkeypatch,
):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002030")
    url = f"/api/agent/knowhow/rows/{row['id']}/cells/{col}/code"

    none_resp = client.get(url, headers=owner_h)
    assert none_resp.status_code == 200
    assert none_resp.json() == {
        "code_text": None, "language": None, "status": "none", "updated_at": None,
    }

    put_resp = client.put(
        url, headers=owner_h, json={"code_text": "print('scope')", "language": "python"}
    )
    assert put_resp.status_code == 200, put_resp.text
    put_body = put_resp.json()
    assert put_body["status"] == "implemented"
    assert put_body["code_text"] == "print('scope')"
    assert put_body["language"] == "python"
    assert put_body["updated_at"]

    assert client.get(url, headers=owner_h).json()["status"] == "implemented"

    # Editing the cell's own content (not the code) makes the attachment stale.
    patch_resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows/{row['id']}/cells/{col}",
        headers=owner_h, json={"content_md": "3. 全新识别步骤"},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    assert client.get(url, headers=owner_h).json()["status"] == "stale"

    delete_resp = client.delete(url, headers=owner_h)
    assert delete_resp.status_code == 204

    assert client.get(url, headers=owner_h).json()["status"] == "none"


def test_code_put_requires_write_reader_gets_404_but_can_still_read(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002031")
    bob_h = _login(client, "b00002031")
    _app_repo().add_member(nb, _user_id(client, bob_h))
    url = f"/api/agent/knowhow/rows/{row['id']}/cells/{col}/code"

    assert client.get(url, headers=bob_h).status_code == 200
    put_resp = client.put(url, headers=bob_h, json={"code_text": "x = 1", "language": "python"})
    assert put_resp.status_code == 404
    assert client.delete(url, headers=bob_h).status_code == 404


def test_code_put_empty_text_returns_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002032")
    url = f"/api/agent/knowhow/rows/{row['id']}/cells/{col}/code"

    resp = client.put(url, headers=owner_h, json={"code_text": "   ", "language": "python"})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "代码内容不能为空"


def test_code_bad_column_returns_400(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002033")
    url = f"/api/agent/knowhow/rows/{row['id']}/cells/no-such-column/code"

    assert client.get(url, headers=owner_h).status_code == 400
    assert client.put(
        url, headers=owner_h, json={"code_text": "x", "language": ""}
    ).status_code == 400
    assert client.delete(url, headers=owner_h).status_code == 400


def test_code_unknown_row_returns_404_for_every_verb(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002034")
    url = "/api/agent/knowhow/rows/no-such-row/cells/no-such-column/code"

    assert client.get(url, headers=owner_h).status_code == 404
    assert client.put(
        url, headers=owner_h, json={"code_text": "x", "language": ""}
    ).status_code == 404
    assert client.delete(url, headers=owner_h).status_code == 404


# ===========================================================================
# Group C: Agent Bearer token HTTP tests
# ===========================================================================


def test_agent_token_with_knowledge_read_scope_can_read_tables_and_row(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002040")
    owner_id = _user_id(client, owner_h)
    issued = _issue_token(owner_id, ["knowledge:read"], nb)
    agent_h = _agent_headers(issued.token)

    assert client.get(f"/api/agent/knowhow/tables?notebook_id={nb}", headers=agent_h).status_code == 200
    assert client.get(f"/api/agent/knowhow/rows/{row['id']}", headers=agent_h).status_code == 200


def test_agent_token_missing_scope_cannot_write_code(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002041")
    owner_id = _user_id(client, owner_h)
    issued = _issue_token(owner_id, ["knowledge:read"], nb)
    agent_h = _agent_headers(issued.token)
    url = f"/api/agent/knowhow/rows/{row['id']}/cells/{col}/code"

    assert client.get(url, headers=agent_h).status_code == 200
    resp = client.put(url, headers=agent_h, json={"code_text": "x = 1", "language": "python"})
    assert resp.status_code == 404


def test_agent_token_with_code_scope_can_write_and_attribution_reaches_row_detail(
    tmp_path, monkeypatch,
):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002042")
    owner_id = _user_id(client, owner_h)
    issued = _issue_token(
        owner_id, ["knowledge:read", "knowhow:code"], nb, name="CodeAgent"
    )
    agent_h = _agent_headers(issued.token)
    url = f"/api/agent/knowhow/rows/{row['id']}/cells/{col}/code"

    resp = client.put(url, headers=agent_h, json={"code_text": "print(1)", "language": "python"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "implemented"

    # updated_by attribution reaches row detail (the single-cell code
    # endpoint deliberately omits updated_by — see design/wire contract).
    detail = client.get(f"/api/agent/knowhow/rows/{row['id']}", headers=owner_h).json()
    code_entry = next(c for c in detail["code"] if c["column_id"] == col)
    assert code_entry["updated_by"] == "CodeAgent"


# ===========================================================================
# knowhow 表版本管理 Task 13 code review: put/delete_agent_cell_code used to
# hard-default origin="user" regardless of who actually wrote the code
# attachment (VALID_ORIGINS has carried "agent" since Task 12, but nothing
# had ever produced it -- see models/knowhow.py's own "honest badge, never a
# silent default" contract). Both writers reach the same dual-mode route, so
# only actor.is_agent tells them apart; the pair below proves the fix
# branches on it rather than hardcoding either value for everyone.
# ===========================================================================


def test_agent_token_code_write_records_agent_origin(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002050")
    owner_id = _user_id(client, owner_h)
    issued = _issue_token(
        owner_id, ["knowledge:read", "knowhow:code"], nb, name="CodeAgent"
    )
    agent_h = _agent_headers(issued.token)
    url = f"/api/agent/knowhow/rows/{row['id']}/cells/{col}/code"

    put_resp = client.put(
        url, headers=agent_h, json={"code_text": "print(1)", "language": "python"}
    )
    assert put_resp.status_code == 200, put_resp.text

    history_url = f"/api/notebooks/{nb}/knowhow/{table['id']}/history"
    changes = client.get(history_url, headers=owner_h).json()["changes"]
    put_change = next(c for c in changes if c["kind"] == "cell_code_put")
    assert put_change["origin"] == "agent"

    assert client.delete(url, headers=agent_h).status_code == 204
    changes = client.get(history_url, headers=owner_h).json()["changes"]
    delete_change = next(c for c in changes if c["kind"] == "cell_code_delete")
    assert delete_change["origin"] == "agent"


def test_session_code_put_still_records_user_origin(tmp_path, monkeypatch):
    """Contrast case: a SESSION user writing through the SAME dual-mode
    route must still be attributed origin="user", not "agent" — this is
    what proves the fix branches on actor.is_agent rather than simply
    swapping the hardcoded default from "user" to "agent" for everyone."""
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002051")
    url = f"/api/agent/knowhow/rows/{row['id']}/cells/{col}/code"

    resp = client.put(
        url, headers=owner_h, json={"code_text": "print(1)", "language": "python"}
    )
    assert resp.status_code == 200, resp.text

    changes = client.get(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/history", headers=owner_h,
    ).json()["changes"]
    put_change = next(c for c in changes if c["kind"] == "cell_code_put")
    assert put_change["origin"] == "user"


def test_agent_token_notebook_not_in_allowlist_returns_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002043")
    owner_id = _user_id(client, owner_h)
    other_nb = _mk_notebook(client, owner_h, name="Other")
    issued = _issue_token(owner_id, ["knowledge:read"], other_nb)
    agent_h = _agent_headers(issued.token)

    assert client.get(
        f"/api/agent/knowhow/tables?notebook_id={nb}", headers=agent_h
    ).status_code == 404
    assert client.get(
        f"/api/agent/knowhow/rows/{row['id']}", headers=agent_h
    ).status_code == 404


def test_agent_token_revoked_returns_401(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002044")
    owner_id = _user_id(client, owner_h)
    issued = _issue_token(owner_id, ["knowledge:read"], nb)
    agent_h = _agent_headers(issued.token)
    _app_repo().revoke_agent_token(owner_id, issued.id)

    resp = client.get(f"/api/agent/knowhow/tables?notebook_id={nb}", headers=agent_h)

    assert resp.status_code == 401


def test_agent_token_expired_returns_401(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002045")
    owner_id = _user_id(client, owner_h)
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    issued = _issue_token(owner_id, ["knowledge:read"], nb, expires_at=past)
    agent_h = _agent_headers(issued.token)

    resp = client.get(f"/api/agent/knowhow/tables?notebook_id={nb}", headers=agent_h)

    assert resp.status_code == 401


def test_agent_token_malformed_bearer_returns_401(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h, nb, table, row, col = _seed(client, "a00002046")

    resp = client.get(
        f"/api/agent/knowhow/tables?notebook_id={nb}",
        headers={"Authorization": "Bearer snm_bogus.not-a-real-token"},
    )

    assert resp.status_code == 401
