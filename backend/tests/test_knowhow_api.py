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


# --- preview normalizes using the USER-confirmed anchor, not the guess -------
#
# knowhow-md-normalize P2 (this fix): the wizard lets the user change/clear the
# row-title (anchor) selection in step 2 before committing; ``import_table``
# then skips the USER-CONFIRMED anchor from normalization. The preview MUST skip
# the same column, or "预览即所得" breaks (preview shows raw where commit stores
# normalized, and vice versa). Fixture: col 0 ("现象类型") is the GUESSED anchor
# (hits the 类型 keyword); BOTH columns hold Excel bullet content that
# ``rule_normalize`` rewrites ("• x" -> "- x"), so raw-vs-normalized is directly
# observable per column.
NORM_HEADER = ["现象类型", "识别步骤"]
NORM_KINDS = ["attribute", "procedure"]
NORM_ROWS = [["• 过冲\n• 欠冲", "• 看上升沿\n• 看下降沿"]]
NORM_RAW_0, NORM_NORM_0 = "• 过冲\n• 欠冲", "- 过冲\n- 欠冲"
NORM_RAW_1, NORM_NORM_1 = "• 看上升沿\n• 看下降沿", "- 看上升沿\n- 看下降沿"


def _preview_norm(client, headers, nb, *, anchor_index=None):
    data = _xlsx_bytes(NORM_HEADER, NORM_ROWS)
    form = {} if anchor_index is None else {"anchor_index": str(anchor_index)}
    return client.post(
        f"/api/notebooks/{nb}/knowhow/import/preview",
        headers=headers,
        files={"file": ("p.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data=form,
    )


def _commit_stored_row0(client, headers, nb, *, anchor_index):
    """Import NORM_ROWS with the given anchor_index and return the single stored
    row's values as a column-ordered list — i.e. what commit ACTUALLY persisted,
    the ground truth the preview must match."""
    resp = _import_xlsx(
        client, headers, nb, header=NORM_HEADER, rows=NORM_ROWS,
        columns_json=json.dumps([{"name": n, "kind": k} for n, k in zip(NORM_HEADER, NORM_KINDS)]),
        anchor_index=anchor_index,
    )
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    col_ids = [c["id"] for c in detail["columns"]]
    row = detail["rows"][0]
    return [row["cells"].get(cid, "") for cid in col_ids]


def test_preview_skips_guessed_anchor_without_param(tmp_path, monkeypatch):
    """Default (no anchor_index form field): unchanged behavior — the GUESSED
    anchor column (col 0) is left raw; every other column is normalized."""
    client = _client(tmp_path, monkeypatch)
    h = _login(client, "a00000531")
    nb = _mk_notebook(client, h)
    body = _preview_norm(client, h, nb).json()
    assert body["anchor_suggestion"] == 0
    assert body["rows_preview"][0] == [NORM_RAW_0, NORM_NORM_1]


def test_preview_skips_user_confirmed_anchor(tmp_path, monkeypatch):
    """anchor_index=1 (user re-picked col 1 as the row-title): col 1 now stays
    raw and the previously-guessed col 0 is normalized — the preview follows the
    CONFIRMED anchor, not the guess. The reported anchor_suggestion still echoes
    the guess (that field is the wizard's initial-selection hint, unchanged)."""
    client = _client(tmp_path, monkeypatch)
    h = _login(client, "a00000532")
    nb = _mk_notebook(client, h)
    body = _preview_norm(client, h, nb, anchor_index=1).json()
    assert body["anchor_suggestion"] == 0
    assert body["rows_preview"][0] == [NORM_NORM_0, NORM_RAW_1]


def test_preview_matches_commit_for_guessed_anchor(tmp_path, monkeypatch):
    """预览即所得, guessed-anchor path: preview-without-param == what a commit
    with anchor_index=0 (the guess) actually stores."""
    client = _client(tmp_path, monkeypatch)
    h = _login(client, "a00000533")
    nb = _mk_notebook(client, h)
    preview_row = _preview_norm(client, h, nb).json()["rows_preview"][0]
    stored_row = _commit_stored_row0(client, h, nb, anchor_index=0)
    assert preview_row == stored_row == [NORM_RAW_0, NORM_NORM_1]


def test_preview_matches_commit_for_changed_anchor(tmp_path, monkeypatch):
    """预览即所得, changed-anchor path (the contract this fix restores): preview
    with anchor_index=1 == what a commit with anchor_index=1 stores."""
    client = _client(tmp_path, monkeypatch)
    h = _login(client, "a00000534")
    nb = _mk_notebook(client, h)
    preview_row = _preview_norm(client, h, nb, anchor_index=1).json()["rows_preview"][0]
    stored_row = _commit_stored_row0(client, h, nb, anchor_index=1)
    assert preview_row == stored_row == [NORM_NORM_0, NORM_RAW_1]


def test_preview_explicit_no_anchor_normalizes_all_and_matches_commit(tmp_path, monkeypatch):
    """预览即所得, cleared-anchor path（三态修复的第三态）：anchor_index=-1 表示
    向导里明确清空「不设行标题」——预览不跳过任何列（全列规整），与 commit 不带
    anchor_index（无锚定列 → 存储环节全列规整）落库的结果逐字一致。若 -1 也退回
    按猜测列跳过，预览会在猜测列上显示 raw、提交却存 normalized——预览即所得撒谎。
    三态：缺省=按猜测列跳过；>=0=按所选列跳过；负数=明确无锚定、不跳过。
    anchor_suggestion 仍如实回报猜测（它是初始预选提示，与跳过哪列无关）。"""
    client = _client(tmp_path, monkeypatch)
    h = _login(client, "a00000535")
    nb = _mk_notebook(client, h)
    body = _preview_norm(client, h, nb, anchor_index=-1).json()
    assert body["anchor_suggestion"] == 0
    assert body["rows_preview"][0] == [NORM_NORM_0, NORM_NORM_1]
    stored_row = _commit_stored_row0(client, h, nb, anchor_index=None)
    assert body["rows_preview"][0] == stored_row


def test_preview_out_of_range_anchor_index_rejected_like_commit(tmp_path, monkeypatch):
    """F5 (this review): preview must validate anchor_index range EXACTLY like the
    import commit does. An anchor_index >= column count previously slipped through
    preview (skips nothing -> normalizes ALL columns, 200) while the commit path
    (`_columns_with_anchor`) rejects it 400 -> preview≠commit. Both must now raise
    the same friendly-Chinese 400. `-1`/None/valid indices are unchanged (locked
    by the four tests above)."""
    client = _client(tmp_path, monkeypatch)
    h = _login(client, "a00000536")
    nb = _mk_notebook(client, h)
    oor = len(HEADER)   # == 5; valid indices are 0..4, so 5 is first out-of-range

    # commit is the ground-truth error shape for out-of-range anchor_index.
    commit = _import_xlsx(client, h, nb, anchor_index=oor)
    assert commit.status_code == 400, commit.text
    commit_detail = commit.json()["detail"]

    # preview must now reject the SAME index with the SAME 400 (was 200 pre-fix).
    data = _xlsx_bytes(HEADER, DATA_ROWS)
    preview = client.post(
        f"/api/notebooks/{nb}/knowhow/import/preview",
        headers=h,
        files={"file": ("p.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"anchor_index": str(oor)},
    )
    assert preview.status_code == 400, preview.text
    assert preview.json()["detail"] == commit_detail   # SAME friendly message
    assert any("一" <= ch <= "鿿" for ch in preview.json()["detail"])


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


def test_import_table_forward_fills_anchor_column(tmp_path, monkeypatch, repo):
    """anchor 分组显示 Task 2: a 转置/合并型 source table's concept column is
    written once per group in the source file (vertical-merge-style "写一
    次" convention) and left blank on the sibling branch rows below.
    import_table must forward-fill the anchor column before insert so every
    branch row shares the SAME anchor value — the downstream cell-level
    projector groups rows by anchor value into one concept KO
    (projection.py); an unfilled blank would silently orphan each branch as
    its own concept instead of merging them. Calls import_table directly
    (not through the HTTP route) against the `repo` fixture's DB, using a
    notebook created via the HTTP client (mirrors
    test_delete_never_projected_table_is_a_safe_noop_for_projection's mix of
    HTTP-created notebook + direct repo/service calls)."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000525")
    nb = _mk_notebook(client, owner_h)

    from app.services.knowhow.api import import_table

    data = _xlsx_bytes(
        ["违例概念", "根因", "修复"],
        [
            ["hold&setup", "inst 变化", "换 VT"],
            ["", "cell delay", "底层走线"],
            ["", "noise", "提高 victim"],
        ],
    )
    columns_json = json.dumps([
        {"name": "违例概念", "kind": "attribute"},
        {"name": "根因", "kind": "procedure"},
        {"name": "修复", "kind": "procedure"},
    ])
    table_id = import_table(
        repo, nb, "t.xlsx", data, "违例表", columns_json, anchor_index=0,
    )

    detail = repo.get_knowhow_table(table_id)
    anchor_col_id = detail["columns"][0]["id"]
    anchor_values = [r["cells"].get(anchor_col_id, "") for r in detail["rows"]]
    assert anchor_values == ["hold&setup", "hold&setup", "hold&setup"]


def test_commit_append_forward_fills_anchor_column(tmp_path, monkeypatch, repo):
    """final-review fix (Important 1): 追加导入 (commit_append) must
    forward-fill the anchor column exactly like import_table already does
    (test_import_table_forward_fills_anchor_column above) — "分组列只写一
    次，兄弟行留空" is the same convention whether the table is brand-new
    (import) or already exists and rows are being appended into it later.
    Before this fix, commit_append aligned the file's rows to the table's
    columns and inserted them verbatim (`if value` merely skips blank cells,
    it never forward-fills) — an appended branch row's blank anchor cell
    stayed blank, so groupRowsByAnchor scatters it into its own single-row
    group and projection._accumulate_row_knowledge (`if not anchor_text:
    return`) drops it out of the KG entirely: silent data loss on append that
    the import path doesn't have. Builds the target table directly via
    create_table (mirrors test_import_table_forward_fills_anchor_column's own
    "no HTTP for the facade call" style), fetches its wire-shaped detail
    (commit_append's own `table` param shape — routes.py's `_require_table`
    always hands it a `to_wire_table` result), then commit_appends a file
    following the same vertical-merge "写一次" convention as the import test
    above."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000526")
    nb = _mk_notebook(client, owner_h)

    from app.services.knowhow.api import commit_append, create_table, to_wire_table

    table_id = create_table(
        repo, nb, "违例表",
        [
            {"name": "违例概念", "kind": "attribute"},
            {"name": "根因", "kind": "procedure"},
            {"name": "修复", "kind": "procedure"},
        ],
        anchor_index=0,
    )
    table = to_wire_table(repo.get_knowhow_table(table_id))

    data = _xlsx_bytes(
        ["违例概念", "根因", "修复"],
        [
            ["hold&setup", "inst 变化", "换 VT"],
            ["", "cell delay", "底层走线"],
            ["", "noise", "提高 victim"],
        ],
    )
    added = commit_append(repo, table_id, table, "append.xlsx", data)
    assert added == 3

    detail = repo.get_knowhow_table(table_id)
    anchor_col_id = detail["columns"][0]["id"]
    anchor_values = [r["cells"].get(anchor_col_id, "") for r in detail["rows"]]
    assert anchor_values == ["hold&setup", "hold&setup", "hold&setup"]


# ---------------------------------------------------------------------------
# knowhow-md-normalize Task 5: import/append must normalize Excel-idiom cell
# formatting (Tab-indented `•` bullets, `A.`/`B.` section markers) into clean
# CommonMark BEFORE storing — rules-only (Task 1's rule_normalize), zero LLM,
# since this is the always-on bulk-import path (an efficiency constraint: it
# must not make a per-cell LLM call). The LLM-backed reformat_cell (Task 3)
# stays a separate, explicit-trigger-only suggestion layer that never runs
# automatically on import/append.
# ---------------------------------------------------------------------------

IDIOM_CELL = "A. 考量\n\t• 增大 R\n\t• 增大 C"


def test_import_table_normalizes_excel_idiom_cells(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000529")
    nb = _mk_notebook(client, owner_h)

    from app.services.knowhow.api import import_table

    data = _xlsx_bytes(["概念", "方法"], [["概念X", IDIOM_CELL]])
    columns_json = json.dumps([
        {"name": "概念", "kind": "attribute"},
        {"name": "方法", "kind": "procedure"},
    ])
    table_id = import_table(repo, nb, "t.xlsx", data, "整改表", columns_json, anchor_index=0)

    detail = repo.get_knowhow_table(table_id)
    method_col_id = detail["columns"][1]["id"]
    stored = detail["rows"][0]["cells"][method_col_id]

    assert "\t" not in stored
    assert "•" not in stored
    assert "- 增大 R" in stored.split("\n")
    assert "**A. 考量**" in stored


def test_commit_append_normalizes_excel_idiom_cells(tmp_path, monkeypatch, repo):
    """Same guarantee as test_import_table_normalizes_excel_idiom_cells above,
    but for the append path — commit_append's own storage loop must not skip
    rule_normalize just because the table already exists."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000530")
    nb = _mk_notebook(client, owner_h)

    from app.services.knowhow.api import commit_append, create_table, to_wire_table

    table_id = create_table(
        repo, nb, "整改表",
        [
            {"name": "概念", "kind": "attribute"},
            {"name": "方法", "kind": "procedure"},
        ],
        anchor_index=0,
    )
    table = to_wire_table(repo.get_knowhow_table(table_id))

    data = _xlsx_bytes(["概念", "方法"], [["概念X", IDIOM_CELL]])
    added = commit_append(repo, table_id, table, "append.xlsx", data)
    assert added == 1

    detail = repo.get_knowhow_table(table_id)
    method_col_id = detail["columns"][1]["id"]
    stored = detail["rows"][0]["cells"][method_col_id]

    assert "\t" not in stored
    assert "•" not in stored
    assert "- 增大 R" in stored.split("\n")
    assert "**A. 考量**" in stored


# ---------------------------------------------------------------------------
# P2-3 (code review): the preview IS the human-review gate ("预览即所得") --
# it must show EXACTLY the text a subsequent commit will persist, not the raw
# parsed grid. preview_import/preview_append used to return grid.rows/
# aligned_rows verbatim while import_table/commit_append stored
# safe_rule_normalize'd text — an Excel-idiom cell previewed one way and
# committed a DIFFERENT way, defeating the point of reviewing before commit.
# ---------------------------------------------------------------------------


def test_preview_import_matches_what_import_table_stores(tmp_path, monkeypatch, repo):
    """Byte-for-byte comparison against the ACTUAL committed cell (not a
    re-implementation of the normalization rules) -- proves preview_import
    and import_table agree because they call the SAME normalization
    function on the SAME input, not because two independent
    implementations happen to produce matching output today.

    P1-c: the anchor column is a grouping KEY -- both preview AND commit must
    keep it byte-raw (never normalize `A. 概念` into `**A. 概念**`), so the
    idiom-shaped anchor cell below is asserted to pass through unchanged on
    BOTH sides (preview == commit stays true for it too)."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000531")
    nb = _mk_notebook(client, owner_h)

    from app.services.knowhow.api import import_table, preview_import

    # column 0 (概念) is the anchor (guess_kinds flags 概念; import passes
    # anchor_index=0) and carries an Excel-idiom-shaped value -- it must NOT be
    # normalized. Column 1 (方法) is a normal content column that MUST be.
    data = _xlsx_bytes(["概念", "方法"], [["A. 概念", IDIOM_CELL]])
    columns_json = json.dumps([
        {"name": "概念", "kind": "attribute"},
        {"name": "方法", "kind": "procedure"},
    ])

    preview = preview_import("t.xlsx", data)
    previewed_anchor = preview["rows_preview"][0][0]
    previewed_method = preview["rows_preview"][0][1]
    assert "\t" not in previewed_method and "•" not in previewed_method  # method normalized
    assert previewed_anchor == "A. 概念"  # anchor kept byte-raw, not **A. 概念**

    table_id = import_table(repo, nb, "t.xlsx", data, "整改表", columns_json, anchor_index=0)
    detail = repo.get_knowhow_table(table_id)
    anchor_col_id = detail["columns"][0]["id"]
    method_col_id = detail["columns"][1]["id"]
    stored_anchor = detail["rows"][0]["cells"][anchor_col_id]
    stored_method = detail["rows"][0]["cells"][method_col_id]

    assert previewed_method == stored_method
    assert stored_anchor == "A. 概念"          # commit kept the anchor byte-raw too
    assert previewed_anchor == stored_anchor    # preview == commit for the anchor


def test_preview_append_matches_what_commit_append_stores(tmp_path, monkeypatch, repo):
    """Same guarantee as test_preview_import_matches_what_import_table_stores
    above, for the append path -- preview_append's rows_preview must match
    what commit_append actually persists into an EXISTING table.

    P1-c: the anchor column stays byte-raw on BOTH the preview and the commit
    side (idiom-shaped `A. 概念` below), exactly like the import test above."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000532")
    nb = _mk_notebook(client, owner_h)

    from app.services.knowhow.api import commit_append, create_table, preview_append, to_wire_table

    table_id = create_table(
        repo, nb, "整改表",
        [
            {"name": "概念", "kind": "attribute"},
            {"name": "方法", "kind": "procedure"},
        ],
        anchor_index=0,
    )
    table = to_wire_table(repo.get_knowhow_table(table_id))
    data = _xlsx_bytes(["概念", "方法"], [["A. 概念", IDIOM_CELL]])

    preview = preview_append(table, "append.xlsx", data)
    previewed_anchor = preview["rows_preview"][0][0]
    previewed_method = preview["rows_preview"][0][1]
    assert "\t" not in previewed_method and "•" not in previewed_method  # method normalized
    assert previewed_anchor == "A. 概念"  # anchor kept byte-raw, not **A. 概念**

    added = commit_append(repo, table_id, table, "append.xlsx", data)
    assert added == 1

    detail = repo.get_knowhow_table(table_id)
    anchor_col_id = detail["columns"][0]["id"]
    method_col_id = detail["columns"][1]["id"]
    stored_anchor = detail["rows"][0]["cells"][anchor_col_id]
    stored_method = detail["rows"][0]["cells"][method_col_id]

    assert previewed_method == stored_method
    assert stored_anchor == "A. 概念"          # commit kept the anchor byte-raw too
    assert previewed_anchor == stored_anchor    # preview == commit for the anchor


def test_commit_append_keeps_anchor_byte_stable_and_in_same_group(tmp_path, monkeypatch, repo):
    """P1-c (the confirmed defect): the anchor column is a grouping KEY, not
    prose. An appended row whose anchor text matches an existing row's anchor
    must be stored BYTE-IDENTICAL, so both land in the SAME anchor group. Before
    the fix commit_append normalized the incoming anchor (`A. Component` ->
    `**A. Component**`) while the existing row kept `A. Component`, splitting the
    group even though the append preview matched them. Both the existing row
    (via import_table) and the appended row (via commit_append) go through the
    now-anchor-skipping bulk paths, so both anchors stay raw and equal."""
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000533")
    nb = _mk_notebook(client, owner_h)

    from app.services.knowhow.api import commit_append, import_table, to_wire_table

    columns_json = json.dumps([
        {"name": "概念", "kind": "attribute"},
        {"name": "方法", "kind": "procedure"},
    ])
    data0 = _xlsx_bytes(["概念", "方法"], [["A. Component", "既有内容"]])
    table_id = import_table(repo, nb, "t.xlsx", data0, "违例表", columns_json, anchor_index=0)
    table = to_wire_table(repo.get_knowhow_table(table_id))
    anchor_col_id = table["columns"][0]["id"]

    existing_anchor = repo.get_knowhow_table(table_id)["rows"][0]["cells"][anchor_col_id]
    assert existing_anchor == "A. Component"  # import kept the anchor byte-raw

    data1 = _xlsx_bytes(["概念", "方法"], [["A. Component", "追加内容"]])
    added = commit_append(repo, table_id, table, "append.xlsx", data1)
    assert added == 1

    detail = repo.get_knowhow_table(table_id)
    anchors = [r["cells"].get(anchor_col_id, "") for r in detail["rows"]]
    # both rows carry byte-identical anchor text -> one group, not two
    assert anchors == ["A. Component", "A. Component"]


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


# ---------------------------------------------------------------------------
# Concurrency P1 fixes (batch-reformat integrity moved SERVER-SIDE):
#  (a) /reformat round-trips the exact saved content_md it read as ``source_md``
#      so the batch client can detect "the server reformatted content I never
#      snapshotted" (a concurrent edit between modal-open and /reformat).
#  (b) the cell-save endpoints take an OPTIONAL ``expected_before`` — when set,
#      the write goes through KnowhowStore.update_knowhow_cells_guarded_atomic
#      (compare-and-write in ONE transaction; a stale baseline → 409, NOTHING
#      written; fan-out is all-or-nothing). Omitted → legacy last-write-wins
#      (the manual editor's semantics stay unchanged).
# ---------------------------------------------------------------------------


def _seed_table_detail(client, headers, nb):
    table_id = _import_xlsx(client, headers, nb).json()["id"]
    detail = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=headers).json()
    return table_id, detail


def _cell(detail, row_index, col_name):
    col = next(c for c in detail["columns"] if c["name"] == col_name)
    row = detail["rows"][row_index]
    return row["id"], col["id"], row["cells"].get(col["id"], "")


def test_reformat_response_carries_source_md_equal_to_stored_cell(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000540")
    nb = _mk_notebook(client, owner_h)
    table_id, detail = _seed_table_detail(client, owner_h, nb)
    row_id, col_id, stored = _cell(detail, 0, "修复方法")
    assert stored  # sanity: the seeded cell is non-empty

    resp = client.post(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows/{row_id}/cells/{col_id}/reformat",
        headers=owner_h,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {"candidate_md", "source", "changed", "source_md"} <= set(body)
    assert body["source_md"] == stored


def test_patch_cell_expected_before_match_writes_and_marks_pending(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000541")
    nb = _mk_notebook(client, owner_h)
    table_id, detail = _seed_table_detail(client, owner_h, nb)
    row_id, col_id, stored = _cell(detail, 0, "修复方法")

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows/{row_id}/cells/{col_id}",
        headers=owner_h,
        json={"content_md": "增加串联阻尼电阻", "expected_before": stored},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content_md"] == "增加串联阻尼电阻"
    assert body["projection_status"] == "pending"
    fresh = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h).json()
    assert _cell(fresh, 0, "修复方法")[2] == "增加串联阻尼电阻"


def test_patch_cell_expected_before_mismatch_409_writes_nothing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000542")
    nb = _mk_notebook(client, owner_h)
    table_id, detail = _seed_table_detail(client, owner_h, nb)
    row_id, col_id, stored = _cell(detail, 0, "修复方法")

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows/{row_id}/cells/{col_id}",
        headers=owner_h,
        json={"content_md": "不该落库", "expected_before": stored + "（他人已改）"},
    )
    assert resp.status_code == 409, resp.text
    fresh = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h).json()
    assert _cell(fresh, 0, "修复方法")[2] == stored  # untouched


def test_patch_cell_without_expected_before_is_legacy_last_write_wins(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000543")
    nb = _mk_notebook(client, owner_h)
    table_id, detail = _seed_table_detail(client, owner_h, nb)
    row_id, col_id, _ = _cell(detail, 0, "修复方法")

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows/{row_id}/cells/{col_id}",
        headers=owner_h,
        json={"content_md": "覆盖写入"},  # no expected_before → unconditional
    )
    assert resp.status_code == 200, resp.text
    fresh = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h).json()
    assert _cell(fresh, 0, "修复方法")[2] == "覆盖写入"


def test_batch_cells_expected_before_all_match_writes_all(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000544")
    nb = _mk_notebook(client, owner_h)
    table_id, detail = _seed_table_detail(client, owner_h, nb)
    col = next(c for c in detail["columns"] if c["name"] == "修复方法")
    r0, r1 = detail["rows"][0], detail["rows"][1]
    eb0 = r0["cells"].get(col["id"], "")
    eb1 = r1["cells"].get(col["id"], "")

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/cells",
        headers=owner_h,
        json={
            "column_id": col["id"],
            "row_ids": [r0["id"], r1["id"]],
            "content_md": "统一改法",
            "expected_before": [eb0, eb1],
        },
    )
    assert resp.status_code == 200, resp.text
    fresh = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h).json()
    by_id = {r["id"]: r for r in fresh["rows"]}
    assert by_id[r0["id"]]["cells"][col["id"]] == "统一改法"
    assert by_id[r1["id"]]["cells"][col["id"]] == "统一改法"


def test_batch_cells_expected_before_any_mismatch_409_writes_nothing(tmp_path, monkeypatch):
    # Fan-out atomicity: ANY stale baseline refuses the WHOLE group (never a
    # half-written concept group).
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000545")
    nb = _mk_notebook(client, owner_h)
    table_id, detail = _seed_table_detail(client, owner_h, nb)
    col = next(c for c in detail["columns"] if c["name"] == "修复方法")
    r0, r1 = detail["rows"][0], detail["rows"][1]
    eb0 = r0["cells"].get(col["id"], "")
    eb1 = r1["cells"].get(col["id"], "")

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/cells",
        headers=owner_h,
        json={
            "column_id": col["id"],
            "row_ids": [r0["id"], r1["id"]],
            "content_md": "统一改法",
            "expected_before": [eb0, eb1 + "（他人已改）"],  # 2nd baseline stale
        },
    )
    assert resp.status_code == 409, resp.text
    fresh = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h).json()
    by_id = {r["id"]: r for r in fresh["rows"]}
    assert by_id[r0["id"]]["cells"][col["id"]] == eb0  # neither row changed
    assert by_id[r1["id"]]["cells"][col["id"]] == eb1


# ---------------------------------------------------------------------------
# F2 — the GUARDED (expected_before) write branch must run the SAME asset
# validation the legacy branch runs: a guarded write whose content references a
# missing/foreign asset:// must be refused with the legacy path's 400 +
# CELL_ASSET_MISSING_MESSAGE, nothing written. Pre-fix the guarded branch went
# straight to update_knowhow_cells_guarded_atomic (no newly_added_asset_refs /
# _require_assets_exist), so a compare-and-write could commit a dead reference.
# ---------------------------------------------------------------------------

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"knowhow-f2-fixture" * 4


def _upload_asset(client, headers, nb) -> str:
    up = client.post(
        f"/api/notebooks/{nb}/assets",
        headers=headers,
        files={"file": ("a.png", _PNG_BYTES, "image/png")},
    )
    assert up.status_code == 200, up.text
    return up.json()["id"]


def test_patch_cell_guarded_bogus_asset_ref_400_writes_nothing(tmp_path, monkeypatch):
    from app.services.knowhow.api import CELL_ASSET_MISSING_MESSAGE

    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000546")
    nb = _mk_notebook(client, owner_h)
    table_id, detail = _seed_table_detail(client, owner_h, nb)
    row_id, col_id, stored = _cell(detail, 0, "修复方法")

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows/{row_id}/cells/{col_id}",
        headers=owner_h,
        # baseline matches -> isolates the asset check from the 409 (stale) path.
        json={"content_md": "见 ![图](asset://bogus-missing-id)", "expected_before": stored},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == CELL_ASSET_MISSING_MESSAGE
    fresh = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h).json()
    assert _cell(fresh, 0, "修复方法")[2] == stored  # nothing written


def test_patch_cell_guarded_valid_asset_ref_succeeds(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000547")
    nb = _mk_notebook(client, owner_h)
    table_id, detail = _seed_table_detail(client, owner_h, nb)
    row_id, col_id, stored = _cell(detail, 0, "修复方法")
    content = f"见 ![图](asset://{_upload_asset(client, owner_h, nb)})"

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/rows/{row_id}/cells/{col_id}",
        headers=owner_h,
        json={"content_md": content, "expected_before": stored},
    )
    assert resp.status_code == 200, resp.text
    fresh = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h).json()
    assert _cell(fresh, 0, "修复方法")[2] == content


def test_batch_cells_guarded_bogus_asset_ref_400_writes_nothing(tmp_path, monkeypatch):
    from app.services.knowhow.api import CELL_ASSET_MISSING_MESSAGE

    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000548")
    nb = _mk_notebook(client, owner_h)
    table_id, detail = _seed_table_detail(client, owner_h, nb)
    col = next(c for c in detail["columns"] if c["name"] == "修复方法")
    r0, r1 = detail["rows"][0], detail["rows"][1]
    eb0 = r0["cells"].get(col["id"], "")
    eb1 = r1["cells"].get(col["id"], "")

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/cells",
        headers=owner_h,
        json={
            "column_id": col["id"],
            "row_ids": [r0["id"], r1["id"]],
            "content_md": "见 ![图](asset://bogus-missing-id)",
            "expected_before": [eb0, eb1],  # baselines match -> not a 409; the asset check must fire
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == CELL_ASSET_MISSING_MESSAGE
    fresh = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h).json()
    by_id = {r["id"]: r for r in fresh["rows"]}
    assert by_id[r0["id"]]["cells"][col["id"]] == eb0  # neither row changed
    assert by_id[r1["id"]]["cells"][col["id"]] == eb1


def test_batch_cells_guarded_valid_asset_ref_succeeds(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000549")
    nb = _mk_notebook(client, owner_h)
    table_id, detail = _seed_table_detail(client, owner_h, nb)
    col = next(c for c in detail["columns"] if c["name"] == "修复方法")
    r0, r1 = detail["rows"][0], detail["rows"][1]
    eb0 = r0["cells"].get(col["id"], "")
    eb1 = r1["cells"].get(col["id"], "")
    content = f"见 ![图](asset://{_upload_asset(client, owner_h, nb)})"

    resp = client.patch(
        f"/api/notebooks/{nb}/knowhow/{table_id}/cells",
        headers=owner_h,
        json={
            "column_id": col["id"],
            "row_ids": [r0["id"], r1["id"]],
            "content_md": content,
            "expected_before": [eb0, eb1],
        },
    )
    assert resp.status_code == 200, resp.text
    fresh = client.get(f"/api/notebooks/{nb}/knowhow/{table_id}", headers=owner_h).json()
    by_id = {r["id"]: r for r in fresh["rows"]}
    assert by_id[r0["id"]]["cells"][col["id"]] == content
    assert by_id[r1["id"]]["cells"][col["id"]] == content
