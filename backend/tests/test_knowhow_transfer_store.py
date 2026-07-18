import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository
from app.repositories.sqlite.knowhow_transfer_store import KnowhowTransferStore

COLUMNS = [
    {"name": "违例类型", "role": "anchor"},
    {"name": "现象识别", "role": "procedure"},
]

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

@pytest.fixture
def store(repo):
    rt = repo._runtime
    return KnowhowTransferStore(rt.database)

def _table(repo) -> str:
    nb = repo.create_notebook(NotebookCreate(name="KH", purpose="p", primary_domain="d")).id
    tid = repo.create_knowhow_table(nb, "时序修复", "desc", COLUMNS)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(tid)["columns"]}
    repo.add_knowhow_row(tid, {cols["违例类型"]: "过冲", cols["现象识别"]: "示波器观察"})
    return tid

def test_snapshot_returns_business_rows(repo, store):
    tid = _table(repo)
    snap = store.snapshot_table(tid)
    assert snap["table"]["id"] == tid
    assert {c["name"] for c in snap["columns"]} == {"违例类型", "现象识别"}
    assert len(snap["rows"]) == 1
    assert len(snap["cells"]) == 2
    # 未投影表没有派生产物
    assert snap["elements"] == [] and snap["chunks"] == []

def test_snapshot_missing_table_raises(store):
    with pytest.raises(KeyError):
        store.snapshot_table("khtbl-nope")

def test_insert_transfer_rejects_count_mismatch(repo, store):
    tid = _table(repo)
    snap = store.snapshot_table(tid)
    # 只插一个全新表行、零 cell，但 expected_counts 声称 cells=1 → 校验必须拒绝并回滚
    payload = {
        "table": {**snap["table"], "id": "khtbl-x"},
        "columns": [], "rows": [], "cells": [], "cell_code": [],
        "assets": [], "source": None, "elements": [], "chunks": [], "chunk_embeddings": [],
    }
    with pytest.raises(RuntimeError):
        store.insert_transfer(payload, {"columns": 0, "rows": 0, "cells": 1, "cell_code": 0})
    # 回滚生效：khtbl-x 未落库
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM knowhow_tables WHERE id='khtbl-x'").fetchone()[0] == 0


# --- PR review round 3 P1-3 (fingerprint gap, data loss precursor):
# upsert_knowhow_cell_code is an INSERT-ON-CONFLICT(row_id,column_id) upsert —
# an in-place code edit on an already-attached cell keeps the same row/column
# pair, so none of the four count-based fingerprint fields move (col_count/
# row_count/cell_count/cell_code_count are all unchanged: same number of
# rows). It also does NOT bump knowhow_tables.mutation_seq — only
# update_knowhow_cell/update_knowhow_cells do (see KnowhowStore's own
# docstring; upsert_knowhow_cell_code is conspicuously absent from that list).
# So a fingerprint built only from mutation_seq + the four counts is blind to
# this edit: move_table's snapshot-vs-delete concurrent-edit guard would sail
# straight past a concurrent code edit and delete the source table, silently
# discarding the newer code forever. table_fingerprint must therefore include
# a CONTENT signal over the code rows themselves, not just their count.
def test_fingerprint_changes_when_cell_code_is_edited_in_place(repo, store):
    tid = _table(repo)
    detail = repo.get_knowhow_table(tid)
    row_id = detail["rows"][0]["id"]
    column_id = detail["columns"][0]["id"]
    repo.upsert_knowhow_cell_code(row_id, column_id, "print(1)", "python", "user-x", "hash-v1")

    before = store.table_fingerprint(tid)
    # 仅仅原地编辑代码内容——不加行、不加列、不碰 knowhow_cells，
    # row/column 都是同一对，UNIQUE(row_id, column_id) 走 UPDATE 分支。
    repo.upsert_knowhow_cell_code(row_id, column_id, "print(2)", "python", "user-x", "hash-v2")
    after = store.table_fingerprint(tid)

    assert before != after, (
        "cell_code 内容原地编辑后 table_fingerprint 必须变化——否则 move_table 的"
        "并发编辑防护对代码编辑视而不见，编辑会随源表一起被删掉、永久丢失"
    )


def test_fingerprint_stable_when_nothing_changes(repo, store):
    """Companion happy-path guard: calling table_fingerprint twice with no
    intervening write must be stable (no spurious churn from e.g. dict key
    ordering or non-deterministic aggregate ordering)."""
    tid = _table(repo)
    detail = repo.get_knowhow_table(tid)
    row_id = detail["rows"][0]["id"]
    column_id = detail["columns"][0]["id"]
    repo.upsert_knowhow_cell_code(row_id, column_id, "print(1)", "python", "user-x", "hash-v1")

    first = store.table_fingerprint(tid)
    second = store.table_fingerprint(tid)

    assert first == second
