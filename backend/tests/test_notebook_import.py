"""Notebook import (``.snbk``) on SQLite -> SQLite. The checks and worlds live in
``tests/notebook_import_cases.py``; the PostgreSQL lane runs the same checks on the
other three directions (``tests/postgres/test_notebook_import_pg.py``). This module
also holds the backend-independent unit checks of the import's building blocks."""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain.notebook_archive import ARCHIVE_TABLES
from app.services.notebook_transfer import archive_reader
from app.services.notebook_transfer.archive_reader import (
    ArchiveError,
    ArchiveRowSource,
    check_columns,
    check_file_paths,
    inspect_archive,
)
from app.services.sqlite_repository import SQLiteRepository
from tests import notebook_import_cases as cases
from tests.test_notebook_archive_export import BACKEND


def _repository(tmp_path: Path, name: str, monkeypatch) -> SQLiteRepository:
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / f'{name}.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / f"{name}-storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    for key, value in cases.ENV.items():
        monkeypatch.setenv(key, value)
    return SQLiteRepository(Settings())


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repository = _repository(tmp_path, "import", monkeypatch)
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def target(tmp_path, monkeypatch, repo):
    """A second deployment (its own database and storage)."""
    repository = _repository(tmp_path, "target", monkeypatch)
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def out_dir(tmp_path):
    path = tmp_path / "out"
    path.mkdir()
    return path


@pytest.mark.parametrize("world", ["full", "memory"])
def test_round_trip_sqlite_to_sqlite(repo, target, out_dir, monkeypatch, world):
    cases.check_round_trip(repo, BACKEND, target, out_dir, monkeypatch, world=world)


def test_importing_one_file_twice_makes_two_independent_notebooks(repo, target, out_dir, monkeypatch):
    cases.check_importing_twice_makes_two_notebooks(repo, BACKEND, target, out_dir, monkeypatch)


def test_vectors_of_the_same_model_land_byte_for_byte(repo, target, out_dir, monkeypatch):
    cases.check_vectors_kept_byte_for_byte(repo, BACKEND, target, out_dir, monkeypatch)


def test_knowhow_objects_end_with_vectors_after_the_reembed_legs(repo, target, out_dir, monkeypatch):
    cases.check_knowhow_objects_end_with_vectors(repo, BACKEND, target, out_dir, monkeypatch)


def test_vectors_of_another_model_are_dropped_and_questions_embedded_again(
    repo, target, out_dir, monkeypatch
):
    cases.check_vectors_of_another_model(repo, BACKEND, target, out_dir, monkeypatch)


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_tampered_archives_are_refused_and_leave_nothing(repo, out_dir, monkeypatch):
    cases.check_tampered_archives_are_refused(repo, BACKEND, out_dir, monkeypatch)


def test_attachment_mimes_and_source_links_a_serving_path_trusts_are_checked(repo, out_dir):
    cases.check_unservable_values_are_refused(repo, BACKEND, out_dir)


def test_dangling_ids_inside_json_are_cleared_and_counted(repo, out_dir):
    cases.check_dangling_ids_inside_json_are_cleared(repo, BACKEND, out_dir)


def test_every_dangling_reference_is_reported_under_its_label(repo, out_dir, monkeypatch):
    cases.check_dangling_references_are_caught(repo, BACKEND, out_dir, monkeypatch)


def test_a_crashed_import_is_reaped_by_the_startup_sweep(repo, out_dir, monkeypatch):
    cases.check_crash_leaves_a_sentinel_the_sweep_reaps(repo, BACKEND, out_dir, monkeypatch)


def test_a_large_failed_import_compensates_through_the_delete_job(repo, out_dir, monkeypatch):
    cases.check_large_import_compensates_through_the_delete_job(repo, BACKEND, out_dir, monkeypatch)


def test_knowhow_is_projected_before_the_reembed_legs(repo, out_dir, monkeypatch):
    cases.check_post_publish_order(repo, BACKEND, out_dir, monkeypatch)


def test_dry_run_writes_nothing_and_predicts_the_import(repo, out_dir):
    cases.check_dry_run_writes_nothing(repo, BACKEND, out_dir)


# ------------------------------------------------- building blocks (no database)


