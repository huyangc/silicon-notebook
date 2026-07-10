# backend/tests/test_source_file_store.py
"""Task 11: SourceFileStore component — source file persistence (upload
write / delete / raw-text read) extracted off the facade.

Invariants under test:
- write_upload defuses path traversal (safe naming owns directory separators)
  and always lands inside ``storage_dir/notebooks/<notebook_id>/``;
- delete is a no-op for empty/missing paths and prunes the per-notebook
  directory only when the deleted file was the last one in it;
- read_source_text prefers the stored .md/.markdown/.txt file (resolved
  through the database boundary's ``resolve_path`` for relative paths) and
  falls back to joined element texts for other types or unreadable files;
- the facade keeps frozen-signature ``_delete_file`` / ``_source_raw_text``
  delegates riding the runtime-composed store.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.repositories.source_files import (
    SourceFileStore,
    delete_source_file,
    safe_filename,
)


def _identity_resolver(root: Path):
    def resolve(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else root / path

    return resolve


@pytest.fixture
def store(tmp_path):
    return SourceFileStore(
        tmp_path / "storage", resolve_path=_identity_resolver(tmp_path)
    )


# ------------------------------------------------------------- safe naming
def test_safe_filename_strips_directories_and_separators():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("a\\b.txt") == "a_b.txt"
    assert safe_filename("  spaced.pdf  ") == "spaced.pdf"


def test_safe_filename_empty_falls_back_to_source_bin():
    assert safe_filename("") == "source.bin"
    assert safe_filename("   ") == "source.bin"


def test_write_upload_defuses_traversal_and_owns_notebook_dir(store):
    stored = store.write_upload("nb-1", "src-1", "../../../evil.bin", b"payload")

    assert stored == store.storage_dir / "notebooks" / "nb-1" / "src-1_evil.bin"
    assert stored.read_bytes() == b"payload"
    # the write never escaped the storage root
    assert stored.resolve().is_relative_to(store.storage_dir.resolve())


def test_write_upload_creates_parent_directories(store):
    assert not (store.storage_dir / "notebooks").exists()
    stored = store.write_upload("nb-2", "src-9", "doc.md", b"# hi")
    assert stored.is_file()


# ------------------------------------------------------------------ delete
def test_delete_missing_file_is_noop(store, tmp_path):
    store.delete("")                                    # empty → no-op
    store.delete(str(tmp_path / "nowhere" / "gone.md"))  # missing → no raise


def test_delete_removes_file_and_prunes_empty_notebook_dir(store):
    stored = store.write_upload("nb-3", "src-1", "only.md", b"x")
    notebook_dir = stored.parent

    store.delete(str(stored))

    assert not stored.exists()
    assert not notebook_dir.exists()   # last file → per-notebook dir pruned


def test_delete_keeps_directory_holding_other_files(store):
    first = store.write_upload("nb-4", "src-1", "a.md", b"a")
    second = store.write_upload("nb-4", "src-2", "b.md", b"b")

    store.delete(str(first))

    assert not first.exists()
    assert second.exists()
    assert second.parent.exists()


def test_delete_source_file_module_helper_matches_store_delete(tmp_path):
    path = tmp_path / "nb" / "one.md"
    path.parent.mkdir(parents=True)
    path.write_text("x", encoding="utf-8")

    delete_source_file(str(path))

    assert not path.exists()
    assert not path.parent.exists()


# ---------------------------------------------------------------- raw text
def test_read_source_text_prefers_stored_markdown_file(store, tmp_path):
    md = tmp_path / "stored.md"
    md.write_text("# stored truth", encoding="utf-8")

    text = store.read_source_text(
        str(md), [SimpleNamespace(text="fallback element")]
    )

    assert text == "# stored truth"


def test_read_source_text_resolves_relative_paths_via_seam(store, tmp_path):
    (tmp_path / "rel").mkdir()
    (tmp_path / "rel" / "doc.txt").write_text("relative body", encoding="utf-8")

    assert store.read_source_text("rel/doc.txt", []) == "relative body"


def test_read_source_text_falls_back_for_non_text_types(store):
    elements = [SimpleNamespace(text="first"), SimpleNamespace(text="second")]
    assert store.read_source_text("scan.pdf", elements) == "first\n\nsecond"


def test_read_source_text_falls_back_when_file_unreadable(store, tmp_path):
    elements = [SimpleNamespace(text="element body")]
    missing = tmp_path / "gone.md"
    assert store.read_source_text(str(missing), elements) == "element body"


# ---------------------------------------------------------- facade wiring
@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import Settings
    from app.services import sqlite_repository

    return sqlite_repository.SQLiteRepository(Settings())


def test_runtime_composes_source_file_store(repo):
    files = repo._runtime.source_files
    assert isinstance(files, SourceFileStore)
    assert files.storage_dir == repo.storage_dir
    assert {"write_upload", "delete", "read_source_text"} <= set(
        SourceFileStore.__dict__
    )
    assert "__getattr__" not in SourceFileStore.__dict__


def test_facade_delete_file_delegates_to_store(repo):
    stored = repo._runtime.source_files.write_upload("nb-x", "src-x", "f.md", b"x")

    repo._delete_file(str(stored))

    assert not stored.exists()
    assert not stored.parent.exists()


def test_facade_source_raw_text_delegates_to_store(repo, tmp_path):
    md = tmp_path / "raw.md"
    md.write_text("markdown wins", encoding="utf-8")

    source = SimpleNamespace(file_path=str(md))
    assert repo._source_raw_text(source, []) == "markdown wins"

    pdf_source = SimpleNamespace(file_path="scan.pdf")
    elements = [SimpleNamespace(text="alpha"), SimpleNamespace(text="beta")]
    assert repo._source_raw_text(pdf_source, elements) == "alpha\n\nbeta"
