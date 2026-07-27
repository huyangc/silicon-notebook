"""Tests for per-source kg_extracted flag and notebook kg_pending_sources count.

These tests verify:
- SourceSummary.kg_extracted requires a linked object and a complete latest KG run
  (or no extraction history for direct/governance compatibility rows).
- SourceSummary.kg_extracted is False when no such rows exist.
- NotebookSummary.kg_pending_sources counts parsed sources without a complete KG.
- A source with no source_elements (not parsed) does NOT count as pending.
- After a source gets KG, the pending count drops.
"""
import pytest
from uuid import uuid4

from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now
from app.core.config import Settings


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings()
    return SQLiteRepository(settings)


def _insert_source(
    repo: SQLiteRepository,
    notebook_id: str,
    *,
    with_elements: bool = True,
    source_type: str = "markdown",
) -> str:
    """Insert a minimal source row; optionally insert a source_elements row.
    Returns the source_id."""
    source_id = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._connect() as db:
        db.execute(
            """INSERT INTO sources
               (id, notebook_id, title, source_type, status, parse_status,
                file_name, file_path, file_size, file_hash, summary, doc_type,
                created_at, updated_at)
               VALUES (?, ?, 'Test Source', ?, 'extracted', 'parsed',
                       'test.md', '', 0, '', '', '', ?, ?)""",
            (source_id, notebook_id, source_type, now, now),
        )
        if with_elements:
            elem_id = f"el-{uuid4().hex[:10]}"
            db.execute(
                """INSERT INTO source_elements
                   (id, source_id, element_type, location_label, text, metadata, created_at)
                   VALUES (?, ?, 'paragraph', 'p1', 'Some content.', '{}', ?)""",
                (elem_id, source_id, now),
            )
    # Raw insert bypasses the pipeline's kg_mutation_seq bump; drop the seq-gated
    # pending/chunk memo so pending-count assertions recompute (mirrors the
    # insert_test_object precedent in knowledge_query.py).
    from app.repositories.sqlite import knowledge_counts_cache
    knowledge_counts_cache.invalidate(notebook_id)
    return source_id


# ---------------------------------------------------------------------------
# _source_has_kg helper
# ---------------------------------------------------------------------------

def test_source_has_kg_false_when_no_objects(repo):
    """A source with no knowledge_objects rows → kg_extracted=False."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src_id = _insert_source(repo, nb.id)
    with repo._connect() as db:
        assert repo._source_has_kg(db, src_id) is False


def test_source_has_kg_true_when_objects_present(repo):
    """A source with a knowledge_objects row (source_id set) → kg_extracted=True."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src_id = _insert_source(repo, nb.id)
    # Insert a knowledge_objects row linked to this source
    repo._test_insert_object(nb.id, "concept", {"name": "MOSFET"}, source_id=src_id)
    with repo._connect() as db:
        assert repo._source_has_kg(db, src_id) is True


def test_source_has_kg_ignores_empty_source_id(repo):
    """A knowledge_objects row with empty source_id='' should NOT count."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src_id = _insert_source(repo, nb.id)
    # Insert a knowledge_objects row with empty source_id (legacy/no-source case)
    repo._test_insert_object(nb.id, "concept", {"name": "Threshold"}, source_id="")
    with repo._connect() as db:
        assert repo._source_has_kg(db, src_id) is False


# ---------------------------------------------------------------------------
# _count_pending_kg_sources helper
# ---------------------------------------------------------------------------

def test_pending_count_zero_when_all_have_kg(repo):
    """If all parsed sources have KG, pending count is 0."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src_id = _insert_source(repo, nb.id)
    repo._test_insert_object(nb.id, "concept", {"name": "Gate"}, source_id=src_id)
    with repo._connect() as db:
        assert repo._count_pending_kg_sources(db, nb.id) == 0


def test_pending_count_one_when_one_missing_kg(repo):
    """2 parsed sources, 1 with KG, 1 without → pending count == 1."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src_with_kg = _insert_source(repo, nb.id)
    src_no_kg = _insert_source(repo, nb.id)
    # Give KG only to src_with_kg
    repo._test_insert_object(nb.id, "concept", {"name": "Oxide"}, source_id=src_with_kg)
    with repo._connect() as db:
        assert repo._count_pending_kg_sources(db, nb.id) == 1


def test_pending_count_ignores_unparsed_sources(repo):
    """A source with NO source_elements (not parsed) should NOT count as pending."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # This source has no source_elements (with_elements=False)
    _insert_source(repo, nb.id, with_elements=False)
    with repo._connect() as db:
        assert repo._count_pending_kg_sources(db, nb.id) == 0


