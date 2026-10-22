"""Notebook export (``.snbk``) on SQLite. The checks and worlds live in
``tests/notebook_archive_cases.py``; PostgreSQL runs the same ones in
``tests/postgres/test_notebook_archive_export_pg.py``. This module only says how SQLite
stores a row, plus the SQLite-only CLI round trip."""
from __future__ import annotations

import errno
import json
import os
import stat
import threading
import time
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


def _carries(path: Path, needle: bytes) -> bool:
    """Whether any entry's DECOMPRESSED content holds ``needle``: the raw file
    bytes are deflated, so a search over them proves nothing."""
    with zipfile.ZipFile(path) as archive:
        return any(needle in archive.read(info) for info in archive.infolist())


def _entries(path: Path) -> dict[str, zipfile.ZipInfo]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: info for info in archive.infolist()}


def test_a_file_that_vanishes_after_the_listing_is_counted_not_fatal(repo, out_dir, monkeypatch):
    tree = _seed_full(repo)
    real_fwalk = os.fwalk

    def walk_then_delete(root, **kwargs):
        for directory, subdirs, names, dir_fd in real_fwalk(root, **kwargs):
            if Path(directory) == tree:
                (tree / "doc.md").unlink()
            yield directory, subdirs, names, dir_fd

    monkeypatch.setattr(export_module.os, "fwalk", walk_then_delete)
    out = out_dir / "vanished.snbk"
    report = cases.export(repo, cases.NB, out)
    inspect_archive(out)
    assert report.missing_files == 1 and report.missing_source_files == 1
    assert "files/notebooks/doc.md" not in _entries(out)
    assert "files/notebooks/extra/notes.txt" in _entries(out)


