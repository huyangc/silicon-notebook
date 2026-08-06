"""服务层的 KNN 路由判据（POSTGRES_LEXICAL_KNN_ENABLED）。

判据集中在 `_lexical_object_hits` 一处：开关开着、run 未按来源收窄、且该库是
「大库」（copyable=False，与自动建索引共用同一判定）。三者缺一即传
`allow_knn=False`，SQL 逐字回到 legacy。这里用 SQLite 仓库 + 假 fts_search
钉服务层判据本身——KNN SQL 的正确性由 PostgreSQL conformance 测试负责，
这份测试只回答「hint 什么时候被给出」。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _captured_hint(repo, notebook_id, *, flag, copyable, allowed_source_ids=None):
    """跑一次 `_lexical_object_hits`,返回 fts_search 实际收到的 allow_knn。"""
    candidates = repo.retrieval.candidates
    captured: dict = {}

    def fake_fts_search(db, nb, query, k, **kwargs):
        captured.update(kwargs)
        return []

    real_stats = candidates.notebook_copy_stats
    candidates.knowledge.fts_search = fake_fts_search
    candidates.notebook_copy_stats = lambda nb: {"copyable": copyable}
    candidates.settings.postgres_lexical_knn_enabled = flag
    try:
        with repo._connect() as db:
            candidates._lexical_object_hits(
                db, notebook_id, "thermal design", 12,
                site="test", corpus_langs=None,
                allowed_source_ids=allowed_source_ids,
            )
    finally:
        candidates.notebook_copy_stats = real_stats
    assert "allow_knn" in captured or allowed_source_ids is not None, (
        "unscoped 调用必须显式携带 allow_knn，缺省依赖等于让路由静默失效"
    )
    return captured.get("allow_knn", False)


def test_hint_requires_flag_and_large_notebook(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    assert _captured_hint(repo, nb.id, flag=True, copyable=False) is True
    assert _captured_hint(repo, nb.id, flag=False, copyable=False) is False
    # 小库（copyable）刚拿到复合 GIN 索引，现状已是最优；KNN 反而要在全局
    # 距离序里翻找自己的行——路由必须把它们留在 legacy。
    assert _captured_hint(repo, nb.id, flag=True, copyable=True) is False


def test_scoped_runs_never_carry_the_hint(repo):
    """按来源收窄的 run 是词法唯一候选来源,且 KNN 形状没有 scoped bench。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    assert _captured_hint(
        repo, nb.id, flag=True, copyable=False,
        allowed_source_ids=["src-1"],
    ) is False


def test_sqlite_adapter_accepts_and_ignores_the_hint(repo):
    """签名对等:service 不判 dialect,同一调用必须在 SQLite 上也合法。

    经真实的服务级 knowledge 句柄调用(与 `_lexical_object_hits` 同一入口),
    不 mock——这条测试的全部意义就是真实签名接得住这个 kwarg。
    """
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    knowledge = repo.retrieval.candidates.knowledge
    with repo._connect() as db:
        rows = knowledge.fts_search(db, nb.id, "thermal", 5, allow_knn=True)
    assert rows == []
