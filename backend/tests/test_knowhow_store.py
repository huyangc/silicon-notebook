"""Task 2 (knowhow-tables PR-1): knowhow_store repository module + facade
composition. Covers every ``KnowhowStore`` method (row-level, direct) plus
one end-to-end test proving the facade's one-hop delegates reach the SAME
runtime-owned store. Task 5's projector and Task 6's import/table API build
directly on these exact names/signatures.
See docs/superpowers/plans/2026-07-15-knowhow-tables-pr1.md Task 2.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite.anchor_normalization import (
    JS_TRIM_WHITESPACE,
    js_trim,
    sqlite_js_trim_expression,
)
from app.repositories.sqlite.knowhow_store import KnowhowStore
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


@pytest.fixture
def store(repo) -> KnowhowStore:
    return repo._runtime.knowhow_store


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id


BASE_COLUMNS = [
    {"name": "违例类型", "role": "anchor"},
    {"name": "现象识别", "role": "procedure"},
    {"name": "修复方法", "role": "attribute"},
]


def _columns_by_name(store: KnowhowStore, table_id: str) -> dict[str, str]:
    detail = store.get_knowhow_table(table_id)
    return {column["name"]: column["id"] for column in detail["columns"]}


def _cell_count(store: KnowhowStore, row_id: str, column_id: str) -> int:
    with store.database.connect() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM knowhow_cells WHERE row_id = ? AND column_id = ?",
            (row_id, column_id),
        ).fetchone()["n"]


# ---------------------------------------------------------------------------
# create_knowhow_table
# ---------------------------------------------------------------------------


def test_runtime_owns_knowhow_store(repo):
    assert isinstance(repo._runtime.knowhow_store, KnowhowStore)


def test_create_table_persists_columns_in_order_with_roles(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "时序修复", "desc", BASE_COLUMNS)
    assert isinstance(table_id, str) and table_id

    detail = store.get_knowhow_table(table_id)
    assert detail["notebook_id"] == notebook_id
    assert detail["title"] == "时序修复"
    assert detail["description"] == "desc"
    assert detail["mutation_seq"] == 0
    assert detail["hidden_source_id"] is None
    assert [c["name"] for c in detail["columns"]] == ["违例类型", "现象识别", "修复方法"]
    assert [c["role"] for c in detail["columns"]] == ["anchor", "procedure", "attribute"]
    assert [c["position"] for c in detail["columns"]] == [0, 1, 2]
    assert detail["rows"] == []


def test_create_table_allows_zero_anchor_columns(store, notebook_id):
    columns = [{"name": "A", "role": "attribute"}, {"name": "B", "role": "procedure"}]
    table_id = store.create_knowhow_table(notebook_id, "T", "", columns)
    assert store.get_knowhow_table(table_id)["title"] == "T"


def test_create_table_rejects_second_anchor_column(store, notebook_id):
    columns = [{"name": "A", "role": "anchor"}, {"name": "B", "role": "anchor"}]
    with pytest.raises(ValueError):
        store.create_knowhow_table(notebook_id, "T", "", columns)


def test_create_table_rejects_empty_column_name(store, notebook_id):
    columns = [{"name": "", "role": "anchor"}, {"name": "B", "role": "attribute"}]
    with pytest.raises(ValueError):
        store.create_knowhow_table(notebook_id, "T", "", columns)


def test_create_table_rejects_duplicate_column_names(store, notebook_id):
    columns = [{"name": "同名", "role": "anchor"}, {"name": "同名", "role": "attribute"}]
    with pytest.raises(ValueError):
        store.create_knowhow_table(notebook_id, "T", "", columns)


def test_create_table_error_messages_are_chinese_friendly(store, notebook_id):
    bad_column_sets = (
        [{"name": "A", "role": "not_a_kind"}],
        [{"name": "", "role": "anchor"}],
        [{"name": "同", "role": "anchor"}, {"name": "同", "role": "attribute"}],
    )
    for columns in bad_column_sets:
        with pytest.raises(ValueError) as exc:
            store.create_knowhow_table(notebook_id, "T", "", columns)
        message = str(exc.value)
        assert any("一" <= ch <= "鿿" for ch in message), message


@pytest.mark.parametrize("bad_title", ["", "   ", "\t\n"])
def test_create_table_rejects_empty_or_whitespace_title(store, notebook_id, bad_title):
    with pytest.raises(ValueError) as exc:
        store.create_knowhow_table(notebook_id, bad_title, "", BASE_COLUMNS)
    message = str(exc.value)
    assert "表标题不能为空" in message
    # Nothing written on failure.
    assert store.list_knowhow_tables(notebook_id) == []


def test_create_table_strips_stored_title(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "  时序修复  ", "", BASE_COLUMNS)
    assert store.get_knowhow_table(table_id)["title"] == "时序修复"


def test_create_table_writes_nothing_on_validation_failure(store, notebook_id):
    with pytest.raises(ValueError):
        store.create_knowhow_table(
            notebook_id, "T", "", [{"name": "A", "role": "not_a_kind"}]
        )
    assert store.list_knowhow_tables(notebook_id) == []


# ---------------------------------------------------------------------------
# list_knowhow_tables
# ---------------------------------------------------------------------------


def test_list_tables_includes_row_count(store, notebook_id):
    t1 = store.create_knowhow_table(notebook_id, "A", "", BASE_COLUMNS)
    t2 = store.create_knowhow_table(notebook_id, "B", "", BASE_COLUMNS)
    store.add_knowhow_row(t1, {})
    store.add_knowhow_row(t1, {})
    store.add_knowhow_row(t2, {})

    listed = {row["id"]: row for row in store.list_knowhow_tables(notebook_id)}
    assert listed[t1]["row_count"] == 2
    assert listed[t2]["row_count"] == 1
    assert listed[t1]["title"] == "A"
    assert listed[t2]["title"] == "B"


def test_list_tables_empty_notebook_returns_empty_list(store, notebook_id):
    assert store.list_knowhow_tables(notebook_id) == []


def test_list_tables_scopes_by_notebook(store, repo):
    nb1 = repo.create_notebook(NotebookCreate(name="n1")).id
    nb2 = repo.create_notebook(NotebookCreate(name="n2")).id
    store.create_knowhow_table(nb1, "A", "", BASE_COLUMNS)
    assert store.list_knowhow_tables(nb2) == []


# ---------------------------------------------------------------------------
# get_knowhow_table
# ---------------------------------------------------------------------------


def test_get_table_raises_key_error_when_missing(store):
    with pytest.raises(KeyError):
        store.get_knowhow_table("does-not-exist")


def test_get_table_orders_columns_and_rows_by_position(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    r_high = store.add_knowhow_row(table_id, {}, position=5)
    r_low = store.add_knowhow_row(table_id, {}, position=1)
    r_mid = store.add_knowhow_row(table_id, {}, position=3)

    detail = store.get_knowhow_table(table_id)
    assert [r["id"] for r in detail["rows"]] == [r_low, r_mid, r_high]
    assert [r["position"] for r in detail["rows"]] == [1, 3, 5]


# ---------------------------------------------------------------------------
# add_knowhow_row
# ---------------------------------------------------------------------------


def test_add_row_default_position_appends_in_order(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    r1 = store.add_knowhow_row(table_id, {})
    r2 = store.add_knowhow_row(table_id, {})
    r3 = store.add_knowhow_row(table_id, {})

    detail = store.get_knowhow_table(table_id)
    assert [r["id"] for r in detail["rows"]] == [r1, r2, r3]
    assert [r["position"] for r in detail["rows"]] == [0, 1, 2]


def test_add_row_explicit_position_is_honored(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    store.add_knowhow_row(table_id, {}, position=7)
    detail = store.get_knowhow_table(table_id)
    assert detail["rows"][0]["position"] == 7


def test_add_row_with_cells_persists_only_provided_columns(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(
        table_id, {cols["违例类型"]: "过冲", cols["现象识别"]: "眼图异常"}
    )

    detail = store.get_knowhow_table(table_id)
    row = detail["rows"][0]
    assert row["id"] == row_id
    assert row["projection_status"] == "pending"
    assert row["cells"] == {cols["违例类型"]: "过冲", cols["现象识别"]: "眼图异常"}
    assert cols["修复方法"] not in row["cells"]


def test_add_row_does_not_bump_table_mutation_seq(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    store.add_knowhow_row(table_id, {})
    assert store.get_knowhow_table(table_id)["mutation_seq"] == 0


# ---------------------------------------------------------------------------
# update_knowhow_cell
# ---------------------------------------------------------------------------


def test_update_cell_upserts_and_bumps_row_and_table_state(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})
    seq0 = store.get_knowhow_table(table_id)["mutation_seq"]

    store.update_knowhow_cell(row_id, cols["修复方法"], "先重新布线")

    after = store.get_knowhow_table(table_id)
    row = after["rows"][0]
    assert row["cells"][cols["修复方法"]] == "先重新布线"
    assert row["projection_status"] == "pending"
    assert after["mutation_seq"] == seq0 + 1


def test_update_cell_is_idempotent_at_storage_level(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})

    store.update_knowhow_cell(row_id, cols["修复方法"], "v1")
    store.update_knowhow_cell(row_id, cols["修复方法"], "v2")
    store.update_knowhow_cell(row_id, cols["修复方法"], "v3")

    assert _cell_count(store, row_id, cols["修复方法"]) == 1
    detail = store.get_knowhow_table(table_id)
    assert detail["rows"][0]["cells"][cols["修复方法"]] == "v3"
    # three upserts on the same cell: three monotonic mutation_seq bumps.
    assert detail["mutation_seq"] == 3


def test_update_cell_resets_projection_status_to_pending(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})
    store.set_knowhow_row_projection(row_id, "synced")
    assert store.get_knowhow_table(table_id)["rows"][0]["projection_status"] == "synced"

    store.update_knowhow_cell(row_id, cols["违例类型"], "过冲")

    assert store.get_knowhow_table(table_id)["rows"][0]["projection_status"] == "pending"


def test_update_cell_resolves_table_id_from_row_without_a_table_id_argument(
    store, notebook_id
):
    t1 = store.create_knowhow_table(notebook_id, "A", "", BASE_COLUMNS)
    t2 = store.create_knowhow_table(notebook_id, "B", "", BASE_COLUMNS)
    cols_t1 = _columns_by_name(store, t1)
    row_t1 = store.add_knowhow_row(t1, {})

    store.update_knowhow_cell(row_t1, cols_t1["违例类型"], "x")

    assert store.get_knowhow_table(t1)["mutation_seq"] == 1
    assert store.get_knowhow_table(t2)["mutation_seq"] == 0


# ---------------------------------------------------------------------------
# update_knowhow_cells (batch — followup A: merged-cell single-transaction
# batch write, spec §6「整组批量写单事务，不半改」)
# ---------------------------------------------------------------------------


def test_update_cells_batch_writes_every_row_and_bumps_state_once(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    row_a = store.add_knowhow_row(table_id, {})
    row_b = store.add_knowhow_row(table_id, {})
    row_c = store.add_knowhow_row(table_id, {})
    seq0 = store.get_knowhow_table(table_id)["mutation_seq"]

    store.update_knowhow_cells([row_a, row_b, row_c], cols["修复方法"], "统一改法")

    after = store.get_knowhow_table(table_id)
    rows_by_id = {row["id"]: row for row in after["rows"]}
    for row_id in (row_a, row_b, row_c):
        assert rows_by_id[row_id]["cells"][cols["修复方法"]] == "统一改法"
        assert rows_by_id[row_id]["projection_status"] == "pending"
    # 批量写只算一次逻辑改动：mutation_seq 只 +1（不是每行各 +1）。
    assert after["mutation_seq"] == seq0 + 1


def test_update_cells_batch_is_idempotent_at_storage_level(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    row_a = store.add_knowhow_row(table_id, {})
    row_b = store.add_knowhow_row(table_id, {})

    store.update_knowhow_cells([row_a, row_b], cols["修复方法"], "v1")
    store.update_knowhow_cells([row_a, row_b], cols["修复方法"], "v2")

    assert _cell_count(store, row_a, cols["修复方法"]) == 1
    assert _cell_count(store, row_b, cols["修复方法"]) == 1
    detail = store.get_knowhow_table(table_id)
    rows_by_id = {row["id"]: row for row in detail["rows"]}
    assert rows_by_id[row_a]["cells"][cols["修复方法"]] == "v2"
    assert rows_by_id[row_b]["cells"][cols["修复方法"]] == "v2"
    assert detail["mutation_seq"] == 2


def test_update_cells_batch_empty_row_ids_is_a_noop(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    seq0 = store.get_knowhow_table(table_id)["mutation_seq"]

    store.update_knowhow_cells([], cols["修复方法"], "x")

    assert store.get_knowhow_table(table_id)["mutation_seq"] == seq0


def test_update_cells_batch_is_all_or_nothing_on_bad_row_id(store, notebook_id):
    """一个事务：批内任一 row_id 不存在(FK 违反)整批回滚，包括排在它前面、
    本会成功的行——事务原子性的核心断言，不是靠调用方预校验。

    ``sqlite3`` 是本文件唯一用到它的地方，就地 import——避免在模块头部插入
    新 import 行，把下面既有 import 挤动行号(test_repository_surface_manifest
    按精确行号冻结了本文件对 SQLiteRepository 的 import 座)。"""
    import sqlite3

    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    row_a = store.add_knowhow_row(table_id, {})
    # 新行默认已是 'pending'，用它来判"回滚"分辨不出改没改过——先显式打成
    # 'synced'，回滚后若仍是 'synced' 才真的证明批内那条 UPDATE 没有生效。
    store.set_knowhow_row_projection(row_a, "synced")
    seq0 = store.get_knowhow_table(table_id)["mutation_seq"]

    with pytest.raises(sqlite3.IntegrityError):
        store.update_knowhow_cells([row_a, "no-such-row"], cols["修复方法"], "不该落库")

    after = store.get_knowhow_table(table_id)
    assert after["rows"][0]["cells"].get(cols["修复方法"], "") == ""
    assert after["rows"][0]["projection_status"] == "synced"
    assert after["mutation_seq"] == seq0


# ---------------------------------------------------------------------------
# set_knowhow_row_projection / set_knowhow_hidden_source / bump_knowhow_mutation_seq
# ---------------------------------------------------------------------------


def test_set_row_projection_updates_status(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    row_id = store.add_knowhow_row(table_id, {})
    assert store.get_knowhow_table(table_id)["rows"][0]["projection_status"] == "pending"

    store.set_knowhow_row_projection(row_id, "synced")
    assert store.get_knowhow_table(table_id)["rows"][0]["projection_status"] == "synced"

    store.set_knowhow_row_projection(row_id, "failed")
    assert store.get_knowhow_table(table_id)["rows"][0]["projection_status"] == "failed"


def test_set_hidden_source_persists(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    assert store.get_knowhow_table(table_id)["hidden_source_id"] is None

    store.set_knowhow_hidden_source(table_id, "src-abc")

    assert store.get_knowhow_table(table_id)["hidden_source_id"] == "src-abc"


def test_bump_mutation_seq_returns_new_value_and_is_monotonic(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    assert store.get_knowhow_table(table_id)["mutation_seq"] == 0

    seq1 = store.bump_knowhow_mutation_seq(table_id)
    seq2 = store.bump_knowhow_mutation_seq(table_id)
    seq3 = store.bump_knowhow_mutation_seq(table_id)

    assert [seq1, seq2, seq3] == [1, 2, 3]
    assert store.get_knowhow_table(table_id)["mutation_seq"] == 3


def test_bump_mutation_seq_missing_table_raises(store):
    with pytest.raises(KeyError):
        store.bump_knowhow_mutation_seq("does-not-exist")


def test_mutation_seq_monotonic_across_cell_updates_and_explicit_bumps(
    store, notebook_id
):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})

    seqs = [store.get_knowhow_table(table_id)["mutation_seq"]]
    store.update_knowhow_cell(row_id, cols["违例类型"], "a")
    seqs.append(store.get_knowhow_table(table_id)["mutation_seq"])
    store.bump_knowhow_mutation_seq(table_id)
    seqs.append(store.get_knowhow_table(table_id)["mutation_seq"])
    store.update_knowhow_cell(row_id, cols["违例类型"], "b")
    seqs.append(store.get_knowhow_table(table_id)["mutation_seq"])

    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


# ---------------------------------------------------------------------------
# delete_knowhow_table
# ---------------------------------------------------------------------------


def test_delete_table_cascades_columns_rows_and_cells(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {cols["违例类型"]: "x"})
    store.set_knowhow_hidden_source(table_id, "src-xyz")

    result = store.delete_knowhow_table(table_id)

    assert result == {"hidden_source_id": "src-xyz"}
    with store.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM knowhow_tables WHERE id=?", (table_id,)
        ).fetchone()["n"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS n FROM knowhow_columns WHERE table_id=?", (table_id,)
        ).fetchone()["n"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS n FROM knowhow_rows WHERE table_id=?", (table_id,)
        ).fetchone()["n"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS n FROM knowhow_cells WHERE row_id=?", (row_id,)
        ).fetchone()["n"] == 0
    with pytest.raises(KeyError):
        store.get_knowhow_table(table_id)


def test_delete_table_without_hidden_source_returns_none(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    assert store.delete_knowhow_table(table_id) == {"hidden_source_id": None}


def test_delete_table_missing_is_a_silent_no_op(store):
    assert store.delete_knowhow_table("does-not-exist") == {"hidden_source_id": None}


# ---------------------------------------------------------------------------
# notebook_assets
# ---------------------------------------------------------------------------


def test_insert_and_get_notebook_asset(store, notebook_id):
    asset_id = store.insert_notebook_asset(
        notebook_id, "a.png", "image/png", 1234, "user-1"
    )
    assert isinstance(asset_id, str) and asset_id

    asset = store.get_notebook_asset(asset_id)
    assert asset["notebook_id"] == notebook_id
    assert asset["filename"] == "a.png"
    assert asset["mime"] == "image/png"
    assert asset["size"] == 1234
    assert asset["created_by"] == "user-1"
    assert asset["created_at"]


def test_get_notebook_asset_missing_returns_none(store):
    assert store.get_notebook_asset("does-not-exist") is None


# ---------------------------------------------------------------------------
# facade one-hop delegation (end-to-end through the SAME runtime-owned store)
# ---------------------------------------------------------------------------


def test_facade_delegates_wire_to_the_runtime_owned_store(repo, notebook_id):
    store = repo._runtime.knowhow_store
    table_id = repo.create_knowhow_table(notebook_id, "T", "desc", BASE_COLUMNS)
    assert isinstance(table_id, str)
    assert store.get_knowhow_table(table_id)["title"] == "T"  # facade write -> store read

    listed = repo.list_knowhow_tables(notebook_id)
    assert listed[0]["id"] == table_id
    assert listed[0]["row_count"] == 0

    row_id = repo.add_knowhow_row(table_id, {})
    detail = repo.get_knowhow_table(table_id)
    assert detail["rows"][0]["id"] == row_id
    cols = {c["name"]: c["id"] for c in detail["columns"]}

    repo.update_knowhow_cell(row_id, cols["违例类型"], "hello")
    assert repo.get_knowhow_table(table_id)["rows"][0]["cells"][cols["违例类型"]] == "hello"

    repo.set_knowhow_row_projection(row_id, "synced")
    assert repo.get_knowhow_table(table_id)["rows"][0]["projection_status"] == "synced"

    repo.set_knowhow_hidden_source(table_id, "src-1")
    assert repo.get_knowhow_table(table_id)["hidden_source_id"] == "src-1"

    seq_before = repo.get_knowhow_table(table_id)["mutation_seq"]
    new_seq = repo.bump_knowhow_mutation_seq(table_id)
    assert new_seq == seq_before + 1

    asset_id = repo.insert_notebook_asset(notebook_id, "b.png", "image/png", 10, "u")
    assert repo.get_notebook_asset(asset_id)["filename"] == "b.png"

    result = repo.delete_knowhow_table(table_id)
    assert result == {"hidden_source_id": "src-1"}
    with pytest.raises(KeyError):
        repo.get_knowhow_table(table_id)


def test_facade_create_table_validation_raises_value_error(repo, notebook_id):
    with pytest.raises(ValueError):
        repo.create_knowhow_table(notebook_id, "T", "", [{"name": "", "role": "anchor"}])


# ---------------------------------------------------------------------------
# Task 1 (knowhow-tables PR-2+3): editing/code-attachment primitives.
# create(created_by) / update_knowhow_table_meta / set_knowhow_anchor_column /
# add·rename·set-kind·delete column / delete row / validate_cell_target /
# knowhow_cell_code CRUD — plus the facade one-hop e2e for all of them.
# ---------------------------------------------------------------------------


def _mk_table(store, notebook_id, title="T"):
    return store.create_knowhow_table(notebook_id, title, "", BASE_COLUMNS)


def test_create_table_persists_created_by(store, notebook_id):
    table_id = store.create_knowhow_table(
        notebook_id, "T", "", BASE_COLUMNS, created_by="user-9"
    )
    assert store.get_knowhow_table(table_id)["created_by"] == "user-9"


def test_create_table_created_by_defaults_to_empty(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    assert store.get_knowhow_table(table_id)["created_by"] == ""


# --- update_knowhow_table_meta ----------------------------------------------


def test_update_table_meta_patches_title_and_description(store, notebook_id):
    table_id = _mk_table(store, notebook_id)

    store.update_knowhow_table_meta(table_id, title="  新标题  ", description="新描述")

    detail = store.get_knowhow_table(table_id)
    assert detail["title"] == "新标题"  # stored stripped, mirroring create
    assert detail["description"] == "新描述"


def test_update_table_meta_none_fields_left_untouched(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    store.update_knowhow_table_meta(table_id, description="只改描述")
    detail = store.get_knowhow_table(table_id)
    assert detail["title"] == "T"
    assert detail["description"] == "只改描述"

    store.update_knowhow_table_meta(table_id, title="只改标题")
    detail = store.get_knowhow_table(table_id)
    assert detail["title"] == "只改标题"
    assert detail["description"] == "只改描述"

    store.update_knowhow_table_meta(table_id)  # all-None: a silent no-op
    assert store.get_knowhow_table(table_id)["title"] == "只改标题"


def test_update_table_meta_rejects_blank_title(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    with pytest.raises(ValueError) as exc:
        store.update_knowhow_table_meta(table_id, title="   ")
    assert "表标题不能为空" in str(exc.value)
    assert store.get_knowhow_table(table_id)["title"] == "T"


def test_update_table_meta_allows_clearing_description(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "有描述", BASE_COLUMNS)
    store.update_knowhow_table_meta(table_id, description="")
    assert store.get_knowhow_table(table_id)["description"] == ""


# --- set_knowhow_anchor_column ------------------------------------------------


def test_set_anchor_moves_anchor_and_returns_old_column_id(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)

    old = store.set_knowhow_anchor_column(table_id, cols["现象识别"])

    assert old == cols["违例类型"]  # BASE_COLUMNS' anchor column
    roles = {c["name"]: c["role"] for c in store.get_knowhow_table(table_id)["columns"]}
    # New anchor promoted; previous anchor demoted to attribute (its original
    # content-kind is not preserved — anchor columns carry no content-kind).
    assert roles == {"违例类型": "attribute", "现象识别": "anchor", "修复方法": "attribute"}


def test_set_anchor_none_clears_and_returns_old(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)

    old = store.set_knowhow_anchor_column(table_id, None)

    assert old == cols["违例类型"]
    roles = [c["role"] for c in store.get_knowhow_table(table_id)["columns"]]
    assert "anchor" not in roles


def test_set_anchor_on_anchorless_table_returns_none(store, notebook_id):
    table_id = store.create_knowhow_table(
        notebook_id, "T", "", [{"name": "A", "role": "attribute"}]
    )
    cols = _columns_by_name(store, table_id)
    assert store.set_knowhow_anchor_column(table_id, cols["A"]) is None
    assert store.set_knowhow_anchor_column(table_id, None) == cols["A"]
    assert store.set_knowhow_anchor_column(table_id, None) is None  # already clear


def test_set_anchor_same_column_is_idempotent(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)
    old = store.set_knowhow_anchor_column(table_id, cols["违例类型"])
    assert old == cols["违例类型"]
    roles = {c["name"]: c["role"] for c in store.get_knowhow_table(table_id)["columns"]}
    assert roles["违例类型"] == "anchor"


def test_set_anchor_rejects_column_from_another_table(store, notebook_id):
    t1 = _mk_table(store, notebook_id, "A")
    t2 = _mk_table(store, notebook_id, "B")
    foreign_col = _columns_by_name(store, t2)["违例类型"]

    with pytest.raises(ValueError) as exc:
        store.set_knowhow_anchor_column(t1, foreign_col)
    assert any("一" <= ch <= "鿿" for ch in str(exc.value))
    # t1's anchor is untouched by the failed attempt.
    roles = {c["name"]: c["role"] for c in store.get_knowhow_table(t1)["columns"]}
    assert roles["违例类型"] == "anchor"


def test_set_anchor_rejects_unknown_column(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    with pytest.raises(ValueError):
        store.set_knowhow_anchor_column(table_id, "no-such-column")


# --- add_knowhow_column --------------------------------------------------------


def test_add_column_appends_at_end_by_default(store, notebook_id):
    table_id = _mk_table(store, notebook_id)

    column_id = store.add_knowhow_column(table_id, "依赖工具", "entity")

    assert isinstance(column_id, str) and column_id
    detail = store.get_knowhow_table(table_id)
    assert [c["name"] for c in detail["columns"]] == [
        "违例类型", "现象识别", "修复方法", "依赖工具"
    ]
    added = detail["columns"][-1]
    assert added["id"] == column_id
    assert added["role"] == "entity"
    assert added["position"] == 3


def test_add_column_explicit_position_is_honored(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    column_id = store.add_knowhow_column(table_id, "首列", "attribute", position=-1)
    detail = store.get_knowhow_table(table_id)
    assert detail["columns"][0]["id"] == column_id
    assert detail["columns"][0]["position"] == -1


def test_add_column_position_survives_gaps_from_deletes(store, notebook_id):
    """Append-position must be MAX(position)+1, not COUNT — after a delete the
    count is smaller than the max and a COUNT-based default would collide."""
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)
    store.delete_knowhow_column(cols["现象识别"])  # positions now 0 and 2

    column_id = store.add_knowhow_column(table_id, "新列", "attribute")

    detail = store.get_knowhow_table(table_id)
    positions = {c["id"]: c["position"] for c in detail["columns"]}
    assert positions[column_id] == 3  # max(0, 2) + 1, NOT count(2)
    assert detail["columns"][-1]["id"] == column_id


def test_add_column_validates_name_and_kind(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    with pytest.raises(ValueError):
        store.add_knowhow_column(table_id, "   ", "attribute")  # blank name
    with pytest.raises(ValueError):
        store.add_knowhow_column(table_id, "违例类型", "attribute")  # duplicate name
    with pytest.raises(ValueError):
        store.add_knowhow_column(table_id, "新列", "bogus")  # unknown kind
    with pytest.raises(ValueError):
        store.add_knowhow_column(table_id, "新列", "anchor")  # anchor only via set_anchor
    assert len(store.get_knowhow_table(table_id)["columns"]) == 3  # nothing written


def test_add_column_does_not_backfill_cells(store, notebook_id):
    """Cells stay sparse (get_knowhow_table's documented contract): adding a
    column must NOT insert empty-string cell rows for existing rows."""
    table_id = _mk_table(store, notebook_id)
    row_id = store.add_knowhow_row(table_id, {})
    column_id = store.add_knowhow_column(table_id, "新列", "attribute")
    assert _cell_count(store, row_id, column_id) == 0


# --- rename_knowhow_column ----------------------------------------------------


def test_rename_column_updates_name(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)

    store.rename_knowhow_column(cols["修复方法"], "  处理方案  ")

    names = [c["name"] for c in store.get_knowhow_table(table_id)["columns"]]
    assert names == ["违例类型", "现象识别", "处理方案"]  # stored stripped


def test_rename_column_rejects_blank_and_duplicate_names(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)
    with pytest.raises(ValueError):
        store.rename_knowhow_column(cols["修复方法"], "   ")
    with pytest.raises(ValueError):
        store.rename_knowhow_column(cols["修复方法"], "违例类型")  # sibling's name
    # Renaming to its own current name is NOT a duplicate — silent success.
    store.rename_knowhow_column(cols["修复方法"], "修复方法")
    names = [c["name"] for c in store.get_knowhow_table(table_id)["columns"]]
    assert names == ["违例类型", "现象识别", "修复方法"]


def test_rename_column_missing_raises_key_error(store):
    with pytest.raises(KeyError):
        store.rename_knowhow_column("no-such-column", "新名")


# --- set_knowhow_column_kind --------------------------------------------------


def test_set_column_kind_updates_kind(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)

    store.set_knowhow_column_kind(cols["修复方法"], "procedure")

    roles = {c["name"]: c["role"] for c in store.get_knowhow_table(table_id)["columns"]}
    assert roles["修复方法"] == "procedure"


def test_set_column_kind_rejects_anchor_and_unknown_kinds(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)
    with pytest.raises(ValueError):
        store.set_knowhow_column_kind(cols["修复方法"], "anchor")  # only via set_anchor
    with pytest.raises(ValueError):
        store.set_knowhow_column_kind(cols["修复方法"], "bogus")
    roles = {c["name"]: c["role"] for c in store.get_knowhow_table(table_id)["columns"]}
    assert roles["修复方法"] == "attribute"  # unchanged by the failed attempts


def test_set_column_kind_missing_raises_key_error(store):
    with pytest.raises(KeyError):
        store.set_knowhow_column_kind("no-such-column", "attribute")


# --- delete_knowhow_column / delete_knowhow_row (cascades) --------------------


def test_delete_column_cascades_cells_and_cell_code(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {cols["修复方法"]: "内容"})
    store.upsert_knowhow_cell_code(
        row_id, cols["修复方法"], "print(1)", "python", "agent", "hash1"
    )

    store.delete_knowhow_column(cols["修复方法"])

    detail = store.get_knowhow_table(table_id)
    assert [c["name"] for c in detail["columns"]] == ["违例类型", "现象识别"]
    assert _cell_count(store, row_id, cols["修复方法"]) == 0
    assert store.get_knowhow_cell_code(row_id, cols["修复方法"]) is None
    # The row itself survives — only the column's slice is gone.
    assert [r["id"] for r in detail["rows"]] == [row_id]


def test_delete_column_missing_is_a_silent_noop(store):
    store.delete_knowhow_column("no-such-column")  # must not raise


def test_delete_row_cascades_cells_and_cell_code(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {cols["违例类型"]: "过冲"})
    store.upsert_knowhow_cell_code(
        row_id, cols["违例类型"], "print(1)", "python", "agent", "hash1"
    )
    keep_row = store.add_knowhow_row(table_id, {cols["违例类型"]: "欠冲"})

    store.delete_knowhow_row(row_id)

    detail = store.get_knowhow_table(table_id)
    assert [r["id"] for r in detail["rows"]] == [keep_row]
    assert _cell_count(store, row_id, cols["违例类型"]) == 0
    assert store.get_knowhow_cell_code(row_id, cols["违例类型"]) is None


def test_delete_row_missing_is_a_silent_noop(store):
    store.delete_knowhow_row("no-such-row")  # must not raise


# --- validate_cell_target -----------------------------------------------------


def test_validate_cell_target_accepts_matching_row_and_column(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})
    store.validate_cell_target(row_id, cols["修复方法"])  # must not raise


def test_validate_cell_target_rejects_cross_table_pairs(store, notebook_id):
    t1 = _mk_table(store, notebook_id, "A")
    t2 = _mk_table(store, notebook_id, "B")
    row_t1 = store.add_knowhow_row(t1, {})
    col_t2 = _columns_by_name(store, t2)["修复方法"]

    with pytest.raises(ValueError) as exc:
        store.validate_cell_target(row_t1, col_t2)
    assert "格子定位不合法" in str(exc.value)


def test_validate_cell_target_rejects_missing_row_or_column(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})
    with pytest.raises(ValueError):
        store.validate_cell_target("no-such-row", cols["修复方法"])
    with pytest.raises(ValueError):
        store.validate_cell_target(row_id, "no-such-column")


# --- knowhow_cell_code CRUD ----------------------------------------------------


def test_upsert_cell_code_inserts_then_updates_in_place(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})

    code_id = store.upsert_knowhow_cell_code(
        row_id, cols["修复方法"], "print(1)", "python", "agent-1", "hash-v1"
    )
    assert isinstance(code_id, str) and code_id

    first = store.get_knowhow_cell_code(row_id, cols["修复方法"])
    assert first["id"] == code_id
    assert first["code_text"] == "print(1)"
    assert first["language"] == "python"
    assert first["updated_by"] == "agent-1"
    assert first["cell_content_hash"] == "hash-v1"
    assert first["created_at"] and first["updated_at"]

    code_id_2 = store.upsert_knowhow_cell_code(
        row_id, cols["修复方法"], "puts 2", "tcl", "agent-2", "hash-v2"
    )
    assert code_id_2 == code_id  # UNIQUE(row_id, column_id) -> stable id

    second = store.get_knowhow_cell_code(row_id, cols["修复方法"])
    assert second["id"] == code_id
    assert second["code_text"] == "puts 2"
    assert second["language"] == "tcl"
    assert second["updated_by"] == "agent-2"
    assert second["cell_content_hash"] == "hash-v2"
    assert second["created_at"] == first["created_at"]  # preserved across upserts

    with store.database.connect() as db:
        n = db.execute(
            "SELECT COUNT(*) AS n FROM knowhow_cell_code WHERE row_id = ?", (row_id,)
        ).fetchone()["n"]
    assert n == 1  # upsert, not append


def test_get_cell_code_missing_returns_none(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})
    assert store.get_knowhow_cell_code(row_id, cols["修复方法"]) is None


def test_delete_cell_code_removes_row_and_is_noop_when_missing(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})
    store.upsert_knowhow_cell_code(
        row_id, cols["修复方法"], "print(1)", "python", "u", "h"
    )

    store.delete_knowhow_cell_code(row_id, cols["修复方法"])
    assert store.get_knowhow_cell_code(row_id, cols["修复方法"]) is None

    store.delete_knowhow_cell_code(row_id, cols["修复方法"])  # second time: silent no-op


def test_list_cell_code_scopes_by_table_in_row_then_column_order(store, notebook_id):
    t1 = _mk_table(store, notebook_id, "A")
    t2 = _mk_table(store, notebook_id, "B")
    cols_t1 = _columns_by_name(store, t1)
    cols_t2 = _columns_by_name(store, t2)
    row_a = store.add_knowhow_row(t1, {})
    row_b = store.add_knowhow_row(t1, {})
    row_other = store.add_knowhow_row(t2, {})

    # Insert out of positional order to prove the ordering is by (row
    # position, column position), not insertion order.
    store.upsert_knowhow_cell_code(row_b, cols_t1["违例类型"], "b1", "", "", "h")
    store.upsert_knowhow_cell_code(row_a, cols_t1["修复方法"], "a2", "", "", "h")
    store.upsert_knowhow_cell_code(row_a, cols_t1["违例类型"], "a1", "", "", "h")
    store.upsert_knowhow_cell_code(row_other, cols_t2["违例类型"], "x", "", "", "h")

    listed = store.list_knowhow_cell_code(t1)
    assert [(item["row_id"], item["code_text"]) for item in listed] == [
        (row_a, "a1"), (row_a, "a2"), (row_b, "b1"),
    ]
    assert all(set(item) >= {
        "id", "row_id", "column_id", "code_text", "language",
        "updated_by", "cell_content_hash", "created_at", "updated_at",
    } for item in listed)


def test_list_cell_code_empty_table_returns_empty_list(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    assert store.list_knowhow_cell_code(table_id) == []


def test_delete_table_cascades_cell_code_too(store, notebook_id):
    table_id = _mk_table(store, notebook_id)
    cols = _columns_by_name(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})
    store.upsert_knowhow_cell_code(row_id, cols["修复方法"], "c", "", "", "h")

    store.delete_knowhow_table(table_id)

    with store.database.connect() as db:
        n = db.execute(
            "SELECT COUNT(*) AS n FROM knowhow_cell_code WHERE row_id = ?", (row_id,)
        ).fetchone()["n"]
    assert n == 0


# --- facade one-hop delegation for the PR-2+3 members --------------------------


def test_facade_delegates_editing_and_code_members(repo, notebook_id):
    table_id = repo.create_knowhow_table(
        notebook_id, "T", "", BASE_COLUMNS, created_by="user-7"
    )
    assert repo.get_knowhow_table(table_id)["created_by"] == "user-7"

    repo.update_knowhow_table_meta(table_id, title="新标题", description="d2")
    detail = repo.get_knowhow_table(table_id)
    assert (detail["title"], detail["description"]) == ("新标题", "d2")

    cols = {c["name"]: c["id"] for c in detail["columns"]}
    old_anchor = repo.set_knowhow_anchor_column(table_id, cols["现象识别"])
    assert old_anchor == cols["违例类型"]

    new_col = repo.add_knowhow_column(table_id, "依赖工具", "entity")
    repo.rename_knowhow_column(new_col, "工具清单")
    repo.set_knowhow_column_kind(new_col, "attribute")
    columns = repo.get_knowhow_table(table_id)["columns"]
    added = next(c for c in columns if c["id"] == new_col)
    assert (added["name"], added["role"]) == ("工具清单", "attribute")

    row_id = repo.add_knowhow_row(table_id, {new_col: "内容"})
    repo.validate_cell_target(row_id, new_col)

    code_id = repo.upsert_knowhow_cell_code(row_id, new_col, "print(1)", "python", "u", "h")
    assert repo.get_knowhow_cell_code(row_id, new_col)["id"] == code_id
    assert [c["id"] for c in repo.list_knowhow_cell_code(table_id)] == [code_id]
    repo.delete_knowhow_cell_code(row_id, new_col)
    assert repo.get_knowhow_cell_code(row_id, new_col) is None

    repo.delete_knowhow_column(new_col)
    assert all(c["id"] != new_col for c in repo.get_knowhow_table(table_id)["columns"])
    repo.delete_knowhow_row(row_id)
    assert repo.get_knowhow_table(table_id)["rows"] == []


# ---------------------------------------------------------------------------
# F1 (P1 code review): the guarded compare-and-write must be CROSS-PROCESS
# atomic. SQLite (pysqlite isolation_level="") opens the transaction lazily at
# the first DML, so the phase-1 ``SELECT current`` re-reads run OUTSIDE the
# write transaction; SqliteDatabase.write()'s ``write_lock`` is only
# PROCESS-local. A second PROCESS (a live backend beside the CLI, another
# worker) could commit a write BETWEEN the compare and this method's own write,
# and the guarded write would clobber it on top -- the advertised
# compare-and-write is then not atomic across processes. The fix issues
# ``BEGIN IMMEDIATE`` before any read so the whole read-compare-write runs under
# a real RESERVED lock. These tests pin (a) the trace: ``BEGIN IMMEDIATE`` is
# emitted before the first SELECT of the guarded path, and (b) the behaviour: a
# second raw connection's own ``BEGIN IMMEDIATE`` is refused (SQLITE_BUSY) while
# the guarded transaction is mid-flight -- proof the reserved lock is actually
# held across the compare, not merely acquired at the trailing write.
# ---------------------------------------------------------------------------


def _one_cell(store: KnowhowStore, notebook_id: str) -> tuple[str, str, str, str]:
    """A table with one procedure column and one row whose cell holds ``old``.
    Returns ``(table_id, row_id, column_id, "old")`` -- the coordinates the
    guarded methods take as ``(table_id, row_id, column_id, expected_before,
    content_md)``."""
    table_id = store.create_knowhow_table(
        notebook_id, "T", "", [{"name": "修复", "role": "procedure"}]
    )
    column_id = store.get_knowhow_table(table_id)["columns"][0]["id"]
    row_id = store.add_knowhow_row(table_id, {column_id: "old"})
    return table_id, row_id, column_id, "old"


def _trace_write_connections(store: KnowhowStore, monkeypatch) -> list[str]:
    """Attach a statement tracer to every write connection SqliteDatabase.write()
    opens from now on, returning the shared list the tracer appends to. The
    callback is set AFTER ``_new_connection`` finishes (past its PRAGMAs), so the
    list starts at the first statement the guarded body itself issues."""
    statements: list[str] = []
    original = store.database._new_connection

    def traced() -> sqlite3.Connection:
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store.database, "_new_connection", traced)
    return statements


def _assert_begin_immediate_before_first_select(statements: list[str]) -> None:
    upper = [s.strip().upper() for s in statements]
    assert any("BEGIN IMMEDIATE" in s for s in upper), (
        "guarded path never issued BEGIN IMMEDIATE -> its phase-1 reads run "
        f"outside any write transaction. Traced: {statements}"
    )
    begin_idx = next(i for i, s in enumerate(upper) if "BEGIN IMMEDIATE" in s)
    select_idx = next(i for i, s in enumerate(upper) if s.startswith("SELECT"))
    assert begin_idx < select_idx, (
        "BEGIN IMMEDIATE must precede the first SELECT so the compare re-reads "
        f"under the reserved lock. Traced: {statements}"
    )


def test_guarded_atomic_emits_begin_immediate_before_first_select(store, notebook_id, monkeypatch):
    table_id, row_id, column_id, before = _one_cell(store, notebook_id)
    statements = _trace_write_connections(store, monkeypatch)

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id, [(table_id, row_id, column_id, before, "new")]
    )

    assert result == {"written": [(row_id, column_id)], "conflict": False}
    _assert_begin_immediate_before_first_select(statements)


def test_bulk_guarded_emits_begin_immediate_before_first_select(store, notebook_id, monkeypatch):
    table_id, row_id, column_id, before = _one_cell(store, notebook_id)
    statements = _trace_write_connections(store, monkeypatch)

    result = store.update_knowhow_cells_bulk_guarded(
        notebook_id, [(table_id, row_id, column_id, before, "new")]
    )

    assert result["written"] == [(row_id, column_id)]
    _assert_begin_immediate_before_first_select(statements)


def _lock_probe(store: KnowhowStore, monkeypatch) -> dict:
    """Hook ``store.new_id`` (called only in the WRITE phase, i.e. AFTER the
    compare re-reads) so that the FIRST time the guarded body reaches its write
    phase, a second raw connection with a zero busy-timeout attempts its own
    ``BEGIN IMMEDIATE``. If the guarded transaction holds the reserved lock
    (post-fix) that attempt raises SQLITE_BUSY; otherwise it succeeds, proving
    the compare ran with no write lock held. Records the outcome and delegates
    to the real id generator so the write still lands."""
    seen: dict = {}
    db_path = str(store.database.db_path)
    original_new_id = store.new_id

    def probing_new_id(prefix: str) -> str:
        if "locked" not in seen:
            second = sqlite3.connect(db_path, timeout=0)
            try:
                second.execute("BEGIN IMMEDIATE")
                seen["locked"] = False
                second.rollback()
            except sqlite3.OperationalError:
                seen["locked"] = True
            finally:
                second.close()
        return original_new_id(prefix)

    monkeypatch.setattr(store, "new_id", probing_new_id)
    return seen


def test_guarded_atomic_holds_reserved_lock_across_compare_and_write(store, notebook_id, monkeypatch):
    table_id, row_id, column_id, before = _one_cell(store, notebook_id)
    seen = _lock_probe(store, monkeypatch)

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id, [(table_id, row_id, column_id, before, "new")]
    )

    assert result == {"written": [(row_id, column_id)], "conflict": False}
    assert seen.get("locked") is True, (
        "a second connection could BEGIN IMMEDIATE mid-transaction -> the "
        "guarded compare-and-write holds no cross-process write lock"
    )


def test_bulk_guarded_holds_reserved_lock_across_compare_and_write(store, notebook_id, monkeypatch):
    table_id, row_id, column_id, before = _one_cell(store, notebook_id)
    seen = _lock_probe(store, monkeypatch)

    result = store.update_knowhow_cells_bulk_guarded(
        notebook_id, [(table_id, row_id, column_id, before, "new")]
    )

    assert result["written"] == [(row_id, column_id)]
    assert seen.get("locked") is True, (
        "a second connection could BEGIN IMMEDIATE mid-transaction -> the "
        "guarded compare-and-write holds no cross-process write lock"
    )


# ---------------------------------------------------------------------------
# Concurrency P1 (round-4 followup): ANCHOR BASELINE in the atomic guard. The
# editor's batch fan-out writes a merged/shared cell to every branch row of an
# anchor group using row ids FROZEN when the modal opened. If another client
# moves a sibling row's ANCHOR out of that group but leaves the EDITED cell
# untouched, expected_before still matches and membership still holds -- the old
# guard let the candidate land on a row that no longer belongs to the group.
# The fix threads an OPTIONAL per-row anchor baseline (anchor_column_id +
# expected_anchor, positionally parallel to updates, mirroring expected_before):
# re-read each target row's anchor cell in the SAME BEGIN IMMEDIATE, and a
# mismatch is a conflict identical to a stale expected_before (whole batch
# refused, nothing written). Omitting the anchor guard is byte-identical legacy.
# ---------------------------------------------------------------------------


def _anchor_group(store: KnowhowStore, notebook_id: str):
    """A table with an anchor column ('违例类型') + a shared 'attribute' column
    ('修复方法'), and TWO rows in the SAME anchor group (same anchor value, same
    shared value) -- the merged-cell fan-out shape the batch-reformat save writes
    as one guarded atomic call. Returns ``(table_id, anchor_col, shared_col, r0,
    r1, anchor_val, shared_val)``."""
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    anchor_col, shared_col = cols["违例类型"], cols["修复方法"]
    r0 = store.add_knowhow_row(table_id, {anchor_col: "示波器", shared_col: "旧改法"})
    r1 = store.add_knowhow_row(table_id, {anchor_col: "示波器", shared_col: "旧改法"})
    return table_id, anchor_col, shared_col, r0, r1, "示波器", "旧改法"


def test_atomic_anchor_guard_rejects_parallel_length_mismatch(store, notebook_id):
    table_id, anchor_col, shared_col, r0, r1, anchor, before = _anchor_group(
        store, notebook_id
    )
    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [
            (table_id, r0, shared_col, before, "new"),
            (table_id, r1, shared_col, before, "new"),
        ],
        anchor_column_id=anchor_col,
        expected_anchor=[anchor],
    )
    assert result == {"written": [], "conflict": True}
    rows = {
        row["id"]: row["cells"]
        for row in store.get_knowhow_table(table_id)["rows"]
    }
    assert rows[r0][shared_col] == before
    assert rows[r1][shared_col] == before


def test_atomic_anchor_guard_rejects_anchor_column_from_another_table(
    store, notebook_id
):
    target = store.create_knowhow_table(notebook_id, "target", "", BASE_COLUMNS)
    target_cols = _columns_by_name(store, target)
    row_id = store.add_knowhow_row(
        target,
        {target_cols["修复方法"]: "old"},
    )

    foreign = store.create_knowhow_table(notebook_id, "foreign", "", BASE_COLUMNS)
    foreign_anchor = _columns_by_name(store, foreign)["违例类型"]

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [
            (
                target,
                row_id,
                target_cols["修复方法"],
                "old",
                "new",
            )
        ],
        anchor_column_id=foreign_anchor,
        expected_anchor=[""],
    )
    assert result == {"written": [], "conflict": True}
    row = store.get_knowhow_table(target)["rows"][0]
    assert row["cells"][target_cols["修复方法"]] == "old"


def test_guarded_atomic_anchor_mismatch_conflicts_writes_nothing(store, notebook_id):
    table_id, anchor_col, shared_col, r0, r1, anchor_val, shared_val = _anchor_group(
        store, notebook_id
    )
    # Another client moves r1 OUT of the group by changing only its anchor cell;
    # the edited (shared) cell is untouched, so expected_before below still matches.
    store.update_knowhow_cell(r1, anchor_col, "万用表")

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [
            (table_id, r0, shared_col, shared_val, "新改法"),
            (table_id, r1, shared_col, shared_val, "新改法"),
        ],
        anchor_column_id=anchor_col,
        expected_anchor=[anchor_val, anchor_val],  # frozen snapshot: both were 示波器
    )

    assert result == {"written": [], "conflict": True}
    rows = {r["id"]: r["cells"] for r in store.get_knowhow_table(table_id)["rows"]}
    assert rows[r0][shared_col] == shared_val  # all-or-nothing: nothing written
    assert rows[r1][shared_col] == shared_val


def test_guarded_atomic_anchor_match_writes_all(store, notebook_id):
    table_id, anchor_col, shared_col, r0, r1, anchor_val, shared_val = _anchor_group(
        store, notebook_id
    )

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [
            (table_id, r0, shared_col, shared_val, "新改法"),
            (table_id, r1, shared_col, shared_val, "新改法"),
        ],
        anchor_column_id=anchor_col,
        expected_anchor=[anchor_val, anchor_val],  # anchors unchanged -> proceed
    )

    assert result["conflict"] is False
    assert result["written"] == [(r0, shared_col), (r1, shared_col)]
    rows = {r["id"]: r["cells"] for r in store.get_knowhow_table(table_id)["rows"]}
    assert rows[r0][shared_col] == "新改法"
    assert rows[r1][shared_col] == "新改法"


def test_guarded_atomic_omitted_anchor_guard_is_legacy(store, notebook_id):
    table_id, anchor_col, shared_col, r0, r1, anchor_val, shared_val = _anchor_group(
        store, notebook_id
    )
    # Same "sibling anchor moved" situation as the mismatch test...
    store.update_knowhow_cell(r1, anchor_col, "万用表")

    # ...but with NO anchor guard supplied: the anchor is never re-read, so the
    # write proceeds exactly as before this fix (byte-identical legacy behavior).
    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [
            (table_id, r0, shared_col, shared_val, "新改法"),
            (table_id, r1, shared_col, shared_val, "新改法"),
        ],
    )

    assert result["conflict"] is False
    assert result["written"] == [(r0, shared_col), (r1, shared_col)]
    rows = {r["id"]: r["cells"] for r in store.get_knowhow_table(table_id)["rows"]}
    assert rows[r0][shared_col] == "新改法"
    assert rows[r1][shared_col] == "新改法"


# ---------------------------------------------------------------------------
# Concurrency P1 (round-5): the per-row byte-exact anchor re-read above catches a
# FROZEN target that LEFT the group, but two structural drifts also make the
# fan-out a partial/misaimed group write while EVERY frozen row still matches its
# snapshot: (a) the table's ANCHOR DESIGNATION moved to a DIFFERENT column after
# the snapshot (the guarded column's cells all still equal their frozen values,
# but the logical grouping is now defined by another column); and (b) a NEW row
# JOINED the group (its anchor cell set to the group value by another client) --
# every frozen id still matches, but the write now covers only a SUBSET of the
# current logical group. The round-5 fix adds, inside the SAME BEGIN IMMEDIATE
# before any write, a DESIGNATION check (anchor_column_id must still be the
# table's role=='anchor' column) and an EXACT-MEMBERSHIP check (for each distinct
# frozen anchor VALUE, the current member row-id set -- rows whose anchor cell
# TRIM-equals that value, mirroring the frontend groupRowsByAnchor -- must EQUAL
# the frozen target set). Both refuse the whole batch (409, nothing written).
# ---------------------------------------------------------------------------


def test_guarded_atomic_anchor_designation_moved_conflicts_writes_nothing(store, notebook_id):
    table_id, anchor_col, shared_col, r0, r1, anchor_val, shared_val = _anchor_group(
        store, notebook_id
    )
    other_col = _columns_by_name(store, table_id)["现象识别"]
    # Another client moves the anchor DESIGNATION to a different column; the OLD
    # anchor column's cell values are untouched (both rows still read 示波器), so
    # the per-row byte-exact anchor re-read passes -- only the designation moved.
    store.set_knowhow_anchor_column(table_id, other_col)

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [
            (table_id, r0, shared_col, shared_val, "新改法"),
            (table_id, r1, shared_col, shared_val, "新改法"),
        ],
        anchor_column_id=anchor_col,  # the column that WAS the anchor at snapshot
        expected_anchor=[anchor_val, anchor_val],
    )

    assert result == {"written": [], "conflict": True}
    rows = {r["id"]: r["cells"] for r in store.get_knowhow_table(table_id)["rows"]}
    assert rows[r0][shared_col] == shared_val  # all-or-nothing: nothing written
    assert rows[r1][shared_col] == shared_val


def test_guarded_atomic_new_row_joined_group_conflicts_writes_nothing(store, notebook_id):
    table_id, anchor_col, shared_col, r0, r1, anchor_val, shared_val = _anchor_group(
        store, notebook_id
    )
    # Another client adds a NEW row whose anchor cell TRIM-equals the group value
    # (surrounding whitespace variance) -- the frontend would show it merged into
    # this group, but the fan-out froze only {r0, r1}. Every frozen row still
    # matches its baseline; only the membership grew. TRIM here is load-bearing:
    # a byte-exact member scan would MISS this joiner ("  示波器 " != "示波器").
    r2 = store.add_knowhow_row(table_id, {anchor_col: "  示波器 ", shared_col: "别的"})

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [
            (table_id, r0, shared_col, shared_val, "新改法"),
            (table_id, r1, shared_col, shared_val, "新改法"),
        ],
        anchor_column_id=anchor_col,
        expected_anchor=[anchor_val, anchor_val],
    )

    assert result == {"written": [], "conflict": True}
    rows = {r["id"]: r["cells"] for r in store.get_knowhow_table(table_id)["rows"]}
    assert rows[r0][shared_col] == shared_val  # nothing written
    assert rows[r1][shared_col] == shared_val
    assert rows[r2][shared_col] == "别的"


def test_guarded_atomic_membership_trims_whitespace_variant_member(store, notebook_id):
    # A member whose STORED anchor differs from the group value only by
    # surrounding whitespace is still an in-group member (frontend groups by
    # .trim()): the membership check must NOT flag it as drift. Both rows' frozen
    # anchor snapshots are their byte-exact stored values (so the per-row
    # byte-exact re-read also passes), yet they TRIM to the SAME key -> the
    # current member set equals the frozen set -> the write proceeds. (This fails
    # if the membership check trims the current members but not the frozen keys,
    # which would split {r0, r1} into two singleton groups and falsely conflict.)
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    anchor_col, shared_col = cols["违例类型"], cols["修复方法"]
    r0 = store.add_knowhow_row(table_id, {anchor_col: "示波器", shared_col: "旧改法"})
    r1 = store.add_knowhow_row(table_id, {anchor_col: " 示波器 ", shared_col: "旧改法"})

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [
            (table_id, r0, shared_col, "旧改法", "新改法"),
            (table_id, r1, shared_col, "旧改法", "新改法"),
        ],
        anchor_column_id=anchor_col,
        expected_anchor=["示波器", " 示波器 "],  # byte-exact snapshots; trim-equal
    )

    assert result["conflict"] is False
    assert result["written"] == [(r0, shared_col), (r1, shared_col)]
    rows = {r["id"]: r["cells"] for r in store.get_knowhow_table(table_id)["rows"]}
    assert rows[r0][shared_col] == "新改法"
    assert rows[r1][shared_col] == "新改法"


# ---------------------------------------------------------------------------
# Concurrency P1 (round-7 P2 followups): two defects in the anchor guard.
# F1 -- a row created with EMPTY cells has NO knowhow_cells row for the anchor
# column. get_knowhow_table represents that absent cell as "" (the column key is
# simply missing from `cells`, read as ""), so the frontend/wire send
# expected_anchor="" for it. The per-row anchor baseline read must map an ABSENT
# cell to "" (not None) to match that wire, else "" != None is a PERMANENT 409 and
# a blank-anchor row can never be batch-saved -- even right after a reload.
# F2 -- the structural membership check rebuilt its map by scanning the table's
# ENTIRE anchor column per guarded call (O(R) each). A unique-anchor table makes
# every changed cell its own singleton coversAnchorGroup unit -> R×C guarded calls
# -> O(R²C), all inside the write lock. The fix fetches only LIKE-substring
# CANDIDATES for each distinct frozen key (the trimmed core is a contiguous
# substring of any raw value that trim-equals it -> LIKE never UNDER-matches a true
# member; over-matches are dropped by the exact _anchor_trim filter).
# ---------------------------------------------------------------------------


def test_guarded_atomic_absent_anchor_cell_compares_as_blank(store, notebook_id):
    # F1: a row whose anchor cell was never written (absent knowhow_cells row) must
    # compare equal to the wire's expected_anchor="" -- the per-row baseline read
    # maps the missing DB row to "" (mirroring get_knowhow_table / the frontend's
    # (row.cells[anchorColumnId] ?? "").trim()), not None. RED before the fix:
    # None != "" -> a deterministic 409 that no reload can clear.
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    anchor_col, shared_col = cols["违例类型"], cols["修复方法"]
    # Only the shared column is given -> the anchor column has NO cell row.
    row_id = store.add_knowhow_row(table_id, {shared_col: "旧改法"})
    assert _cell_count(store, row_id, anchor_col) == 0  # precondition: absent

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [(table_id, row_id, shared_col, "旧改法", "新改法")],
        anchor_column_id=anchor_col,
        expected_anchor=[""],  # the wire's representation of the absent anchor
    )

    assert result["conflict"] is False
    assert result["written"] == [(row_id, shared_col)]
    rows = {r["id"]: r["cells"] for r in store.get_knowhow_table(table_id)["rows"]}
    assert rows[row_id][shared_col] == "新改法"


def test_guarded_atomic_stored_empty_anchor_compares_as_blank(store, notebook_id):
    # F1 sibling lock: an anchor cell that EXISTS but stores "" (content_md is NOT
    # NULL DEFAULT '') behaves identically to an absent one -- both are "" to the
    # per-row baseline read, so expected_anchor="" passes and the write lands.
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    anchor_col, shared_col = cols["违例类型"], cols["修复方法"]
    row_id = store.add_knowhow_row(table_id, {anchor_col: "", shared_col: "旧改法"})
    assert _cell_count(store, row_id, anchor_col) == 1  # precondition: stored ""

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [(table_id, row_id, shared_col, "旧改法", "新改法")],
        anchor_column_id=anchor_col,
        expected_anchor=[""],
    )

    assert result["conflict"] is False
    assert result["written"] == [(row_id, shared_col)]


def test_guarded_atomic_blank_anchor_rows_never_form_a_membership_group(store, notebook_id):
    # F1/round-5 lock: a blank-after-trim anchor is its OWN group on the frontend
    # (never aggregated), so the structural membership check skips blank keys. Rows
    # with blank anchors are NOT a group -- a third blank-anchor "joiner" must NOT
    # trip the membership guard, and both frozen rows still write. (Uses only STORED
    # blank anchors so it locks the no-aggregation behavior green before AND after.)
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    anchor_col, shared_col = cols["违例类型"], cols["修复方法"]
    r0 = store.add_knowhow_row(table_id, {anchor_col: "", shared_col: "旧改法"})
    r1 = store.add_knowhow_row(table_id, {anchor_col: "  ", shared_col: "旧改法"})
    # Extra blank-anchor row: if blanks aggregated it would be an unexpected member
    # of the "" group and force a conflict; blanks are per-row, so it is ignored.
    store.add_knowhow_row(table_id, {anchor_col: "\t", shared_col: "别的"})

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [
            (table_id, r0, shared_col, "旧改法", "新改法"),
            (table_id, r1, shared_col, "旧改法", "新改法"),
        ],
        anchor_column_id=anchor_col,
        expected_anchor=["", "  "],  # both blank-after-trim
    )

    assert result["conflict"] is False
    assert result["written"] == [(r0, shared_col), (r1, shared_col)]


def test_guarded_atomic_membership_query_uses_normalized_anchor_index(store, notebook_id):
    # The write-transaction membership lookup is a hot path: a reformat of R
    # singleton groups and C columns invokes it R×C times. Its observable query
    # plan must therefore seek the normalized-anchor expression index, rather
    # than scanning knowhow_cells once per save unit. The expression is the
    # ECMAScript String.prototype.trim() code-point set; the production helper
    # owns this spelling once the migration/store change lands.
    table_id, anchor_col, _shared_col, _r0, _r1, anchor_val, _shared_val = _anchor_group(
        store, notebook_id
    )
    with store.database.connect() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT c.row_id "
            "FROM knowhow_cells c JOIN knowhow_rows r ON r.id = c.row_id "
            "WHERE c.column_id = ? "
            f"AND {sqlite_js_trim_expression('c.content_md')} = ? "
            "AND r.table_id = ?",
            (anchor_col, anchor_val, table_id),
        ).fetchall()

    details = [row["detail"] for row in plan]
    assert any(
        "idx_knowhow_cells_column_normalized_anchor_row" in detail
        for detail in details
    ), details
    assert not any("SCAN " in detail for detail in details), details


@pytest.mark.parametrize("padding", list(JS_TRIM_WHITESPACE))
def test_sql_normalized_anchor_matches_js_trim_for_every_included_codepoint(store, padding):
    value = f"{padding}anchor{padding}"
    with store.database.connect() as db:
        normalized = db.execute(
            f"SELECT {sqlite_js_trim_expression('?')}", (value,)
        ).fetchone()[0]
    assert js_trim(value) == "anchor"
    assert normalized == "anchor"


@pytest.mark.parametrize("padding", ["\x1c", "\x1d", "\x1e", "\x1f", "\x85"])
def test_sql_normalized_anchor_keeps_js_trim_excluded_edge_cases(store, padding):
    value = f"{padding}anchor{padding}"
    with store.database.connect() as db:
        normalized = db.execute(
            f"SELECT {sqlite_js_trim_expression('?')}", (value,)
        ).fetchone()[0]
    assert js_trim(value) == value
    assert normalized == value


def test_guarded_atomic_joiner_with_exotic_whitespace_padding_detected(store, notebook_id):
    # The expression index applies the same ECMAScript trim as the frontend, so a
    # joiner padded by U+3000 IDEOGRAPHIC SPACE has the same normalized key as
    # "示波器". Membership grows beyond frozen {r0, r1} and must conflict.
    table_id, anchor_col, shared_col, r0, r1, anchor_val, shared_val = _anchor_group(
        store, notebook_id
    )
    r2 = store.add_knowhow_row(table_id, {anchor_col: "　示波器", shared_col: "别的"})

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [
            (table_id, r0, shared_col, shared_val, "新改法"),
            (table_id, r1, shared_col, shared_val, "新改法"),
        ],
        anchor_column_id=anchor_col,
        expected_anchor=[anchor_val, anchor_val],
    )

    assert result == {"written": [], "conflict": True}
    rows = {r["id"]: r["cells"] for r in store.get_knowhow_table(table_id)["rows"]}
    assert rows[r0][shared_col] == shared_val  # nothing written
    assert rows[r1][shared_col] == shared_val
    assert rows[r2][shared_col] == "别的"


def test_guarded_atomic_membership_treats_like_wildcards_as_literal_anchor_text(store, notebook_id):
    # Membership is equality on the normalized expression, so LIKE metacharacters
    # and backslashes are ordinary anchor text. A group carrying all three writes
    # correctly while a wildcard-shaped decoy remains outside the group.
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_name(store, table_id)
    anchor_col, shared_col = cols["违例类型"], cols["修复方法"]
    tricky = "a_b%c\\d"  # underscore + percent + backslash
    r0 = store.add_knowhow_row(table_id, {anchor_col: tricky, shared_col: "旧改法"})
    r1 = store.add_knowhow_row(table_id, {anchor_col: tricky, shared_col: "旧改法"})
    # It resembles a wildcard match but is not the literal normalized anchor.
    decoy = store.add_knowhow_row(table_id, {anchor_col: "aXbYcZd", shared_col: "别的"})

    result = store.update_knowhow_cells_guarded_atomic(
        notebook_id,
        [
            (table_id, r0, shared_col, "旧改法", "新改法"),
            (table_id, r1, shared_col, "旧改法", "新改法"),
        ],
        anchor_column_id=anchor_col,
        expected_anchor=[tricky, tricky],
    )

    assert result["conflict"] is False
    assert result["written"] == [(r0, shared_col), (r1, shared_col)]
    rows = {r["id"]: r["cells"] for r in store.get_knowhow_table(table_id)["rows"]}
    assert rows[r0][shared_col] == "新改法"
    assert rows[r1][shared_col] == "新改法"
    assert rows[decoy][shared_col] == "别的"  # decoy never joined the group
