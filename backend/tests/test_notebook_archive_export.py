"""Notebook export (``.snbk``) on SQLite. The checks and worlds live in
``tests/notebook_archive_cases.py``; PostgreSQL runs the same ones in
``tests/postgres/test_notebook_archive_export_pg.py``. This module only says how SQLite
stores a row, plus the SQLite-only CLI round trip."""
from __future__ import annotations

import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite import sharing_store as store_module
from app.services.notebook_transfer import export as export_module
from app.services.notebook_transfer.archive_reader import inspect_archive
from app.services.notebook_transfer.export import NotebookExportError, NotebookNotFoundError
from app.services.sqlite_repository import SQLiteRepository
from tests import notebook_archive_cases as cases
from tests.cli_schema_guest_support import load_script


def _insert(db, table: str, values: dict) -> None:
    columns = list(values)
    db.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [json.dumps(values[c], ensure_ascii=False) if c in cases.JSON_COLUMNS else values[c]
         for c in columns],
    )


BACKEND = cases.Backend(name="sqlite", insert=_insert)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'archive.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    repository = SQLiteRepository(Settings())
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def out_dir(tmp_path):
    path = tmp_path / "out"
    path.mkdir()
    return path


def test_export_rows_equal_the_copy_snapshot_without_memory(repo, out_dir):
    cases.check_export_equals_snapshot(repo, BACKEND, out_dir)


def test_full_world_export_rows_equal_the_copy_snapshot(repo, out_dir):
    cases.check_full_world_export_equals_snapshot(repo, BACKEND, out_dir)


def test_dirty_notebook_export_carries_no_clusters(repo, out_dir):
    cases.check_dirty_notebook_carries_no_clusters(repo, BACKEND, out_dir)


def test_streaming_read_sees_one_snapshot(repo):
    cases.check_stream_reads_one_snapshot(repo, BACKEND)


def test_stream_consumer_must_read_every_page(repo, out_dir):
    cases.check_consumer_must_read_every_page(repo, BACKEND, out_dir)


def test_export_encodes_bytes_and_timestamps_portably(repo, out_dir):
    cases.check_byte_columns_and_timestamps_are_portable(repo, BACKEND, out_dir)


def test_export_carries_files_assets_and_manifest(repo, out_dir):
    cases.check_full_world_export(repo, BACKEND, out_dir)


def test_export_records_the_global_custom_object_types_it_uses(repo, out_dir):
    cases.check_global_object_types(repo, BACKEND, out_dir)


def test_export_normalises_every_soft_pointer_leaving_the_closure(repo, out_dir):
    cases.check_soft_pointers_are_normalised(repo, BACKEND, out_dir)


def test_export_without_vectors_leaves_the_four_vector_tables_empty(repo, out_dir):
    cases.check_without_vectors(repo, BACKEND, out_dir)


def test_export_streams_each_table_in_pages(repo, out_dir, monkeypatch):
    cases.check_streams_in_pages(repo, BACKEND, out_dir, monkeypatch, store_module)


def test_export_is_not_bounded_by_the_copy_limits(repo, out_dir, monkeypatch):
    cases.check_exports_beyond_the_copy_limits(repo, BACKEND, out_dir, monkeypatch)


def test_export_refuses_unknown_notebooks_and_leaves_no_part_file(repo, out_dir, monkeypatch):
    cases.check_unknown_notebook_and_cleanup(repo, BACKEND, out_dir, monkeypatch)