def test_startup_sweep_runs_before_the_delete_job_sweep(monkeypatch):
    """Server startup hands abandoned imports to the delete job first, so the
    delete-job sweep right after keeps them moving."""
    from app.services import startup_warmup

    order: list[str] = []

    class Runtime:
        class notebook_import:
            @staticmethod
            def sweep_stale_imports():
                order.append("imports")
                return 1

        class notebook_delete:
            @staticmethod
            def sweep_once():
                order.append("delete_jobs")
                return 0

    class Repo:
        _runtime = Runtime()

    startup_warmup._sweep_stale_notebook_imports(Repo())
    startup_warmup._sweep_notebook_delete_jobs_on_start(Repo())
    assert order == ["imports", "delete_jobs"]
    source = Path(startup_warmup.__file__).read_text(encoding="utf-8")
    run_startup = source[source.index("def run_startup"):]
    assert run_startup.index("_sweep_stale_notebook_imports(repo)") < run_startup.index(
        "_sweep_notebook_delete_jobs_on_start(repo)"
    )


def test_import_stale_seconds_is_validated(monkeypatch):
    monkeypatch.setenv("NOTEBOOK_IMPORT_STALE_SECONDS", "30")
    with pytest.raises(ValueError):
        Settings()
    monkeypatch.setenv("NOTEBOOK_IMPORT_STALE_SECONDS", "600")
    assert Settings().notebook_import_stale_seconds == 600


def test_file_paths_collide_case_and_unicode_insensitively():
    check_file_paths(["files/notebooks/a.txt", "files/notebooks/b/c.txt"])
    for paths in (
        ["files/notebooks/A.txt", "files/notebooks/a.txt"],
        ["files/notebooks/café", "files/notebooks/café"],
        ["files/notebooks/x", "files/notebooks/X/y.txt"],
        ["files/notebooks/X/y.txt", "files/notebooks/x"],
    ):
        with pytest.raises(ArchiveError, match="重名"):
            check_file_paths(paths)


def test_columns_must_be_identifiers_the_target_has(repo, out_dir):
    notebook_id = cases.seed_world(repo, BACKEND, "full")
    path = out_dir / "columns.snbk"
    cases.export(repo, notebook_id, path)
    manifest = inspect_archive(path).manifest
    target = repo._runtime.sharing_store.import_table_columns(ARCHIVE_TABLES)
    check_columns(manifest, target)
    narrower = {**target, "sources": target["sources"] - {"summary"}}
    with pytest.raises(ArchiveError, match="导出端版本较新"):
        check_columns(manifest, narrower)
    # An empty table's [] says nothing about the table's columns.
    assert manifest.tables["notebook_bases"].columns == ()
    check_columns(manifest, {**target, "notebook_bases": frozenset()})


def test_row_source_rehashes_every_pass(repo, out_dir):
    """A table read after the preflight must still match the manifest: a pass that
    ends at another checksum or row count fails (the file changed meanwhile)."""
    import dataclasses

    notebook_id = cases.seed_world(repo, BACKEND, "full")
    path = out_dir / "rehash.snbk"
    cases.export(repo, notebook_id, path)
    manifest = inspect_archive(path).manifest
    with zipfile.ZipFile(path) as archive:
        source = ArchiveRowSource(archive, manifest, adapt=lambda t, r: r, page_rows=2)
        pages = list(source.batches("chunks"))
        assert sum(len(p) for p in pages) == manifest.tables["chunks"].rows
        assert all(len(p) <= 2 for p in pages) and len(pages) > 1
        assert list(source.batches("notebook_bases")) == [[]]
        lying = dataclasses.replace(
            manifest,
            tables={**manifest.tables, "chunks": dataclasses.replace(
                manifest.tables["chunks"], sha256="0" * 64)},
        )
        with pytest.raises(ArchiveError, match="自预检后已被改动"):
            list(ArchiveRowSource(archive, lying, adapt=lambda t, r: r, page_rows=2)
                 .batches("chunks"))


