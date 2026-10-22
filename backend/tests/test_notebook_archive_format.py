"""The ``.snbk`` format contract: the archive table set IS the copy closure, the portable
row encoding has one definition, and ``inspect`` refuses every tampered archive.

Backend-independent: the tampering cases rewrite one exported archive (SQLite world) at
the zip level; the reader never touches a database."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain import notebook_archive, portable_rows
from app.domain.notebook_archive import (
    ARCHIVE_TABLES,
    FILES_INDEX_NAME,
    MANIFEST_NAME,
    VECTOR_TABLES,
    ArchiveManifest,
    ArchiveVersionTooNewError,
    ManifestError,
    entry_kind,
    rows_entry,
)
from app.migration.sync import package
from app.services.notebook_transfer.archive_reader import ArchiveError, inspect_archive
from app.services.sqlite_repository import SQLiteRepository
from tests import notebook_archive_cases as cases
from tests.cli_schema_guest_support import load_script
from tests.test_notebook_archive_export import BACKEND


def test_archive_tables_are_exactly_the_copy_closure_on_both_backends():
    """Adding a table to the deep copy's closure (either backend's
    ``_COPY_SNAPSHOT_QUERIES``) turns this red: the archive format must list it in
    ``ARCHIVE_TABLES`` AND ``FORMAT_VERSION`` must move, because an older reader would
    silently drop the new table's rows."""
    from app.repositories.postgres import sharing_store as pg_store
    from app.repositories.sqlite import sharing_store as sqlite_store

    for module in (sqlite_store, pg_store):
        closure = tuple(table for table, _query in module._COPY_SNAPSHOT_QUERIES)
        assert closure == ARCHIVE_TABLES, (
            f"{module.__name__}: the copy closure changed; update "
            "app.domain.notebook_archive.ARCHIVE_TABLES and bump FORMAT_VERSION"
        )
    assert pg_store._COPY_TABLES == frozenset(ARCHIVE_TABLES)
    assert set(VECTOR_TABLES) < set(ARCHIVE_TABLES)
    assert notebook_archive.FORMAT_VERSION == 1
    assert notebook_archive.FORMAT_VERSION in notebook_archive.READABLE_FORMAT_VERSIONS


def test_sync_package_reexports_the_one_portable_encoding():
    for name in (
        "BYTES_KEY", "encode_value", "decode_value", "encode_row", "decode_row",
        "utc_timestamp_text", "json_line", "json_document", "is_safe_identifier",
        "is_safe_path_segment", "is_safe_relative_path", "column_encoder",
        "_SAFE_IDENTIFIER_RE",
    ):
        assert getattr(package, name) is getattr(portable_rows, name), name


def test_entry_name_whitelist():
    assert entry_kind("manifest.json") == "manifest"
    assert entry_kind("files.jsonl") == "files_index"
    assert entry_kind("rows/chunks.jsonl") == "rows"
    assert entry_kind("files/notebooks/研究 notes.pdf") == "file"
    assert entry_kind("files/assets/asset-1.png") == "file"
    for bad in (
        "rows/memory_items.jsonl", "files/notebooks/../x", "files/notebooks/a\\b",
        "/manifest.json", "files/notebooks/", "files/other/x", "files/notebooks/a//b",
        "C:/x", "notes.txt",
    ):
        assert entry_kind(bad) is None, bad


def test_manifest_round_trips_and_validates():
    raw = {
        "format": notebook_archive.FORMAT, "format_version": 1,
        "exported_at": "2026-10-22T00:00:00+00:00", "schema": {}, "notebook": {"name": "n"},
        "kg": {"starts_dirty": False, "clusters_dropped": False},
        "embeddings": {
            "vectors_included": True,
            "workloads": {w: None for w in notebook_archive.EMBEDDING_WORKLOADS},
        },
        "tables": {
            t: {"columns": [], "rows": 0, "bytes": 0, "sha256": "0" * 64} for t in ARCHIVE_TABLES
        },
        "files": {"count": 0, "bytes": 0, "index_bytes": 0, "index_sha256": "0" * 64},
        "global_object_types": [], "excluded": {"memory": "not_carried", "mount_edges": 0},
    }
    manifest = ArchiveManifest.from_dict(raw)
    assert manifest.to_dict() == raw
    with pytest.raises(ArchiveVersionTooNewError):
        ArchiveManifest.from_dict({**raw, "format_version": 2})
    with pytest.raises(ManifestError):
        ArchiveManifest.from_dict({**raw, "format": "something-else"})
    with pytest.raises(ManifestError):
        ArchiveManifest.from_dict({**raw, "tables": {**raw["tables"], "memory_items": {}}})
    with pytest.raises(ManifestError):
        ArchiveManifest.from_dict({**raw, "excluded": {"memory": 3, "mount_edges": 0}})


# ------------------------------------------------------------------ tampering


@pytest.fixture
def archive(tmp_path, monkeypatch) -> Path:
    tmp = tmp_path / "source"
    tmp.mkdir()
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp / 'a.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    repo = SQLiteRepository(Settings())
    try:
        cases.seed(repo, BACKEND, cases.char_cases.full_world(), repo.storage_dir,
                   cases.char_cases.FULL_FILES)
        out = tmp / "good.snbk"
        cases.export(repo, cases.NB, out)
    finally:
        repo.close()
    return out


