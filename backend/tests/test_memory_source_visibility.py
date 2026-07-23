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
- search_notebook (GET /notebooks/{id}/search, the front-end search box)
  surfaces neither a scope="Source" hit nor a scope="Element" hit for a
  memory-derived source (both the source_rows and the source_elements JOIN
  legs are filtered);
- shared_preview (GET /shared/{token}, shown to a would-be copier/joiner)
  omits memory-derived titles from its source_titles list;
- get_source still resolves a memory-derived source by id — the evidence
  round-trip path must not be filtered.

Rows are built directly via SourceStore.insert_source(source_type='memory')
(Task 1's column/index), independent of the Task 2/3 ingestion pipeline.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite.source_store import SourceElementWrite
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


# ---------------------------------------------------------------------------
# Reviewer follow-up: two MORE user-facing read paths leak the synthetic
# source, both discovered because the original brief's grep scope was too
# narrow. Search (GET /notebooks/{id}/search) surfaces it as a Source/Element
# hit; shared preview (GET /shared/{token}) lists its title to a prospective
# copier/joiner. Both must go dark, same as the list/count/analytics surfaces.
# ---------------------------------------------------------------------------

_NEEDLE = "ZZSECRETMEMZZ"  # unlikely to collide with any seeded/default text


def _insert_memory_with_searchable_content(repo, store, notebook_id):
    """A memory-derived source whose TITLE and one ELEMENT's text both contain
    _NEEDLE — a leak in either search_notebook leg (source_rows on title, or
    the source_elements JOIN sources on element text) would surface it."""
    _insert(
        store, notebook_id, "src-mem-search",
        title=f"{_NEEDLE} memory title", source_type="memory",
        doc_type="memory", memory_id="mem-search",
    )
    with repo._write() as db:
        store.replace_elements(
            db, "src-mem-search",
            [SourceElementWrite("el-mem-1", "paragraph", "p1", f"{_NEEDLE} in body", {})],
            created_at="2026-01-01T00:00:00",
        )


def test_search_notebook_excludes_memory_source_and_its_elements(repo, store, notebook_id):
    # A normal source that also matches _NEEDLE (title + element), so the test
    # proves search still returns real hits — the memory row alone goes dark.
    _insert(store, notebook_id, "src-normal-search", title=f"{_NEEDLE} normal title")
    with repo._write() as db:
        store.replace_elements(
            db, "src-normal-search",
            [SourceElementWrite("el-norm-1", "paragraph", "p1", f"{_NEEDLE} normal body", {})],
            created_at="2026-01-01T00:00:00",
        )
    _insert_memory_with_searchable_content(repo, store, notebook_id)

    resp = repo.search_notebook(notebook_id, _NEEDLE)

    # The memory-derived source must not appear under ANY scope (source_rows
    # would carry scope="Source", the element JOIN would carry scope="Element";
    # both set source_id to the memory source id).
    assert all(h.source_id != "src-mem-search" for h in resp.hits), [
        (h.scope, h.source_id) for h in resp.hits
    ]
    # Search is still functional: the normal source is found (both legs).
    hit_source_ids = {h.source_id for h in resp.hits}
    assert "src-normal-search" in hit_source_ids


def test_source_metadata_exposes_hidden_source_classification(store, notebook_id):
    _insert(store, notebook_id, "src-normal-meta", title="Normal")
    _insert(
        store,
        notebook_id,
        "src-memory-meta",
        title="Memory",
        source_type="memory",
        doc_type="memory",
        memory_id="mem-meta",
    )

    metadata = store.source_metadata(["src-normal-meta", "src-memory-meta"])

    assert metadata["src-normal-meta"]["source_type"] == "markdown"
    assert metadata["src-memory-meta"]["source_type"] == "memory"


def test_shared_preview_titles_exclude_memory_source(repo, notebook_id, store):
    """GET /shared/{token} preview (the copy/join modal) lists source_titles
    to another user — memory-derived titles must not leak into it."""
    _insert(store, notebook_id, "src-normal", title="Normal Doc")
    _insert(
        store, notebook_id, "src-memory", title="Memory Doc",
        source_type="memory", doc_type="memory", memory_id="mem-1",
    )
    preview = repo.shared_preview(notebook_id)
    assert "Memory Doc" not in preview["source_titles"]
    assert "Normal Doc" in preview["source_titles"]


# ---------------------------------------------------------------------------
# Whole-branch review follow-up: two MORE sources read paths (neither in the
# original T5 grep scope) present a memory-derived title as if it were a real
# source document. Both feed LLM/auto-generated user-visible text, so the
# synthetic source must go dark on them too.
# ---------------------------------------------------------------------------

def test_report_source_rows_excludes_memory_source(store, notebook_id):
    """report_source_rows → report_engine._build_corpus_map → the STORM
    deep-report planner prompt ("本 notebook 来源文件:\\n- <title>"). A
    memory-derived title must not be presented to the planner as a source
    document (its KG objects still participate via knowledge_objects)."""
    _insert(store, notebook_id, "src-normal", title="Normal Doc")
    _insert(
        store, notebook_id, "src-memory", title="Memory Doc",
        source_type="memory", doc_type="memory", memory_id="mem-1",
    )
    titles = [r["title"] for r in store.report_source_rows(notebook_id)]
    assert "Memory Doc" not in titles
    assert "Normal Doc" in titles


def test_meta_sources_excludes_memory_source(store, notebook_id):
    """meta_sources → augment_notebook_metadata (notebook auto-naming). A
    hidden memory source (status='extracted') must not contribute its title or
    inflate the source count baked into the auto-generated name/description."""
    _insert(store, notebook_id, "src-normal", title="Normal Doc")
    _insert(
        store, notebook_id, "src-memory", title="Memory Doc",
        source_type="memory", doc_type="memory", memory_id="mem-1",
    )
    titles = [r["title"] for r in store.meta_sources(notebook_id)]
    assert "Memory Doc" not in titles
    assert "Normal Doc" in titles
