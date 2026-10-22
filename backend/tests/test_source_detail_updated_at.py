"""单来源详情的 ``updated_at``(SQLite;PG 孪生在
``tests/postgres/test_core_store_conformance.py::test_source_detail_updated_at_matches_sqlite_semantics``)。

前端用它判断「这次重新解析跑完了没有」:重新解析端点只把 process_source 交给作业池、返回前
不改来源状态,作业池忙时轮询可能还读到上一次的 extracted / failed。前端在提交前读一次
``updated_at`` 作基准,之后只有「终态且它相对基准推进了」才算这一次跑完。

钉三件事:
1. 详情带上这一行最近一次被写的时刻;
2. 状态机的每一步(``set_status``)都推进它——一次重新解析的起点(parsing)与终点
   (extracted / failed)各推进一次;
3. 它只在详情路径上出,不进列表投影。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return Settings(_env_file=None)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return SQLiteRepository(_settings(tmp_path, monkeypatch))


def test_source_detail_updated_at_follows_every_status_step(repo):
    store = repo._runtime.source_store
    notebook_id = repo.create_notebook(NotebookCreate(name="nb")).id
    store.now = lambda: "2026-10-22T08:00:00+08:00"
    store.insert_source(
        source_id="src-1",
        notebook_id=notebook_id,
        title="a.md",
        source_type="markdown",
        status="extracted",
        parse_status="extracted",
        file_name="a.md",
        file_path="uploads/a.md",
        file_size=1,
        file_hash="",
        summary="",
        doc_type="",
    )
    baseline = store.get_source("src-1").updated_at
    assert baseline == "2026-10-22T08:00:00+08:00"

    # 作业还没开始:状态与 updated_at 都不动(前端据此不把旧终态当成这一次跑完)。
    assert store.get_source("src-1").parse_status == "extracted"
    assert store.get_source("src-1").updated_at == baseline

    store.now = lambda: "2026-10-22T08:05:00+08:00"
    store.set_status("src-1", "parsing")
    started = store.get_source("src-1").updated_at
    assert started == "2026-10-22T08:05:00+08:00"

    store.now = lambda: "2026-10-22T08:06:00+08:00"
    store.set_status("src-1", "extracted")
    finished = store.get_source("src-1")
    assert finished.parse_status == "extracted"
    assert finished.updated_at == "2026-10-22T08:06:00+08:00"
    assert finished.updated_at != baseline


def test_updated_at_is_on_the_detail_path_only(repo):
    store = repo._runtime.source_store
    notebook_id = repo.create_notebook(NotebookCreate(name="nb")).id
    store.insert_source(
        source_id="src-1",
        notebook_id=notebook_id,
        title="a.md",
        source_type="markdown",
        status="extracted",
        parse_status="extracted",
        file_name="a.md",
        file_path="uploads/a.md",
        file_size=1,
        file_hash="",
        summary="",
        doc_type="",
    )
    assert store.get_source("src-1").updated_at
    listed = store.list_sources(notebook_id)
    assert [item.id for item in listed] == ["src-1"]
    assert "updated_at" not in listed[0].model_dump()