def _export_with_deadline(repo, out: Path, *, fifos=(), timeout: float = 60.0):
    """Run the export in a thread so that a reader blocked in ``open()`` on a FIFO
    (``O_NONBLOCK`` missing) fails the test instead of hanging the run; the FIFO is
    then opened for writing once, which releases the stuck reader."""
    result: dict = {}

    def run() -> None:
        try:
            result["report"] = cases.export(repo, cases.NB, out)
        except BaseException as exc:  # noqa: BLE001 -- re-raised below
            result["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        for fifo in fifos:
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        worker.join(10)
        pytest.fail("the export blocked on a FIFO (opened without O_NONBLOCK)")
    if "error" in result:
        raise result["error"]
    return result["report"]


def test_links_and_fifos_are_skipped_never_followed_never_blocking(repo, out_dir, tmp_path):
    tree = _seed_full(repo)
    secret = tmp_path / "outside-secret.txt"
    secret.write_bytes(b"HOST-SECRET")
    (tree / "link.txt").symlink_to(secret)
    (tree / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(tree / "pipe")
    out = out_dir / "links.snbk"
    report = _export_with_deadline(repo, out, fifos=[tree / "pipe"])
    inspect_archive(out)
    names = set(_entries(out))
    assert {"files/notebooks/doc.md", "files/notebooks/extra/notes.txt"} <= names
    assert not {"files/notebooks/link.txt", "files/notebooks/pipe"} & names
    assert not any(name.startswith("files/notebooks/linked-dir") for name in names)
    assert report.skipped_links == 3  # link, linked dir, FIFO
    assert not _carries(out, b"HOST-SECRET")


def test_a_link_swapped_in_right_before_the_open_is_not_followed(repo, out_dir, tmp_path,
                                                                 monkeypatch):
    """The file is a regular file whenever anyone looks at it by name, and becomes a
    link only at the very moment it is opened: only a check on the opened descriptor
    (``O_NOFOLLOW`` + ``fstat``) can refuse it; a name-based check (``lstat``, then an
    ``open`` that follows links) would carry the host file."""
    tree = _seed_full(repo)
    secret = tmp_path / "outside-secret.txt"
    secret.write_bytes(b"HOST-SECRET")
    target = tree / "extra" / "notes.txt"
    real_open = os.open
    swapped: list[int] = []

    def open_after_swap(path, flags, *args, **kwargs):
        # Files are opened by leaf name relative to their directory descriptor.
        if os.fspath(path) == target.name and kwargs.get("dir_fd") is not None and not swapped:
            target.unlink()
            target.symlink_to(secret)
            swapped.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(export_module.os, "open", open_after_swap)
    out = out_dir / "swap.snbk"
    report = cases.export(repo, cases.NB, out)
    monkeypatch.undo()
    inspect_archive(out)
    assert swapped, "the hook must have fired"
    assert "files/notebooks/extra/notes.txt" not in _entries(out)
    assert report.skipped_links == 1
    assert not _carries(out, b"HOST-SECRET")


def test_a_directory_swapped_for_a_link_right_before_a_file_open_is_not_followed(
    repo, out_dir, tmp_path, monkeypatch
):
    """codex #974 r2 (P1): ``O_NOFOLLOW`` guards only the last component. A
    directory above a file, replaced by a link to a host directory between the
    listing and the file's open, must not lead the open outside the tree: the
    open is anchored to the descriptor of the directory that was listed."""
    tree = _seed_full(repo)
    inner = tree / "deep"
    inner.mkdir()
    (inner / "leaf.txt").write_bytes(b"NOTEBOOK-OWN")
    host = tmp_path / "host-dir"
    host.mkdir()
    (host / "leaf.txt").write_bytes(b"HOST-SECRET")
    real_open = os.open
    swapped: list[str] = []

    def open_after_swap(path, flags, *args, **kwargs):
        name = os.fspath(path)
        if os.path.basename(name) == "leaf.txt" and not swapped:
            inner.rename(tree / "deep.moved")
            inner.symlink_to(host, target_is_directory=True)
            swapped.append(name)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(export_module.os, "open", open_after_swap)
    out = out_dir / "dirswap.snbk"
    cases.export(repo, cases.NB, out)
    monkeypatch.undo()
    inspect_archive(out)
    assert swapped, "the hook must have fired"
    assert not _carries(out, b"HOST-SECRET")
    with zipfile.ZipFile(out) as archive:
        assert archive.read("files/notebooks/deep/leaf.txt") == b"NOTEBOOK-OWN"


def _refuse_open(monkeypatch, leaf: str) -> list[str]:
    """Make the descriptor-relative open of ``leaf`` fail with EACCES, the way
    mode bits would for a non-root user. Injected rather than ``chmod``-based so
    the case holds whoever runs it (root ignores mode bits) and needs no skip
    marker (the test architecture policy forbids them)."""
    real_open = os.open
    refused: list[str] = []

    def open_or_refuse(path, flags, *args, **kwargs):
        if os.fspath(path) == leaf and kwargs.get("dir_fd") is not None:
            refused.append(leaf)
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), leaf)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(export_module.os, "open", open_or_refuse)
    return refused


def test_an_unreadable_file_fails_the_export(repo, out_dir, monkeypatch):
    _seed_full(repo)
    refused = _refuse_open(monkeypatch, "doc.md")
    with pytest.raises(NotebookExportError, match="EACCES"):
        cases.export(repo, cases.NB, out_dir / "eacces.snbk")
    monkeypatch.undo()
    assert refused == ["doc.md"]
    assert list(out_dir.iterdir()) == []


def test_an_unreadable_directory_fails_the_export(repo, out_dir, monkeypatch):
    # os.fwalk opens each subdirectory relative to its parent's descriptor; a
    # refused open reaches the walk's onerror, which must fail the export.
    _seed_full(repo)
    refused = _refuse_open(monkeypatch, "extra")
    with pytest.raises(NotebookExportError, match="目录"):
        cases.export(repo, cases.NB, out_dir / "eacces-dir.snbk")
    monkeypatch.undo()
    assert refused == ["extra"]
    assert list(out_dir.iterdir()) == []


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