def test_pending_count_drops_after_kg_added(repo):
    """After a source gets KG entries, pending count drops."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src_id = _insert_source(repo, nb.id)
    with repo._connect() as db:
        assert repo._count_pending_kg_sources(db, nb.id) == 1
    # Now add KG
    repo._test_insert_object(nb.id, "concept", {"name": "Vt"}, source_id=src_id)
    with repo._connect() as db:
        assert repo._count_pending_kg_sources(db, nb.id) == 0


def test_compat_pending_helper_is_physical_while_summary_is_visible(repo):
    nb = repo.create_notebook(NotebookCreate(name="mixed pending"))
    _insert_source(repo, nb.id, source_type="markdown")
    _insert_source(repo, nb.id, source_type="memory")
    _insert_source(repo, nb.id, source_type="knowhow")

    with repo._connect() as db:
        assert repo._count_pending_kg_sources(db, nb.id) == 3
    assert repo.get_notebook(nb.id).kg_pending_sources == 1


# ---------------------------------------------------------------------------
# Schema-level: fields serialize through SourceSummary / NotebookSummary
# ---------------------------------------------------------------------------

def test_source_summary_kg_extracted_field(repo):
    """_source_from_row sets kg_extracted correctly on the returned SourceSummary."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src_id = _insert_source(repo, nb.id)

    # Before KG: kg_extracted should be False
    src_summary = repo.get_source(src_id)
    assert src_summary.kg_extracted is False

    # After adding a KG object linked to this source: kg_extracted should be True
    repo._test_insert_object(nb.id, "concept", {"name": "Drain"}, source_id=src_id)
    src_summary = repo.get_source(src_id)
    assert src_summary.kg_extracted is True


def test_notebook_summary_kg_pending_sources_field(repo):
    """_notebook_from_row sets kg_pending_sources correctly on the returned NotebookSummary."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src1 = _insert_source(repo, nb.id)
    src2 = _insert_source(repo, nb.id)

    # Both parsed, neither has KG
    nb_summary = repo.get_notebook(nb.id)
    assert nb_summary.kg_pending_sources == 2

    # Give KG to src1
    repo._test_insert_object(nb.id, "concept", {"name": "Source"}, source_id=src1)
    nb_summary = repo.get_notebook(nb.id)
    assert nb_summary.kg_pending_sources == 1

    # Give KG to src2 as well
    repo._test_insert_object(nb.id, "concept", {"name": "Drain"}, source_id=src2)
    nb_summary = repo.get_notebook(nb.id)
    assert nb_summary.kg_pending_sources == 0


def test_list_sources_includes_kg_extracted_field(repo):
    """list_sources returns SourceSummary items with the kg_extracted field."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src_id = _insert_source(repo, nb.id)

    items = repo.list_sources(nb.id)
    assert len(items) == 1
    assert items[0].kg_extracted is False

    repo._test_insert_object(nb.id, "concept", {"name": "Gate"}, source_id=src_id)
    items = repo.list_sources(nb.id)
    assert items[0].kg_extracted is True


# ---------------------------------------------------------------------------
# Task 12: status transitions ride SourceIngestionService.set_source_status
# ---------------------------------------------------------------------------

def test_set_source_status_persists_and_emits_status_event(repo, monkeypatch):
    """The facade _set_source_status delegate persists status/parse_status/
    summary/error_message in one write AND emits the status-machine event —
    behavior frozen across the Task 12 move into SourceIngestionService."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src_id = _insert_source(repo, nb.id)
    events = []
    original_emit = repo.event_log.emit
    monkeypatch.setattr(
        repo.event_log,
        "emit",
        lambda payload, **kw: (events.append(dict(payload)), original_emit(payload, **kw))[0],
    )
    repo._set_source_status(src_id, "failed", summary="S", error_message="boom")
    src = repo.get_source(src_id)
    assert (src.parse_status, src.summary, src.error_message) == ("failed", "S", "boom")
    assert any(
        e.get("kind") == "status" and e.get("status") == "failed"
        and e.get("source_id") == src_id and e.get("error") == "boom"
        for e in events
    )


def test_failed_latest_extraction_does_not_hide_partial_source_graph(repo):
    """A failed run plus leftover rows is unfinished and must be resumable."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src_id = _insert_source(repo, nb.id)
    repo._test_insert_object(
        nb.id, "concept", {"name": "Partial"}, source_id=src_id
    )
    now = _now()
    with repo._write() as db:
        db.execute(
            """
            INSERT INTO extraction_runs
            (id, notebook_id, source_id, run_type, status, error_message,
             created_at, updated_at)
            VALUES (?, ?, ?, 'kg', 'failed', 'chunk failure', ?, ?)
            """,
            (f"run-{uuid4().hex[:10]}", nb.id, src_id, now, now),
        )
    from app.repositories.sqlite import knowledge_counts_cache
    knowledge_counts_cache.invalidate(nb.id)

    with repo._connect() as db:
        assert repo._source_has_kg(db, src_id) is False
        assert repo._count_pending_kg_sources(db, nb.id) == 1
    assert repo.get_source(src_id).kg_extracted is False
    pages = list(repo._runtime.knowledge_lifecycle._kg_target_pages(
        nb.id, "incremental"
    ))
    targets = [source for page, _skipped, _missing in pages for source in page]
    skipped = [source for _page, rows, _missing in pages for source in rows]
    assert targets == [src_id]
    assert skipped == []


def test_completed_extraction_invalidates_prewarmed_pending_cache(repo):
    """Completing the run must refresh pending count without another KG write."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src_id = _insert_source(repo, nb.id)
    run_id = f"run-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            """
            INSERT INTO extraction_runs
            (id, notebook_id, source_id, run_type, status, error_message,
             created_at, updated_at)
            VALUES (?, ?, ?, 'kg', 'running', '', ?, ?)
            """,
            (run_id, nb.id, src_id, now, now),
        )
    repo._test_insert_object(
        nb.id, "concept", {"name": "Complete"}, source_id=src_id
    )

    assert repo.get_notebook(nb.id).kg_pending_sources == 1
    repo._runtime.knowledge.finish_extraction(
        run_id, "completed", "kg objects=1"
    )

    assert repo.get_notebook(nb.id).kg_pending_sources == 0
