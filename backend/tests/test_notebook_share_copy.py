# backend/tests/test_notebook_share_copy.py
import json
import uuid
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    return SQLiteRepository(Settings())


@pytest.fixture
def client(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    from app.main import app
    return TestClient(app)


def _mk_nb(repo, name="NB", owner="user-local"):
    """直接建一个空 notebook(不依赖当前用户 ContextVar),返回 nb_id。"""
    nb_id = f"nb-{uuid.uuid4().hex[:10]}"; now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (nb_id, name, "", "Semiconductor", "draft", owner, now, now))
    return nb_id


def _rows(repo, table, nb):
    with repo._connect() as db:
        return db.execute(f"SELECT * FROM {table} WHERE notebook_id=?", (nb,)).fetchall()


def test_notebooks_has_share_columns(repo):
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(notebooks)")}
    assert "is_shared" in cols
    assert "share_token" in cols


def test_copy_thresholds_defaults():
    from app.core.config import Settings
    s = Settings()
    assert s.notebook_copy_max_bytes == 50 * 1024 * 1024
    assert s.notebook_copy_max_rows == 5000


def test_share_sets_token_idempotent_then_unshare_clears(repo):
    nb = _mk_nb(repo, "L")
    out = repo.share_notebook(nb)
    assert out["share_token"].startswith("shr-")
    assert repo.find_notebook_by_share_token(out["share_token"]) == nb
    # 幂等:再分享返回同一个 token
    assert repo.share_notebook(nb)["share_token"] == out["share_token"]
    # 取消 → token 失效
    repo.unshare_notebook(nb)
    assert repo.find_notebook_by_share_token(out["share_token"]) is None


def test_copy_stats_reports_size_and_copyable(repo):
    nb = _mk_nb(repo, "L")
    stats = repo.notebook_copy_stats(nb)
    assert stats["copyable"] is True          # 空库当然可拷贝
    assert set(stats["size"]) == {"bytes", "sources", "chunks", "nodes", "edges"}


def test_remap_json_ids_scalars_and_arrays():
    from app.services.sqlite_repository import _remap_json_ids
    # 生产里 copy_notebook 对 element_id / element_ids 传的是同一个 emap,故这里
    # 两个键共用同一份 element 映射(el-1→el-A, el-2→el-B),与真实调用一致。
    el_map = {"el-1": "el-A", "el-2": "el-B"}
    maps = {"element_id": el_map, "element_ids": el_map,
            "source_id": {"src-1": "src-A"}, "object_id": {"ko-1": "ko-A"}}
    payload = {
        "source_id": "src-1",
        "steps": [{"element_id": "el-1", "quote": "keep me"}],
        "evidence": [{"element_id": "el-2", "source_id": "src-1", "quoted_span": "keep"}],
        "element_ids": ["el-1", "el-2", "el-unknown"],
        "note": "untouched",
    }
    out = _remap_json_ids(payload, maps)
    assert out["source_id"] == "src-A"
    assert out["steps"][0]["element_id"] == "el-A"
    assert out["steps"][0]["quote"] == "keep me"
    assert out["evidence"][0]["element_id"] == "el-B"
    assert out["evidence"][0]["source_id"] == "src-A"
    assert out["element_ids"] == ["el-A", "el-B", "el-unknown"]  # 未命中的原样
    assert out["note"] == "untouched"