def test_file_entries_are_written_new_and_rehashed(repo, out_dir, tmp_path):
    notebook_id = cases.seed_world(repo, BACKEND, "full")
    path = out_dir / "files.snbk"
    cases.export(repo, notebook_id, path)
    inspection = inspect_archive(path)
    name, (size, sha256) = next(iter(inspection.files.items()))
    with zipfile.ZipFile(path) as archive:
        target = tmp_path / "copied"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            archive_reader.copy_file_entry(archive, name, size, sha256, fd)
        finally:
            os.close(fd)
        assert target.stat().st_size == size
        fd = os.open(tmp_path / "wrong", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with pytest.raises(ArchiveError, match="自预检后已被改动"):
                archive_reader.copy_file_entry(archive, name, size, "0" * 64, fd)
        finally:
            os.close(fd)



def test_cli_import_dry_run_reports_and_failures(repo, out_dir, capsys):
    cases.check_cli_import(repo, BACKEND, out_dir, capsys, confirm=())


def test_cluster_seed_ids_are_reminted_from_the_new_objects(repo, out_dir):
    cases.check_cluster_seeds_are_reminted(repo, BACKEND, out_dir)


def test_reembed_legs_follow_the_verdicts_and_always_cover_knowhow_objects_and_relations():
    """PR3 handoff: a workload not kept needs its leg when there are rows of that
    kind; a notebook with a Knowhow table always needs ``kg_objects`` and
    ``kg_relations`` (the post-publish projection rebuilds its objects and edges,
    which carry no vector afterwards), even when every vector was kept."""
    from pathlib import Path as _Path

    from app.services.notebook_copy_writer import NotebookCopyRun
    from app.services.notebook_transfer.import_ import VectorVerdict, reembed_legs

    def verdicts(*not_kept):
        return {
            w: VectorVerdict(kept=w not in not_kept, reason="x", archive=None, target=None)
            for w in ("chunk_embedding", "source_element_embedding",
                      "knowledge_object_embedding", "relation_embedding", "knowhow_embedding")
        }

    run = NotebookCopyRun("nb-old", "n", "u", "nb-new", "now", 10, _Path("/tmp/x"))
    rows = {"chunks": 3, "source_elements": 2, "knowledge_objects": 2, "knowledge_relations": 1}
    assert reembed_legs(verdicts(), run, rows) == ()
    assert reembed_legs(verdicts(), run, {**rows, "knowhow_tables": 1}) == (
        "kg_objects", "kg_relations")
    assert reembed_legs(verdicts("chunk_embedding", "relation_embedding"), run, rows) == (
        "chunks", "kg_relations")
    assert reembed_legs(verdicts("source_element_embedding"), run, {"chunks": 1}) == ()
    run.dropped["knowledge_embeddings.knowledge_object_embedding"] = 1
    assert reembed_legs(verdicts(), run, rows) == ("kg_objects",)
    run.dropped.clear()
    run.knowhow_chunk_ids_old.update({"c1", "c2", "c3"})
    assert reembed_legs(verdicts("knowhow_embedding"), run, rows) == ("chunks",)
    assert reembed_legs(verdicts("chunk_embedding"), run, rows) == ()


def test_seed_space_travels_sqlite_to_sqlite(repo, target, out_dir):
    cases.check_seed_space_travels(repo, BACKEND, target, out_dir)


def test_heartbeat_due_during_file_extraction_finds_the_sentinel(repo, out_dir, monkeypatch):
    cases.check_heartbeat_during_file_extraction(repo, BACKEND, out_dir, monkeypatch)


def test_a_crash_while_extracting_files_is_reaped(repo, out_dir, monkeypatch):
    cases.check_crash_while_extracting_is_reaped(repo, BACKEND, out_dir, monkeypatch)


def test_deep_json_and_an_archive_changed_after_preflight_are_refused(repo, out_dir, monkeypatch):
    cases.check_hostile_content_is_refused(repo, BACKEND, out_dir, monkeypatch)


def test_object_types_follow_the_effective_definition(repo, target, out_dir):
    cases.check_object_types(repo, BACKEND, target, out_dir)


def test_unfinished_sources_fail_and_orphan_attachments_are_dropped(repo, target, out_dir):
    cases.check_unfinished_sources_and_orphan_assets(repo, BACKEND, target, out_dir)


def test_cli_reports_every_failure(repo, out_dir, capsys, monkeypatch):
    cases.check_cli_reports_every_failure(repo, BACKEND, out_dir, capsys, monkeypatch, confirm=())


def test_a_page_closes_at_its_byte_budget(repo, out_dir):
    """Rows stream in pages bounded by bytes as well as rows: a page closes once
    its lines reach ``page_bytes`` (so it holds at most that plus one row)."""
    notebook_id = cases.seed_world(repo, BACKEND, "full")
    path = out_dir / "budget.snbk"
    cases.export(repo, notebook_id, path)
    manifest = inspect_archive(path).manifest
    with zipfile.ZipFile(path) as archive:
        lines = [len(line) + 1 for line in archive.read("rows/source_elements.jsonl").splitlines()]
        one_each = list(ArchiveRowSource(archive, manifest, adapt=lambda t, r: r, page_rows=1000,
                                         page_bytes=1).batches("source_elements"))
        budget = 2 * max(lines)
        bounded = list(ArchiveRowSource(archive, manifest, adapt=lambda t, r: r, page_rows=1000,
                                        page_bytes=budget).batches("source_elements"))
    assert [len(page) for page in one_each] == [1] * len(lines)
    assert sum(len(page) for page in bounded) == len(lines) and len(bounded) > 1
    offset = 0
    for page in bounded:
        size = sum(lines[offset:offset + len(page)])
        assert size < budget + max(lines)
        offset += len(page)


def test_the_dangling_checks_are_anti_joins():
    """Every reference check is one ``NOT EXISTS`` per statement, never ``NOT IN
    (subquery)`` (PostgreSQL's linear fallback past work_mem) nor an ``OR`` of two
    (which keeps the planner from an anti-join), on both backends."""
    from app.repositories.postgres import sharing_store as pg
    from app.repositories.sqlite import sharing_store as sq

    for module in (pg, sq):
        statements = [query for _label, query in module._IMPORT_DANGLING_SQL]
        statements.append(module._DANGLING_RELATION_ENDPOINTS_SQL)
        for query in statements:
            assert " NOT IN " not in query.upper(), query
            assert " OR " not in query.upper(), query
            assert "NOT EXISTS" in query, query


def test_file_paths_compare_under_a_simple_case_mapping():
    """ß and ss are two files on APFS / NTFS, so they do not collide here either."""
    check_file_paths(["files/notebooks/straße.txt", "files/notebooks/strasse.txt"])


def test_a_page_beat_failure_reaches_the_caller_as_itself(repo, out_dir):
    """What ``on_page`` raises mid-table (a reaped import, a database error) is
    not the archive's fault: it reaches the caller unchanged, never rewritten
    as a damaged file."""
    import sqlite3

    from app.services.notebook_transfer.import_ import NotebookImportError

    notebook_id = cases.seed_world(repo, BACKEND, "full")
    path = out_dir / "beat.snbk"
    cases.export(repo, notebook_id, path)
    manifest = inspect_archive(path).manifest
    assert manifest.tables["chunks"].rows > 2
    for failure in (NotebookImportError("导入中途被收割"), sqlite3.OperationalError("locked")):
        beats: list[int] = []

        def on_page(failure=failure, beats=beats):
            beats.append(1)
            if len(beats) == 2:  # the second page: the table is not done yet
                raise failure

        with zipfile.ZipFile(path) as archive:
            source = ArchiveRowSource(archive, manifest, adapt=lambda t, r: r, page_rows=1,
                                      on_page=on_page)
            with pytest.raises(type(failure)) as raised:
                list(source.batches("chunks"))
        assert raised.value is failure


def test_json_text_check_is_linear_on_a_long_string():
    """An 8 MB string full of brackets and escaped quotes is checked within a
    small multiple of its own size (a string-literal regex once took ~1 GB)."""
    import json
    import tracemalloc

    from app.services.notebook_transfer.import_ import _check_json_text

    text = json.dumps({"note": '[\\"{x' * (8 * 1024 * 1024 // 6)})
    assert len(text) > 8 * 1024 * 1024
    tracemalloc.start()
    try:
        _check_json_text(text, "t.c")
        _size, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 64 * 1024 * 1024, peak
