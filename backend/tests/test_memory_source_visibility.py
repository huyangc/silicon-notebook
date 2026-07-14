"""Task 5 (memory-kg-extract): Memory-derived synthetic sources
(source_type='memory') must be invisible on user-facing surfaces — a
confirmed Memory's derived source would otherwise double-count and show
duplicate content right next to the Memory panel — while staying visible on
internal paths (evidence lookup by id, pending-kg counts, copy
materialization, scale-index scans) that need the true full source set.

RED-first contracts:
- list_sources / list_sources_page exclude source_type='memory' rows, and
  their totals (list length / PaginatedSources.total_count) exclude them too;
- NotebookSummary's counts["sources"] aggregate excludes them;
- notebook_analytics' source_status_counts (parse_status distribution, the
  /analytics 看板) excludes them;
- get_source still resolves a memory-derived source by id — the evidence
  round-trip path must not be filtered.

Rows are built directly via SourceStore.insert_source(source_type='memory')
(Task 1's column/index), independent of the Task 2/3 ingestion pipeline.
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
def store(repo):
    return repo._runtime.source_store


@pytest.fixture
def notebook_id(repo):
    return repo.create_notebook(NotebookCreate(name="nb")).id


def _insert(store, notebook_id, source_id, **overrides):
    kwargs = dict(
        source_id=source_id,
        notebook_id=notebook_id,
        title=f"Doc {source_id}",
        source_type="markdown",
        status="extracted",
        parse_status="extracted",
        file_name=f"{source_id}.md",
        file_path=f"/tmp/{source_id}.md",
        file_size=0,
        file_hash="",
        summary="",
        doc_type="",
    )
    kwargs.update(overrides)
    store.insert_source(**kwargs)


def _seed_one_normal_one_memory(store, notebook_id):
    """1 普通源 + 1 source_type='memory' 合成源(挂 memory_id,状态都是
    'extracted' 便于断言 parse_status 分布)。"""
    _insert(store, notebook_id, "src-normal")
    _insert(
        store, notebook_id, "src-memory",
        source_type="memory", doc_type="memory", memory_id="mem-1",
    )


def test_list_sources_excludes_memory_source(store, notebook_id):
    _seed_one_normal_one_memory(store, notebook_id)
    summaries = store.list_sources(notebook_id)
    assert [s.id for s in summaries] == ["src-normal"]


def test_list_sources_page_excludes_memory_source_and_total(store, notebook_id):
    _seed_one_normal_one_memory(store, notebook_id)
    page = store.list_sources_page(notebook_id)
    assert [s.id for s in page.items] == ["src-normal"]
    assert page.total_count == 1, "total_count must not double-count the synthetic source"


def test_notebook_summary_source_count_excludes_memory_source(repo, store, notebook_id):
    _seed_one_normal_one_memory(store, notebook_id)
    summary = repo.get_notebook(notebook_id)
    assert summary.counts["sources"] == 1


def test_analytics_parse_status_excludes_memory_source(repo, store, notebook_id):
    _seed_one_normal_one_memory(store, notebook_id)
    analytics = repo.notebook_analytics(notebook_id)
    assert analytics.source_status_counts == {"extracted": 1}


def test_get_source_still_resolves_memory_source_by_id(store, notebook_id):
    """内部证据回查路径必须不受过滤影响 —— evidence card 按 id 直查合成源。"""
    _seed_one_normal_one_memory(store, notebook_id)
    detail = store.get_source("src-memory")
    assert detail.id == "src-memory"
    assert detail.type == "memory"
