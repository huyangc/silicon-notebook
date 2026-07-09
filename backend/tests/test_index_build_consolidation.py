"""索引与构建统一整合:聚合状态 / 取消 / built_at。"""
import json
import os

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_index_status_aggregates_three_systems(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    # 纯读、不得触发 viz build
    called = {"viz": 0}
    monkeypatch.setattr(repo, "_spawn_viz_build", lambda *a, **k: called.__setitem__("viz", called["viz"] + 1))
    out = repo.index_status(nb.id)
    assert set(out) == {"kg", "unified_kg", "scale_index"}
    assert set(out["kg"]) >= {"ready", "building", "pending_sources"}
    assert set(out["unified_kg"]) >= {"dirty", "building", "last_rebuild_at"}
    assert "state" in out["scale_index"]
    # 与各自旧 status 一致
    assert out["scale_index"]["state"] == repo.scale_index_status(nb.id)["state"]
    assert out["unified_kg"]["dirty"] == repo.unified_kg_status(nb.id)["dirty"]
    assert called["viz"] == 0   # 聚合是纯读
