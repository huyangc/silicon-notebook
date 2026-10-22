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


def test_full_world_export_rows_equal_the_copy_snapshot_pg(repo, out_dir):
    cases.check_full_world_export_equals_snapshot(repo, BACKEND, out_dir)


def test_dirty_notebook_export_carries_no_clusters_pg(repo, out_dir):
    cases.check_dirty_notebook_carries_no_clusters(repo, BACKEND, out_dir)


def test_streaming_read_sees_one_snapshot_pg(repo):
    cases.check_stream_reads_one_snapshot(repo, BACKEND)


def test_stream_consumer_must_read_every_page_pg(repo, out_dir):
    cases.check_consumer_must_read_every_page(repo, BACKEND, out_dir)


def test_export_encodes_bytes_and_timestamps_portably_pg(repo, out_dir):
    cases.check_byte_columns_and_timestamps_are_portable(repo, BACKEND, out_dir)


def test_export_carries_files_assets_and_manifest_pg(repo, out_dir):
    cases.check_full_world_export(repo, BACKEND, out_dir)


def test_export_records_the_global_custom_object_types_it_uses_pg(repo, out_dir):
    cases.check_global_object_types(repo, BACKEND, out_dir)


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


def test_a_failing_consumer_leaves_no_server_cursor_on_the_connection_pg(repo, monkeypatch):
    """The page generator is closed while the store still holds its connection: when
    the consumer raises mid-table, the named ``copy_rows_*`` cursor is gone before the
    connection goes back to the pool, and the transaction the pool gets back is the
    store's own (nothing left for the next borrower to trip over)."""
    import contextlib

    cases.seed(repo, BACKEND, cases.char_cases.full_world(), repo.storage_dir)
    store = repo._runtime.sharing_store
    database = store.database
    real_connect = database.connect
    at_release: list[dict] = []

    @contextlib.contextmanager
    def watched_connect():
        with real_connect() as connection:
            try:
                yield connection
            finally:
                cursors = connection.execute(
                    "SELECT name FROM pg_cursors WHERE name LIKE 'copy_rows_%%'"
                ).fetchall()
                at_release.append({"cursors": [row["name"] for row in cursors]})

    class Boom(Exception):
        pass

    opened: list[str] = []

    def failing(table, pages):
        for _page in pages:
            if table == "knowledge_objects":
                opened.append(table)
                raise Boom(table)

    with monkeypatch.context() as patched:
        patched.setattr(store_module, "COPY_STREAM_PAGE_ROWS", 1)
        patched.setattr(database, "connect", watched_connect)
        with pytest.raises(Boom):
            store.iter_copy_rows(cases.NB, on_table=failing)
    assert opened == ["knowledge_objects"]
    assert at_release == [{"cursors": []}]
    with database.connect() as connection:
        assert connection.execute(
            "SELECT count(*) AS n FROM pg_cursors"
        ).fetchone()["n"] == 0
