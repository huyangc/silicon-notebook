"""P1-A:per-ask embed 缓存。ask 作用域内同文本只打一次 embed 端点;
作用域外(ContextVar 默认 None)行为不变;失败不缓存。"""
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services import sqlite_repository as sr

import pytest


class _CountingEmbedder:
    def __init__(self):
        self.calls = []
        self.fail_next = False

    def embed_query(self, text):
        self.calls.append(text)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("boom")
        return [0.1, 0.2, 0.3]


@pytest.fixture
def repo_factory(tmp_path, monkeypatch):
    """镜像 test_scale_version_probe.py 的 repo_factory 形状,额外设四个
    EMBED_* env 让 embedder_configured(config.py 只读 property)天然为真——
    不能直接赋值 repo.settings.embedder_configured(property 无 setter)。"""
    def _make():
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
        monkeypatch.setenv("EMBED_PROVIDER", "dashscope")  # embedder_configured=True
        monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
        monkeypatch.setenv("EMBED_API_KEY", "test-key")
        monkeypatch.setenv("EMBED_MODEL", "test-model")
        r = SQLiteRepository(Settings())
        return r
    return _make


def _mk_repo(repo_factory):
    repo = repo_factory()
    emb = _CountingEmbedder()
    repo.embedder = emb
    assert repo.settings.embedder_configured  # 确认走真实 embed 路径,非早退
    return repo, emb


def test_no_cache_outside_ask_scope(repo_factory):
    repo, emb = _mk_repo(repo_factory)
    repo._embed_query("q1")
    repo._embed_query("q1")
    assert len(emb.calls) == 2      # 默认 None:每次都打端点(现状不变)


def test_cache_within_ask_scope(repo_factory):
    repo, emb = _mk_repo(repo_factory)
    tok = sr._ASK_EMBED_CACHE.set({})
    try:
        v1 = repo._embed_query("q1")
        v2 = repo._embed_query("q1")
        repo._embed_query("q2")
    finally:
        sr._ASK_EMBED_CACHE.reset(tok)
    assert len(emb.calls) == 2      # q1 一次 + q2 一次
    assert v1 == v2


def test_failure_not_cached(repo_factory):
    repo, emb = _mk_repo(repo_factory)
    tok = sr._ASK_EMBED_CACHE.set({})
    try:
        emb.fail_next = True
        assert repo._embed_query("q1") is None
        assert repo._embed_query("q1") is not None   # 失败未缓存,重试成功
    finally:
        sr._ASK_EMBED_CACHE.reset(tok)
    assert len(emb.calls) == 2
