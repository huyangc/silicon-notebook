"""Task 2 (knowhow-tables PR-1): knowhow_store repository module + facade
composition. Covers every ``KnowhowStore`` method (row-level, direct) plus
one end-to-end test proving the facade's one-hop delegates reach the SAME
runtime-owned store. Task 5's projector and Task 6's import/table API build
directly on these exact names/signatures.
See docs/superpowers/plans/2026-07-15-knowhow-tables-pr1.md Task 2.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
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
    {"name": "违例类型", "role": "concept"},
    {"name": "现象识别", "role": "identify"},
    {"name": "修复方法", "role": "fix"},
]


def _columns_by_role(store: KnowhowStore, table_id: str) -> dict[str, str]:
    detail = store.get_knowhow_table(table_id)
    return {column["role"]: column["id"] for column in detail["columns"]}


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
    assert [c["role"] for c in detail["columns"]] == ["concept", "identify", "fix"]
    assert [c["position"] for c in detail["columns"]] == [0, 1, 2]
    assert detail["rows"] == []


def test_create_table_rejects_missing_concept_role(store, notebook_id):
    columns = [{"name": "A", "role": "plain"}, {"name": "B", "role": "identify"}]
    with pytest.raises(ValueError):
        store.create_knowhow_table(notebook_id, "T", "", columns)


def test_create_table_rejects_duplicate_concept_role(store, notebook_id):
    columns = [{"name": "A", "role": "concept"}, {"name": "B", "role": "concept"}]
    with pytest.raises(ValueError):
        store.create_knowhow_table(notebook_id, "T", "", columns)


def test_create_table_rejects_empty_column_name(store, notebook_id):
    columns = [{"name": "", "role": "concept"}, {"name": "B", "role": "plain"}]
    with pytest.raises(ValueError):
        store.create_knowhow_table(notebook_id, "T", "", columns)


def test_create_table_rejects_duplicate_column_names(store, notebook_id):
    columns = [{"name": "同名", "role": "concept"}, {"name": "同名", "role": "plain"}]
    with pytest.raises(ValueError):
        store.create_knowhow_table(notebook_id, "T", "", columns)


def test_create_table_error_messages_are_chinese_friendly(store, notebook_id):
    bad_column_sets = (
        [{"name": "A", "role": "plain"}],
        [{"name": "", "role": "concept"}],
        [{"name": "同", "role": "concept"}, {"name": "同", "role": "plain"}],
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
            notebook_id, "T", "", [{"name": "A", "role": "plain"}]
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
    cols = _columns_by_role(store, table_id)
    row_id = store.add_knowhow_row(
        table_id, {cols["concept"]: "过冲", cols["identify"]: "眼图异常"}
    )

    detail = store.get_knowhow_table(table_id)
    row = detail["rows"][0]
    assert row["id"] == row_id
    assert row["projection_status"] == "pending"
    assert row["cells"] == {cols["concept"]: "过冲", cols["identify"]: "眼图异常"}
    assert cols["fix"] not in row["cells"]


def test_add_row_does_not_bump_table_mutation_seq(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    store.add_knowhow_row(table_id, {})
    assert store.get_knowhow_table(table_id)["mutation_seq"] == 0


# ---------------------------------------------------------------------------
# update_knowhow_cell
# ---------------------------------------------------------------------------


def test_update_cell_upserts_and_bumps_row_and_table_state(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_role(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})
    seq0 = store.get_knowhow_table(table_id)["mutation_seq"]

    store.update_knowhow_cell(row_id, cols["fix"], "先重新布线")

    after = store.get_knowhow_table(table_id)
    row = after["rows"][0]
    assert row["cells"][cols["fix"]] == "先重新布线"
    assert row["projection_status"] == "pending"
    assert after["mutation_seq"] == seq0 + 1


def test_update_cell_is_idempotent_at_storage_level(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_role(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})

    store.update_knowhow_cell(row_id, cols["fix"], "v1")
    store.update_knowhow_cell(row_id, cols["fix"], "v2")
    store.update_knowhow_cell(row_id, cols["fix"], "v3")

    assert _cell_count(store, row_id, cols["fix"]) == 1
    detail = store.get_knowhow_table(table_id)
    assert detail["rows"][0]["cells"][cols["fix"]] == "v3"
    # three upserts on the same cell: three monotonic mutation_seq bumps.
    assert detail["mutation_seq"] == 3


def test_update_cell_resets_projection_status_to_pending(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_role(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})
    store.set_knowhow_row_projection(row_id, "synced")
    assert store.get_knowhow_table(table_id)["rows"][0]["projection_status"] == "synced"

    store.update_knowhow_cell(row_id, cols["concept"], "过冲")

    assert store.get_knowhow_table(table_id)["rows"][0]["projection_status"] == "pending"


def test_update_cell_resolves_table_id_from_row_without_a_table_id_argument(
    store, notebook_id
):
    t1 = store.create_knowhow_table(notebook_id, "A", "", BASE_COLUMNS)
    t2 = store.create_knowhow_table(notebook_id, "B", "", BASE_COLUMNS)
    cols_t1 = _columns_by_role(store, t1)
    row_t1 = store.add_knowhow_row(t1, {})

    store.update_knowhow_cell(row_t1, cols_t1["concept"], "x")

    assert store.get_knowhow_table(t1)["mutation_seq"] == 1
    assert store.get_knowhow_table(t2)["mutation_seq"] == 0


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
    cols = _columns_by_role(store, table_id)
    row_id = store.add_knowhow_row(table_id, {})

    seqs = [store.get_knowhow_table(table_id)["mutation_seq"]]
    store.update_knowhow_cell(row_id, cols["concept"], "a")
    seqs.append(store.get_knowhow_table(table_id)["mutation_seq"])
    store.bump_knowhow_mutation_seq(table_id)
    seqs.append(store.get_knowhow_table(table_id)["mutation_seq"])
    store.update_knowhow_cell(row_id, cols["concept"], "b")
    seqs.append(store.get_knowhow_table(table_id)["mutation_seq"])

    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


# ---------------------------------------------------------------------------
# delete_knowhow_table
# ---------------------------------------------------------------------------


def test_delete_table_cascades_columns_rows_and_cells(store, notebook_id):
    table_id = store.create_knowhow_table(notebook_id, "T", "", BASE_COLUMNS)
    cols = _columns_by_role(store, table_id)
    row_id = store.add_knowhow_row(table_id, {cols["concept"]: "x"})
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
    cols = {c["role"]: c["id"] for c in detail["columns"]}

    repo.update_knowhow_cell(row_id, cols["concept"], "hello")
    assert repo.get_knowhow_table(table_id)["rows"][0]["cells"][cols["concept"]] == "hello"

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
        repo.create_knowhow_table(notebook_id, "T", "", [{"name": "A", "role": "plain"}])
