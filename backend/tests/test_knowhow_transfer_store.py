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
