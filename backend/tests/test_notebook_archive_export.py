"""Notebook export (``.snbk``) on SQLite. The checks and worlds live in
``tests/notebook_archive_cases.py``; PostgreSQL runs the same ones in
``tests/postgres/test_notebook_archive_export_pg.py``. This module only says how SQLite
stores a row, plus the SQLite-only CLI round trip."""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.repositories.sqlite import sharing_store as store_module
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


def test_export_encodes_bytes_and_timestamps_portably(repo, out_dir):
    cases.check_byte_columns_and_timestamps_are_portable(repo, BACKEND, out_dir)


def test_export_carries_files_assets_and_manifest(repo, out_dir):
    cases.check_full_world_export(repo, BACKEND, out_dir)


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
        (report,) = json.loads(capsys.readouterr().out)
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
