# backend/tests/test_source_file_store.py
"""Task 11: SourceFileStore component — source file persistence (upload
write / delete / raw-text read) extracted off the facade.

Invariants under test:
- write_upload defuses path traversal (safe naming owns directory separators)
  and always lands inside ``storage_dir/notebooks/<notebook_id>/``;
- the stored component ``{source_id}_{name}`` is clamped to the filesystem's
  255-byte cap (budget = 255 − id prefix − suffix, clipped on ENCODED bytes,
  extension preserved so read_source_text's suffix dispatch keeps working);
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
    FILESYSTEM_NAME_MAX_BYTES,
    SourceFileStore,
    delete_source_file,
    safe_filename,
    stored_upload_name,
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


# ------------------------------------------------- filesystem byte budget
# Production ids are fixed-width: `_new_id("src")` = "src-" + uuid4().hex,
# i.e. 36 ASCII bytes. With the "_" separator that is a 37-byte prefix.
WIDEST_ID = "src-" + "0" * 32


def test_write_upload_clamps_component_to_fs_byte_limit_keeping_suffix(store):
    """A browser may legally submit a 255-byte file name; composed with the
    37-byte id prefix that is a 292-byte component — over the 255-BYTE cap of
    ext4/XFS/NTFS.

    ⚠ The failure cannot reproduce on a dev Mac even when the clamp is wrong:
    APFS/HFS+ count 255 UTF-16 units, so the oversized write succeeds here and
    raises `OSError: File name too long` only in Linux production — with the
    storage absolute path inside the error text. Hence the assertions are byte
    arithmetic on the composed name, not "did the write raise".
    """
    long_name = "笔" * 83 + ".pdf"  # 249 + 4 = 253 bytes: a legal client name
    assert len(long_name.encode("utf-8")) <= 255

    stored = store.write_upload("nb-fs", WIDEST_ID, long_name, b"payload")

    component = stored.name
    assert len(component.encode("utf-8")) <= FILESYSTEM_NAME_MAX_BYTES
    # The budget is USED, not over-truncated: a mid-character clip may shave at
    # most one character (≤ 3 bytes here) under the exact cap.
    assert len(component.encode("utf-8")) >= FILESYSTEM_NAME_MAX_BYTES - 3
    assert component.startswith(f"{WIDEST_ID}_")
    # The extension survives: read_source_text and friends dispatch on the
    # STORED path's suffix.
    assert component.endswith(".pdf")
    # Clipping never splits a character: the stored stem is a clean PREFIX of
    # the client stem, no U+FFFD, no dangling bytes.
    stem = component[len(f"{WIDEST_ID}_") : -len(".pdf")]
    assert stem and ("笔" * 83).startswith(stem)
    assert stored.read_bytes() == b"payload"


def test_write_upload_keeps_exact_fit_names_byte_identical(store):
    budget = FILESYSTEM_NAME_MAX_BYTES - len(f"{WIDEST_ID}_".encode("utf-8"))
    name = "a" * (budget - 3) + ".md"  # composes to exactly 255 bytes

    stored = store.write_upload("nb-fit", WIDEST_ID, name, b"x")

    assert stored.name == f"{WIDEST_ID}_{name}"
    assert len(stored.name.encode("utf-8")) == FILESYSTEM_NAME_MAX_BYTES


def test_stored_upload_name_one_byte_over_clips_one_stem_byte():
    budget = FILESYSTEM_NAME_MAX_BYTES - len(f"{WIDEST_ID}_".encode("utf-8"))
    name = "a" * (budget - 2) + ".md"  # would compose to 256 bytes

    component = stored_upload_name(WIDEST_ID, name)

    assert component == f"{WIDEST_ID}_" + "a" * (budget - 3) + ".md"


def test_stored_upload_name_clips_suffixless_names_whole():
    component = stored_upload_name(WIDEST_ID, "x" * 300)

    assert len(component.encode("utf-8")) == FILESYSTEM_NAME_MAX_BYTES
    assert component == f"{WIDEST_ID}_" + "x" * 218


def test_stored_upload_name_folds_oversized_suffix_into_stem():
    # A "suffix" longer than the whole budget is garbage, not an extension:
    # keeping it whole would blow the cap however short the stem got, so it is
    # clipped together with the stem.
    component = stored_upload_name(WIDEST_ID, "v." + "y" * 300)

    assert len(component.encode("utf-8")) == FILESYSTEM_NAME_MAX_BYTES
    assert component.startswith(f"{WIDEST_ID}_v.y")


def test_stored_upload_name_fallback_stem_respects_tiny_budgets():
    # The empty-stem fallback is sliced to the stem budget, so the 255-byte
    # guarantee is arithmetic — it does not rest on ids being 36 bytes wide.
    source_id = "s" * 250  # leaves a 4-byte budget after the "_" separator

    component = stored_upload_name(source_id, "笔笔.md")

    assert component == "s" * 250 + "_s.md"
    assert len(component.encode("utf-8")) == FILESYSTEM_NAME_MAX_BYTES


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
