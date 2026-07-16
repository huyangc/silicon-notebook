"""NotebookAnalytics.paper_meta_counts 三态计数（paper-meta-status Task 4）。

看板 /analytics 端点新增的 is_paper 三态 GROUP BY：has_meta（is_paper=1）/
marker（is_paper=0，已判定非论文的标记行）/ missing（合规候选、尚无 meta 行，
口径镜像 SourceStore.sources_missing_paper_meta）。paper-meta 写入不走
kg_mutation_seq（create_source/upsert_paper_meta 都不 bump 该计数器），所以这两条
GROUP BY 必须是未缓存的直接查询——不能复用 knowledge_counts_cache 的 seq 门（会读到
陈旧值），与紧邻的 source_status_counts 走同一套「uncached GROUP BY」模式。
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


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(NotebookCreate(name="nb")).id


def test_notebook_analytics_paper_meta_counts_three_states(repo, notebook_id):
    """构造 has_meta/marker/missing/非合规 混合，断言三键精确。"""
    store = repo._runtime.source_store
    # 2 has_meta
    for sid, is_paper in [("h1", True), ("h2", True), ("m1", False)]:
        store.insert_source(
            source_id=sid, notebook_id=notebook_id, title=sid,
            source_type="pdf", status="parsed", parse_status="parsed",
            file_name=f"{sid}.pdf", file_path=f"/tmp/{sid}.pdf",
            file_size=0, file_hash=f"h-{sid}", summary="", doc_type="",
        )
        store.upsert_paper_meta(sid, notebook_id, {
            "is_paper": is_paper, "paper_title": None, "venue": None,
            "pub_year": None, "doi": None, "keywords": [], "authors": [],
            "raw_json": "{}", "model": "t",
        })
    # 2 missing
    for sid in ("mi1", "mi2"):
        store.insert_source(
            source_id=sid, notebook_id=notebook_id, title=sid,
            source_type="pdf", status="parsed", parse_status="parsed",
            file_name=f"{sid}.pdf", file_path=f"/tmp/{sid}.pdf",
            file_size=0, file_hash=f"h-{sid}", summary="", doc_type="",
        )
    # 1 非合规（memory）不计
    store.insert_source(
        source_id="mm", notebook_id=notebook_id, title="mm",
        source_type="memory", status="parsed", parse_status="parsed",
        file_name="", file_path="", file_size=0, file_hash="h-mm",
        summary="", doc_type="",
    )
    a = repo.notebook_analytics(notebook_id)
    assert a.paper_meta_counts == {"has_meta": 2, "marker": 1, "missing": 2}


def test_notebook_analytics_paper_meta_counts_no_stale_after_write(repo, notebook_id):
    """meta 写入后立即查得新计数（不用 kg_mutation_seq 缓存）。"""
    store = repo._runtime.source_store
    store.insert_source(
        source_id="s1", notebook_id=notebook_id, title="s1",
        source_type="pdf", status="parsed", parse_status="parsed",
        file_name="s1.pdf", file_path="/tmp/s1.pdf", file_size=0,
        file_hash="h", summary="", doc_type="",
    )
    assert repo.notebook_analytics(notebook_id).paper_meta_counts == {
        "has_meta": 0, "marker": 0, "missing": 1
    }
    store.upsert_paper_meta("s1", notebook_id, {
        "is_paper": True, "paper_title": None, "venue": None,
        "pub_year": None, "doi": None, "keywords": [], "authors": [],
        "raw_json": "{}", "model": "t",
    })
    # 无 seq bump 也应立即刷新
    assert repo.notebook_analytics(notebook_id).paper_meta_counts == {
        "has_meta": 1, "marker": 0, "missing": 0
    }