def test_the_ratio_probe_reads_little_of_an_ordinary_file_and_stays_exact(tmp_path,
                                                                          monkeypatch):
    ordinary = tmp_path / "random.bin"
    ordinary.write_bytes(os.urandom(4 * 1024 * 1024))
    zeros = tmp_path / "zeros.bin"
    zeros.write_bytes(bytes(4 * 1024 * 1024))
    real_read = os.read
    read_bytes: list[int] = []

    def counting_read(fd, n):
        block = real_read(fd, n)
        read_bytes.append(len(block))
        return block

    monkeypatch.setattr(export_module.os, "read", counting_read)
    for path, expected in ((ordinary, True), (zeros, False)):
        read_bytes.clear()
        fd = os.open(path, os.O_RDONLY)
        try:
            assert export_module._deflate_within_ratio(fd, path.stat().st_size) is expected
        finally:
            os.close(fd)
        if expected:
            # Proven after a few 64 KiB reads, not after a 1 MiB copy block.
            assert max(read_bytes) <= export_module._PROBE_BLOCK
            assert sum(read_bytes) <= 4 * export_module._PROBE_BLOCK
        else:
            assert sum(read_bytes) == path.stat().st_size


def _no_hard_links(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(export_module.os, "link", refuse)


def test_publishes_by_rename_where_hard_links_are_refused(repo, out_dir, monkeypatch):
    _seed_full(repo)
    _no_hard_links(monkeypatch)
    out = out_dir / "nolink.snbk"
    report = cases.export(repo, cases.NB, out)
    inspect_archive(out)
    assert sorted(p.name for p in out_dir.iterdir()) == ["nolink.snbk"]
    assert out.stat().st_size == report.archive_bytes


def test_a_failed_rename_leaves_neither_part_nor_placeholder(repo, out_dir, monkeypatch):
    _seed_full(repo)
    _no_hard_links(monkeypatch)

    def failing_replace(*args, **kwargs):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(export_module.os, "replace", failing_replace)
    with pytest.raises(OSError):
        cases.export(repo, cases.NB, out_dir / "half.snbk")
    assert list(out_dir.iterdir()) == []


def test_the_part_file_and_the_directory_are_fsynced_around_publication(repo, out_dir,
                                                                        monkeypatch):
    _seed_full(repo)
    events: list[str] = []
    real_fsync, real_link = os.fsync, os.link

    def fsync(fd):
        events.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync-file")
        return real_fsync(fd)

    def link(src, dst, *args, **kwargs):
        events.append("link")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(export_module.os, "fsync", fsync)
    monkeypatch.setattr(export_module.os, "link", link)
    cases.export(repo, cases.NB, out_dir / "durable.snbk")
    published = [e for e in events if e in ("fsync-file", "link", "fsync-dir")]
    assert published[-3:] == ["fsync-file", "link", "fsync-dir"]


def test_leftover_part_files_are_reported_never_deleted(repo, out_dir):
    _seed_full(repo)
    out = out_dir / "again.snbk"
    old = out_dir / "again.snbk.0123456789ab.part"
    fresh = out_dir / "again.snbk.ba9876543210.part"
    old.write_bytes(b"killed")
    fresh.write_bytes(b"maybe running")
    hours_ago = time.time() - 2 * export_module.STALE_PART_SECONDS
    os.utime(old, (hours_ago, hours_ago))
    report = cases.export(repo, cases.NB, out)
    assert report.stale_part_files == (old.name,)
    assert old.exists() and fresh.exists()


def test_cli_reports_an_io_error_on_a_later_notebook(repo, out_dir, capsys, monkeypatch):
    from app.core.config import get_settings

    _seed_full(repo)
    real_export = export_module.export_notebook
    calls: list[str] = []

    def full_disk_on_second(store, notebook_id, *args, **kwargs):
        calls.append(notebook_id)
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_export(store, notebook_id, *args, **kwargs)

    monkeypatch.setattr(export_module, "export_notebook", full_disk_on_second)
    cli = load_script("notebook_archive")
    get_settings.cache_clear()
    try:
        status = cli.main([
            # The second id needs no rows: the injected disk error comes first.
            "export", "--notebook", cases.NB, "--notebook", "nb-second",
            "--out-dir", str(out_dir), "--json",
        ])
    finally:
        get_settings.cache_clear()
    assert status == 1
    payload = json.loads(capsys.readouterr().out)
    assert [item["notebook_id"] for item in payload["exported"]] == [cases.NB]
    (failed,) = payload["failed"]
    assert failed["notebook_id"] == "nb-second"
    assert "No space left" in failed["error"]


@pytest.mark.parametrize("tree", ["notebooks", "assets", "linked"])
def test_an_output_inside_an_exported_tree_is_refused(repo, tmp_path, tree, monkeypatch):
    """codex #974 r1: written inside the notebook's file or asset tree, the
    archive's own part file would be walked and copied into itself until the
    disk fills. Refused before anything is written, on resolved paths.

    Creating the writer is made to fail loudly: without the refusal this case
    would otherwise reproduce the self-copy and never finish."""

    def no_writer(*_args, **_kwargs):
        raise AssertionError("the writer was created for an output inside an exported tree")

    monkeypatch.setattr(export_module._ArchiveWriter, "create", no_writer)
    notebook_root = _seed_full(repo)
    assets_root = Path(repo.storage_dir) / "assets" / cases.NB
    assets_root.mkdir(parents=True, exist_ok=True)
    if tree == "notebooks":
        target_dir = notebook_root / "exports"
    elif tree == "assets":
        target_dir = assets_root
    else:
        target_dir = tmp_path / "looks-elsewhere"
        target_dir.symlink_to(notebook_root, target_is_directory=True)
    target_dir.mkdir(exist_ok=True)
    before = sorted(p.name for p in target_dir.iterdir())
    with pytest.raises(NotebookExportError, match="被导出笔记本的文件目录内"):
        cases.export(repo, cases.NB, target_dir / "self.snbk")
    assert sorted(p.name for p in target_dir.iterdir()) == before


def test_an_output_beside_the_exported_trees_is_accepted(repo):
    _seed_full(repo)
    beside = Path(repo.storage_dir) / "exports"
    beside.mkdir()
    cases.export(repo, cases.NB, beside / "ok.snbk")
    inspect_archive(beside / "ok.snbk")


@pytest.mark.parametrize("driver", ["sqlite", "postgres"])
def test_cli_reports_a_database_failure_on_a_later_notebook_without_its_message(
    repo, out_dir, capsys, monkeypatch, driver
):
    """codex #974 r1: driver errors are not OSError; a dropped connection on a
    later notebook still yields the structured report, naming the exception
    type (and SQLSTATE) but never the driver message's connection details."""
    import sqlite3

    import psycopg

    from app.core.config import get_settings

    _seed_full(repo)
    real_export = export_module.export_notebook
    calls: list[str] = []
    secret = "connection to server at secret-db.internal port 5433 user=svc failed"

    def drop_on_second(store, notebook_id, *args, **kwargs):
        calls.append(notebook_id)
        if len(calls) == 2:
            if driver == "sqlite":
                raise sqlite3.OperationalError(secret)
            raise psycopg.OperationalError(secret)
        return real_export(store, notebook_id, *args, **kwargs)

    monkeypatch.setattr(export_module, "export_notebook", drop_on_second)
    cli = load_script("notebook_archive")
    get_settings.cache_clear()
    try:
        status = cli.main([
            "export", "--notebook", cases.NB, "--notebook", "nb-second",
            "--out-dir", str(out_dir), "--json",
        ])
    finally:
        get_settings.cache_clear()
    captured = capsys.readouterr()
    assert status == 1
    payload = json.loads(captured.out)
    assert [item["notebook_id"] for item in payload["exported"]] == [cases.NB]
    (failed,) = payload["failed"]
    assert failed["notebook_id"] == "nb-second"
    assert failed["error"].startswith("数据库错误: OperationalError")
    assert "secret-db" not in captured.out + captured.err


def test_a_manifest_beyond_the_reader_limit_fails_the_export(repo, out_dir, monkeypatch):
    """codex #974 r3: the reader refuses a manifest larger than
    MAX_MANIFEST_BYTES, so the export must refuse it too rather than publish
    an archive its own reader rejects. The limit is lowered to make the full
    world's manifest exceed it."""
    _seed_full(repo)
    monkeypatch.setattr(export_module, "MAX_MANIFEST_BYTES", 256)
    out = out_dir / "huge-manifest.snbk"
    with pytest.raises(NotebookExportError, match="清单过大"):
        cases.export(repo, cases.NB, out)
    assert list(out_dir.iterdir()) == []
