"""大库检索按磁盘索引身份缓存:stale 实例按磁盘 manifest 版本复用,脱离 kg_mutation_seq
churn,使摄取期严格推理恒定 O(1)。"""
import json
import os
import threading

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder


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


def test_read_manifest_version(repo, tmp_path):
    out_dir = tmp_path / "idxdir"
    out_dir.mkdir()
    # 无 manifest → None
    assert repo._read_manifest_version(str(out_dir)) is None
    # 有 manifest → 返回 version list
    (out_dir / "manifest.json").write_text(json.dumps({"version": ["a", 3, "t"]}))
    assert repo._read_manifest_version(str(out_dir)) == ["a", 3, "t"]
    # 损坏 JSON → None(不抛)
    (out_dir / "manifest.json").write_text("{not json")
    assert repo._read_manifest_version(str(out_dir)) is None
    # 无 version 字段 → None
    (out_dir / "manifest.json").write_text(json.dumps({"n_nodes": 5}))
    assert repo._read_manifest_version(str(out_dir)) is None


def test_load_lock_table_present(repo):
    assert isinstance(repo._scale_idx_load_lock, threading.Lock().__class__)
    assert repo._scale_idx_load_locks == {}
