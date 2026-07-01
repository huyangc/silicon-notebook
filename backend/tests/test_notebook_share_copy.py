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
