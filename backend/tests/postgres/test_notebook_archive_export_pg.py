"""Notebook export (``.snbk``) on real PostgreSQL: the twin of
``tests/test_notebook_archive_export.py``, running the same checks on the same worlds
(``tests/notebook_archive_cases.py``). PostgreSQL reads each statement through a named
server-side cursor inside one REPEATABLE READ transaction; this module only says how
PostgreSQL stores a row (JSON columns as jsonb)."""
from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.repositories.postgres import sharing_store as store_module
from app.services.repository_factory import create_repository
from tests import notebook_archive_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_archive_export"),
]


def _insert(db, table: str, values: dict) -> None:
    values = dict(values)
    if table == "users":
        values.update(username=values["id"], password_hash="", password_salt="",
                      password_iterations=0)
    columns = list(values)
    db.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('%s' for _ in columns)})",
        [Jsonb(values[c]) if c in cases.JSON_COLUMNS else values[c] for c in columns],
    )


BACKEND = cases.Backend(name="postgres", insert=_insert)


@pytest.fixture
def repo(postgres_scope, tmp_path, monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    repository = create_repository(Settings())
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def out_dir(tmp_path):
    path = tmp_path / "out"
    path.mkdir()
    return path


def test_export_rows_equal_the_copy_snapshot_without_memory_pg(repo, out_dir):
    cases.check_export_equals_snapshot(repo, BACKEND, out_dir)


def test_export_encodes_bytes_and_timestamps_portably_pg(repo, out_dir):
    cases.check_byte_columns_and_timestamps_are_portable(repo, BACKEND, out_dir)


def test_export_carries_files_assets_and_manifest_pg(repo, out_dir):
    cases.check_full_world_export(repo, BACKEND, out_dir)


def test_export_normalises_every_soft_pointer_leaving_the_closure_pg(repo, out_dir):
    cases.check_soft_pointers_are_normalised(repo, BACKEND, out_dir)


def test_export_without_vectors_leaves_the_four_vector_tables_empty_pg(repo, out_dir):
    cases.check_without_vectors(repo, BACKEND, out_dir)


def test_export_streams_each_table_in_pages_pg(repo, out_dir, monkeypatch):
    cases.check_streams_in_pages(repo, BACKEND, out_dir, monkeypatch, store_module)


def test_export_is_not_bounded_by_the_copy_limits_pg(repo, out_dir, monkeypatch):
    cases.check_exports_beyond_the_copy_limits(repo, BACKEND, out_dir, monkeypatch)


def test_export_refuses_unknown_notebooks_and_leaves_no_part_file_pg(
    repo, out_dir, monkeypatch
):
    cases.check_unknown_notebook_and_cleanup(repo, BACKEND, out_dir, monkeypatch)


def test_streaming_read_sees_one_snapshot_pg(repo):
    """Every statement of the export reads the snapshot pinned by the root read: a chunk
    committed by another connection while the stream is open (after ``sources``, before
    ``chunks`` is read) does not appear in it."""
    cases.seed(repo, BACKEND, cases.char_cases.full_world(), repo.storage_dir)
    store = repo._runtime.sharing_store
    seen: dict[str, list] = {}
    late_chunk = {
        "id": "ck-late", "notebook_id": cases.NB, "source_id": "src-web",
        "text": "committed mid-export", "element_ids": [], "created_at": cases.memory_cases.NOW,
    }

    def on_table(table, pages):
        rows = [row for page in pages for row in page]
        seen[table] = rows
        if table == "sources":
            with repo._runtime.database.write() as db:
                _insert(db, "chunks", late_chunk)

    store.iter_copy_rows(cases.NB, on_table=on_table)
    assert "ck-late" not in {row["id"] for row in seen["chunks"]}
    with repo._runtime.database.connect() as db:
        assert db.execute("SELECT 1 FROM chunks WHERE id = 'ck-late'").fetchone()