def _rewrite(source: Path, target: Path, *, replace=None, drop=(), add=None) -> Path:
    """Copy ``source`` entry by entry, replacing/dropping/adding entries (the zip layer
    stays valid: CRCs are recomputed)."""
    replace = replace or {}
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as new:
        for info in old.infolist():
            if info.filename in drop:
                continue
            data = old.read(info)
            if info.filename in replace:
                data = replace[info.filename](data)
            new.writestr(info, data)
        for name, data in (add or {}).items():
            new.writestr(name, data)
    return target


def _manifest_edit(edit):
    def apply(data: bytes) -> bytes:
        manifest = json.loads(data)
        edit(manifest)
        return json.dumps(manifest).encode()
    return apply


def _flip_last_byte(data: bytes) -> bytes:
    return data[:-2] + bytes([data[-2] ^ 0x01]) + data[-1:]


def test_a_good_archive_verifies(archive):
    inspection = inspect_archive(archive)
    assert inspection.manifest.notebook["id"] == cases.NB
    assert inspection.summary()["rows"]["notebooks"] == 1


@pytest.mark.parametrize(
    "name, rewrite, message",
    [
        ("row-byte", lambda a, t: _rewrite(a, t, replace={
            rows_entry("chunks"): lambda d: d.replace(b"web chunk", b"web chunK", 1)}),
         "校验和"),
        ("file-byte", lambda a, t: _rewrite(
            a, t, replace={"files/notebooks/doc.md": _flip_last_byte}), "校验和"),
        ("index-byte", lambda a, t: _rewrite(a, t, replace={FILES_INDEX_NAME: _flip_last_byte}),
         "files.jsonl"),
        ("row-count", lambda a, t: _rewrite(a, t, replace={MANIFEST_NAME: _manifest_edit(
            lambda m: m["tables"]["chunks"].update(rows=m["tables"]["chunks"]["rows"] + 1))}),
         "行数"),
        ("missing-manifest", lambda a, t: _rewrite(a, t, drop={MANIFEST_NAME}), "manifest"),
        ("newer-version", lambda a, t: _rewrite(a, t, replace={MANIFEST_NAME: _manifest_edit(
            lambda m: m.update(format_version=2))}), "更新"),
        ("missing-rows", lambda a, t: _rewrite(a, t, drop={rows_entry("sources")}), "缺少条目"),
        ("undeclared-entry", lambda a, t: _rewrite(
            a, t, add={"files/notebooks/smuggled.txt": b"x"}), "未在清单中声明"),
        ("traversal-name", lambda a, t: _rewrite(
            a, t, add={"files/notebooks/../../etc/passwd": b"x"}), "不允许的条目名"),
        ("unknown-table", lambda a, t: _rewrite(
            a, t, add={"rows/notebook_members.jsonl": b"{}\n"}), "不允许的条目名"),
        ("undeclared-column", lambda a, t: _rewrite(a, t, replace={
            MANIFEST_NAME: _manifest_edit(
                lambda m: m["tables"]["notebooks"].update(columns=["id"]))}), "未声明的列"),
        ("not-json-manifest", lambda a, t: _rewrite(
            a, t, replace={MANIFEST_NAME: lambda d: b"{"}), "JSON"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_inspect_refuses_a_tampered_archive(archive, tmp_path, name, rewrite, message):
    tampered = rewrite(archive, tmp_path / f"{name}.snbk")
    with pytest.raises(ArchiveError, match=message):
        inspect_archive(tampered)


def test_inspect_refuses_a_corrupted_compressed_byte(archive, tmp_path):
    """A byte flipped on disk inside a compressed entry is caught by the zip layer
    (CRC / inflate) and reported as a damaged archive, never trusted."""
    data = bytearray(archive.read_bytes())
    with zipfile.ZipFile(archive) as zf:
        info = zf.getinfo(rows_entry("knowledge_objects"))
    header = info.header_offset
    name_length = int.from_bytes(data[header + 26:header + 28], "little")
    extra_length = int.from_bytes(data[header + 28:header + 30], "little")
    offset = header + 30 + name_length + extra_length + 4
    data[offset] ^= 0xFF
    damaged = tmp_path / "damaged.snbk"
    damaged.write_bytes(bytes(data))
    with pytest.raises(ArchiveError):
        inspect_archive(damaged)


def test_inspect_refuses_duplicate_names_and_symlinks(archive, tmp_path):
    duplicated = tmp_path / "dup.snbk"
    with pytest.warns(UserWarning):
        _rewrite(archive, duplicated, add={MANIFEST_NAME: b"{}"})
    with pytest.raises(ArchiveError, match="重复"):
        inspect_archive(duplicated)

    linked = tmp_path / "link.snbk"
    with zipfile.ZipFile(archive) as old, zipfile.ZipFile(linked, "w") as new:
        for info in old.infolist():
            data = old.read(info)
            if info.filename == "files/notebooks/doc.md":
                info.create_system = 3
                info.external_attr = (0o120777) << 16
            new.writestr(info, data)
    with pytest.raises(ArchiveError, match="符号链接"):
        inspect_archive(linked)


def test_cli_inspect_exit_status(archive, tmp_path, capsys):
    cli = load_script("notebook_archive")
    assert cli.main(["inspect", str(archive)]) == 0
    tampered = _rewrite(archive, tmp_path / "t.snbk", drop={MANIFEST_NAME})
    assert cli.main(["inspect", str(tampered)]) == 1
    assert "manifest" in capsys.readouterr().err