def test_cli_exports_and_inspects(repo, out_dir, capsys):
    """``scripts/cli.sh notebook export|inspect`` end to end on the deployment settings:
    exit 0 with a JSON report, the archive verifies; an unknown notebook exits 2; an
    output that exists exits 2 without touching it."""
    from app.core.config import get_settings

    cases.seed(repo, BACKEND, cases.char_cases.full_world(), repo.storage_dir,
               cases.char_cases.FULL_FILES)
    cli = load_script("notebook_archive")
    get_settings.cache_clear()
    try:
        assert cli.main(["export", "--notebook", cases.NB, "--out-dir", str(out_dir), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["failed"] == []
        (report,) = payload["exported"]
        archive = out_dir / f"{cases.NB}.snbk"
        assert report["path"] == str(archive) and report["vectors_included"] is True
        assert cli.main(["inspect", str(archive), "--json"]) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["notebook"]["id"] == cases.NB
        assert summary["files"]["count"] == 5
        assert cli.main(["inspect", str(archive)]) == 0
        assert "OK" in capsys.readouterr().out

        assert cli.main(["export", "--notebook", "nb-missing", "--out-dir", str(out_dir)]) == 2
        assert "nb-missing" in capsys.readouterr().err
        assert cli.main(["export", "--notebook", cases.NB, "--out", str(archive)]) == 2
        assert cli.main([
            "export", "--notebook", "a", "--notebook", "b", "--out", str(out_dir / "x.snbk"),
        ]) == 2
        broken = out_dir / "broken.snbk"
        broken.write_bytes(b"not a zip")
        assert cli.main(["inspect", str(broken)]) == 1
    finally:
        get_settings.cache_clear()


# ------------------------------------------------- backend-independent file side


def _seed_full(repo, files=None):
    cases.seed(repo, BACKEND, cases.char_cases.full_world(), repo.storage_dir,
               cases.char_cases.FULL_FILES if files is None else files)
    return Path(repo.storage_dir) / "notebooks" / cases.NB


def _entries(path: Path) -> dict[str, zipfile.ZipInfo]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: info for info in archive.infolist()}


def test_a_file_that_vanishes_after_the_listing_is_counted_not_fatal(repo, out_dir, monkeypatch):
    tree = _seed_full(repo)
    real_walk = os.walk

    def walk_then_delete(root, **kwargs):
        for directory, subdirs, names in real_walk(root, **kwargs):
            if Path(directory) == tree:
                (tree / "doc.md").unlink()
            yield directory, subdirs, names

    monkeypatch.setattr(export_module.os, "walk", walk_then_delete)
    out = out_dir / "vanished.snbk"
    report = cases.export(repo, cases.NB, out)
    inspect_archive(out)
    assert report.missing_files == 1 and report.missing_source_files == 1
    assert "files/notebooks/doc.md" not in _entries(out)
    assert "files/notebooks/extra/notes.txt" in _entries(out)


def test_links_fifos_and_a_swapped_in_link_are_skipped_never_followed(repo, out_dir, tmp_path,
                                                                     monkeypatch):
    tree = _seed_full(repo)
    secret = tmp_path / "outside-secret.txt"
    secret.write_bytes(b"HOST-SECRET")
    (tree / "link.txt").symlink_to(secret)
    (tree / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(tree / "pipe")
    real_walk = os.walk

    def walk_then_swap(root, **kwargs):
        for directory, subdirs, names in real_walk(root, **kwargs):
            if Path(directory) == tree / "extra":
                # Listed as a regular file, replaced by a link before it is opened.
                (tree / "extra" / "notes.txt").unlink()
                (tree / "extra" / "notes.txt").symlink_to(secret)
            yield directory, subdirs, names

    monkeypatch.setattr(export_module.os, "walk", walk_then_swap)
    out = out_dir / "links.snbk"
    report = cases.export(repo, cases.NB, out)
    inspect_archive(out)
    names = set(_entries(out))
    assert "files/notebooks/doc.md" in names
    assert not {"files/notebooks/link.txt", "files/notebooks/pipe",
                "files/notebooks/extra/notes.txt"} & names
    assert not any(name.startswith("files/notebooks/linked-dir") for name in names)
    assert report.skipped_links == 4  # link, linked dir, FIFO, swapped-in link
    assert b"HOST-SECRET" not in out.read_bytes()


def test_a_tree_root_that_is_a_symlink_is_counted_and_not_followed(repo, out_dir, tmp_path):
    _seed_full(repo, files={})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "stolen.txt").write_bytes(b"not this notebook's")
    assets_root = Path(repo.storage_dir) / "assets" / cases.NB
    assets_root.parent.mkdir(parents=True, exist_ok=True)
    assets_root.symlink_to(elsewhere, target_is_directory=True)
    out = out_dir / "rootlink.snbk"
    report = cases.export(repo, cases.NB, out)
    inspect_archive(out)
    assert report.skipped_links == 1 and report.files == 0


def test_a_highly_compressible_large_file_is_stored_and_reads_back(repo, out_dir):
    files = dict(cases.char_cases.FULL_FILES)
    files[f"notebooks/{cases.NB}/zeros.bin"] = bytes(2 * 1024 * 1024)
    _seed_full(repo, files)
    out = out_dir / "zeros.snbk"
    cases.export(repo, cases.NB, out)
    inspect_archive(out)
    entries = _entries(out)
    assert entries["files/notebooks/zeros.bin"].compress_type == zipfile.ZIP_STORED
    assert entries["files/notebooks/doc.md"].compress_type == zipfile.ZIP_DEFLATED


@pytest.mark.parametrize(
    "constant, value, message",
    [
        ("MAX_ENTRIES", 3, "条目数"),
        ("MAX_TOTAL_UNCOMPRESSED_BYTES", 10, "总量"),
        ("MAX_JSONL_LINE_BYTES", 20, "单行上限"),
    ],
)
def test_the_writer_refuses_to_break_a_reader_limit(repo, out_dir, monkeypatch, constant,
                                                    value, message):
    _seed_full(repo)
    monkeypatch.setattr(export_module, constant, value)
    with pytest.raises(NotebookExportError, match=message):
        cases.export(repo, cases.NB, out_dir / "capped.snbk")
    assert list(out_dir.iterdir()) == []


def test_the_writer_refuses_a_rows_entry_beyond_the_ratio_limit(repo, out_dir, monkeypatch):
    _seed_full(repo)
    monkeypatch.setattr(export_module, "MAX_ENTRY_COMPRESSION_RATIO", 1)
    monkeypatch.setattr(export_module, "COMPRESSION_RATIO_FLOOR_BYTES", 0)
    with pytest.raises(NotebookExportError, match="压缩比"):
        cases.export(repo, cases.NB, out_dir / "ratio.snbk")
    assert list(out_dir.iterdir()) == []


def test_only_the_root_read_refusal_becomes_notebook_not_found(repo, out_dir, monkeypatch):
    _seed_full(repo)
    with pytest.raises(NotebookNotFoundError):
        cases.export(repo, "nb-missing", out_dir / "missing.snbk")

    def broken(self, table, row):
        raise KeyError(cases.NB)

    monkeypatch.setattr(export_module._Normalizer, "__call__", broken)
    with pytest.raises(KeyError) as raised:
        cases.export(repo, cases.NB, out_dir / "broken.snbk")
    assert not isinstance(raised.value, NotebookNotFoundError)


def test_two_exports_to_one_path_never_overwrite_each_other(repo, out_dir, monkeypatch):
    """B starts and finishes to the same path while A is mid-stream: B's archive is
    published intact, A refuses to publish over it, and neither leaves a part file."""
    _seed_full(repo)
    target = out_dir / "same.snbk"
    store = repo._runtime.sharing_store
    real = store.iter_copy_rows
    outcome: dict = {}

    def run_b() -> None:
        try:
            outcome["b"] = cases.export(repo, cases.NB, target)
        except BaseException as exc:  # noqa: BLE001 -- asserted below
            outcome["b_error"] = exc

    def iter_with_rival(notebook_id, *, on_table, skip_tables=frozenset()):
        def hooked(table, pages):
            on_table(table, pages)
            if table == "sources" and "b" not in outcome:
                monkeypatch.setattr(store, "iter_copy_rows", real)
                rival = threading.Thread(target=run_b)
                rival.start()
                rival.join()
        return real(notebook_id, on_table=hooked, skip_tables=skip_tables)

    monkeypatch.setattr(store, "iter_copy_rows", iter_with_rival)
    with pytest.raises(NotebookExportError, match="已存在"):
        cases.export(repo, cases.NB, target)
    assert "b_error" not in outcome, outcome.get("b_error")
    inspect_archive(target)
    assert target.stat().st_size == outcome["b"].archive_bytes
    assert sorted(p.name for p in out_dir.iterdir()) == ["same.snbk"]


def test_cli_reports_what_was_exported_before_a_later_failure(repo, out_dir, capsys):
    from app.core.config import get_settings

    _seed_full(repo)
    cli = load_script("notebook_archive")
    get_settings.cache_clear()
    try:
        status = cli.main([
            "export", "--notebook", cases.NB, "--notebook", "nb-missing",
            "--out-dir", str(out_dir), "--json",
        ])
    finally:
        get_settings.cache_clear()
    assert status == 2
    payload = json.loads(capsys.readouterr().out)
    assert [item["notebook_id"] for item in payload["exported"]] == [cases.NB]
    assert [item["notebook_id"] for item in payload["failed"]] == ["nb-missing"]
    inspect_archive(out_dir / f"{cases.NB}.snbk")
