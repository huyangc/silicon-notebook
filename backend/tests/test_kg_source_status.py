"""Tests for per-source kg_extracted flag and notebook kg_pending_sources count.

These tests verify:
- SourceSummary.kg_extracted is True when ≥1 knowledge_objects row links to the source.
- SourceSummary.kg_extracted is False when no such rows exist.
- NotebookSummary.kg_pending_sources counts sources that are PARSED (have source_elements)
  but have NO KG (no knowledge_objects with that source_id).
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


def _insert_source(repo: SQLiteRepository, notebook_id: str, *,
                   with_elements: bool = True) -> str:
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
               VALUES (?, ?, 'Test Source', 'markdown', 'extracted', 'parsed',
                       'test.md', '', 0, '', '', '', ?, ?)""",
            (source_id, notebook_id, now, now),
        )
        if with_elements:
            elem_id = f"el-{uuid4().hex[:10]}"
            db.execute(
                """INSERT INTO source_elements
                   (id, source_id, element_type, location_label, text, metadata, created_at)
                   VALUES (?, ?, 'paragraph', 'p1', 'Some content.', '{}', ?)""",
                (elem_id, source_id, now),
            )
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
