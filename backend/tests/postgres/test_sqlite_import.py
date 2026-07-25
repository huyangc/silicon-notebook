from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values
from psycopg.rows import dict_row

from app.core.config import Settings
from app.migration.database_activation import activate_postgres_from_receipt
from app.migration.sqlite_to_postgres import (
    MigrationTuning,
    SqliteToPostgresMigrationError,
    migrate,
    preflight,
)
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from tests.postgres.conftest import ScopedPostgres


pytestmark = pytest.mark.postgres_integration
REPO_ROOT = Path(__file__).resolve().parents[3]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _create_source(path: Path) -> dict[str, int]:
    settings = Settings(database_url=f"sqlite:///{path}")
    database = SqliteDatabase(settings, REPO_ROOT)
    try:
        SqliteMigrator(database, settings).initialize()
        with database.write() as connection:
            connection.execute(
                "INSERT INTO notebooks "
                "(id,name,created_by,created_at,updated_at,expected_questions,"
                "source_types,taxonomy) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "nb-migration",
                    "迁移验证",
                    "user-local",
                    "2026-07-23T10:00:00+00:00",
                    "2026-07-23T10:00:00+00:00",
                    '["什么是迁移？"]',
                    '["markdown"]',
                    '["数据库"]',
                ),
            )
            connection.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "src-migration",
                    "nb-migration",
                    "source.md",
                    "markdown",
                    "2026-07-23 10:01:00",
                    "2026-07-23 10:01:00",
                ),
            )
            connection.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    "elem-migration",
                    "src-migration",
                    "paragraph",
                    "p1",
                    "中英 mixed text",
                    '{"page":1}',
                    "2026-07-23T10:02:00Z",
                ),
            )
            connection.execute(
                "INSERT INTO chunks "
                "(id,notebook_id,source_id,text,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "chunk-migration",
                    "nb-migration",
                    "src-migration",
                    "中英 mixed text",
                    '["elem-migration"]',
                    "2026-07-23T10:03:00+00:00",
                ),
            )
            connection.execute(
                "INSERT INTO chunk_embeddings(chunk_id,notebook_id,vector,created_at) "
                "VALUES (?,?,?,?)",
                (
                    "chunk-migration",
                    "nb-migration",
                    sqlite3.Binary(b"\x00\x00\x80?"),
                    "2026-07-23T10:04:00+00:00",
                ),
            )
            connection.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "ko-migration",
                    "nb-migration",
                    "Concept",
                    '{"name":"事务"}',
                    '[{"element_id":"elem-migration"}]',
                    "src-migration",
                    "2026-07-23T10:05:00+00:00",
                    "2026-07-23T10:05:00+00:00",
                ),
            )
        with database.connect() as connection:
            return {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in (
                    "users",
                    "notebooks",
                    "sources",
                    "source_elements",
                    "chunks",
                    "chunk_embeddings",
                    "knowledge_objects",
                )
            }
    finally:
        database.close()


def test_migrate_sqlite_snapshot_to_empty_postgres(
    tmp_path: Path, postgres_scope: ScopedPostgres
):
    source = tmp_path / "source.db"
    expected_counts = _create_source(source)
    before_hash = _sha256(source)
    before_mtime = source.stat().st_mtime_ns

    source_preflight, target_preflight = preflight(
        source_path=source,
        target_url=postgres_scope.url,
        root_dir=REPO_ROOT,
    )
    assert source_preflight.schema_version > 0
    assert target_preflight.prepared_version == 0

    result = migrate(
        source_path=source,
        target_url=postgres_scope.url,
        work_dir=tmp_path / "migration",
        root_dir=REPO_ROOT,
        batch_rows=2,
        progress=lambda _message: None,
    )

    assert _sha256(source) == before_hash
    assert source.stat().st_mtime_ns == before_mtime
    assert result.total_rows > 0
    receipt = json.loads(Path(result.receipt_path).read_text(encoding="utf-8"))
    assert receipt["target"]["redacted_url"] not in {postgres_scope.url, ""}
    assert "password" not in Path(result.receipt_path).read_text(encoding="utf-8")
    assert receipt["storage"]["copied"] is False

    with psycopg.connect(postgres_scope.url, row_factory=dict_row) as connection:
        for table, expected in expected_counts.items():
            actual = connection.execute(
                f'SELECT COUNT(*) AS value FROM "{table}"'
            ).fetchone()["value"]
            assert int(actual) == expected
        notebook = connection.execute(
            "SELECT expected_questions,taxonomy FROM notebooks WHERE id=%s",
            ("nb-migration",),
        ).fetchone()
        assert notebook["expected_questions"] == ["什么是迁移？"]
        assert notebook["taxonomy"] == ["数据库"]
        vector = connection.execute(
            "SELECT vector FROM chunk_embeddings WHERE chunk_id=%s",
            ("chunk-migration",),
        ).fetchone()["vector"]
        assert bytes(vector) == b"\x00\x00\x80?"
        source_rowid = sqlite3.connect(source).execute(
            "SELECT rowid FROM knowledge_objects WHERE id='ko-migration'"
        ).fetchone()[0]
        ordinal = connection.execute(
            "SELECT ordinal FROM knowledge_objects WHERE id=%s", ("ko-migration",)
        ).fetchone()["ordinal"]
        assert int(ordinal) == int(source_rowid)

    env_path = tmp_path / ".env"
    env_path.write_text(f"DATABASE_URL=sqlite:///{source}\n", encoding="utf-8")
    env_path.chmod(0o600)
    activation = activate_postgres_from_receipt(
        source_path=source,
        target_url=postgres_scope.url,
        receipt_path=Path(result.receipt_path),
        work_dir=tmp_path / "migration",
        env_path=env_path,
        root_dir=REPO_ROOT,
        batch_rows=2,
        progress=lambda _message: None,
    )
    assert activation.changed is True
    assert activation.verification.total_rows == result.total_rows
    assert "password" not in activation.active_database
    activated_env = dotenv_values(env_path, interpolate=False)
    assert activated_env["DATABASE_URL"] == postgres_scope.url
    assert activated_env["SHADOW_DATABASE_URL"] == f"sqlite:///{source}"

    with pytest.raises(SqliteToPostgresMigrationError, match="not empty"):
        preflight(
            source_path=source,
            target_url=postgres_scope.url,
            root_dir=REPO_ROOT,
        )


def _crash_after_copies(limit: int):
    """Progress callback that raises once ``limit`` tables have COPY-committed."""

    state = {"copies": 0}

    def progress(message: str) -> None:
        if message.startswith("COPY "):
            state["copies"] += 1
            if state["copies"] > limit:
                raise RuntimeError("simulated crash mid-import")

    return progress


def test_interrupted_import_resumes_from_checkpoint(
    tmp_path: Path, postgres_scope: ScopedPostgres
):
    source = tmp_path / "source.db"
    expected_counts = _create_source(source)
    work_dir = tmp_path / "migration"

    # A crash after a few tables leaves a partially loaded target plus an
    # unfinalized run header and per-table checkpoints.
    with pytest.raises(SqliteToPostgresMigrationError):
        migrate(
            source_path=source,
            target_url=postgres_scope.url,
            work_dir=work_dir,
            root_dir=REPO_ROOT,
            batch_rows=2,
            progress=_crash_after_copies(4),
        )

    with psycopg.connect(postgres_scope.url, row_factory=dict_row) as connection:
        run = connection.execute(
            "SELECT finalized FROM silicon_migration_progress"
        ).fetchone()
        assert run is not None and run["finalized"] is False
        checkpoints = connection.execute(
            "SELECT COUNT(*) AS n FROM silicon_migration_table_progress"
        ).fetchone()["n"]
        assert 0 < int(checkpoints) < len(expected_counts) + 60

    # The identical stopped source re-snapshots to the same sealed hash, so the
    # copier resumes: prior tables are skipped and the run finalizes.
    result = migrate(
        source_path=source,
        target_url=postgres_scope.url,
        work_dir=work_dir,
        root_dir=REPO_ROOT,
        batch_rows=2,
        progress=lambda _message: None,
        tuning=MigrationTuning(
            maintenance_work_mem="64MB", max_parallel_index_workers=0
        ),
    )
    assert result.total_rows > 0

    with psycopg.connect(postgres_scope.url, row_factory=dict_row) as connection:
        for table, expected in expected_counts.items():
            actual = connection.execute(
                f'SELECT COUNT(*) AS value FROM "{table}"'
            ).fetchone()["value"]
            assert int(actual) == expected
        assert (
            connection.execute(
                "SELECT finalized FROM silicon_migration_progress"
            ).fetchone()["finalized"]
            is True
        )

    # A full (non-fast) activation re-checksums every table against the receipt,
    # proving the resumed target is byte-correct, then switches the deployment.
    env_path = tmp_path / ".env"
    env_path.write_text(f"DATABASE_URL=sqlite:///{source}\n", encoding="utf-8")
    env_path.chmod(0o600)
    activation = activate_postgres_from_receipt(
        source_path=source,
        target_url=postgres_scope.url,
        receipt_path=Path(result.receipt_path),
        work_dir=work_dir,
        env_path=env_path,
        root_dir=REPO_ROOT,
        batch_rows=2,
        progress=lambda _message: None,
    )
    assert activation.changed is True
    assert activation.verification.total_rows == result.total_rows


def test_fast_activation_skips_the_redundant_target_reverify(
    tmp_path: Path, postgres_scope: ScopedPostgres
):
    source = tmp_path / "source.db"
    _create_source(source)
    work_dir = tmp_path / "migration"
    result = migrate(
        source_path=source,
        target_url=postgres_scope.url,
        work_dir=work_dir,
        root_dir=REPO_ROOT,
        batch_rows=2,
        progress=lambda _message: None,
    )

    env_path = tmp_path / ".env"
    env_path.write_text(f"DATABASE_URL=sqlite:///{source}\n", encoding="utf-8")
    env_path.chmod(0o600)
    messages: list[str] = []
    activation = activate_postgres_from_receipt(
        source_path=source,
        target_url=postgres_scope.url,
        receipt_path=Path(result.receipt_path),
        work_dir=work_dir,
        env_path=env_path,
        root_dir=REPO_ROOT,
        batch_rows=2,
        progress=messages.append,
        skip_target_reverify=True,
    )
    assert activation.changed is True
    assert activation.verification.total_rows == result.total_rows
    # The cutover anchor (source re-snapshot) still ran; the second full-table
    # PostgreSQL checksum pass was skipped and never emitted per-table lines.
    assert any("fast-activation" in message for message in messages)
    assert not any(message.startswith("ACTIVATION VERIFY ") for message in messages)


def test_checkpoint_from_a_different_snapshot_fails_closed(
    tmp_path: Path, postgres_scope: ScopedPostgres
):
    first = tmp_path / "first.db"
    _create_source(first)
    work_dir = tmp_path / "migration"
    with pytest.raises(SqliteToPostgresMigrationError):
        migrate(
            source_path=first,
            target_url=postgres_scope.url,
            work_dir=work_dir,
            root_dir=REPO_ROOT,
            batch_rows=2,
            progress=_crash_after_copies(4),
        )

    second = tmp_path / "second.db"
    _create_source(second)
    with sqlite3.connect(second) as connection:
        connection.execute(
            "INSERT INTO notebooks "
            "(id,name,created_by,created_at,updated_at,expected_questions,"
            "source_types,taxonomy) VALUES (?,?,?,?,?,?,?,?)",
            (
                "nb-divergent",
                "另一个源",
                "user-local",
                "2026-07-23T11:00:00+00:00",
                "2026-07-23T11:00:00+00:00",
                "[]",
                "[]",
                "[]",
            ),
        )

    with pytest.raises(
        SqliteToPostgresMigrationError, match="different SQLite snapshot"
    ):
        migrate(
            source_path=second,
            target_url=postgres_scope.url,
            work_dir=work_dir,
            root_dir=REPO_ROOT,
            batch_rows=2,
            progress=lambda _message: None,
        )


def test_fresh_import_into_a_nonempty_target_fails_closed(
    tmp_path: Path, postgres_scope: ScopedPostgres
):
    # Pins the empty-target guard that moved from inspect_target into the copier's
    # fresh-run branch: business rows with no checkpoint must fail closed, not
    # append. (Deleting the copier's `_assert_target_empty` calls makes this raise
    # a checksum-mismatch instead, so the `not empty` match keeps it a reverse
    # guard.)
    source = tmp_path / "source.db"
    _create_source(source)
    work_dir = tmp_path / "migration"
    migrate(
        source_path=source,
        target_url=postgres_scope.url,
        work_dir=work_dir,
        root_dir=REPO_ROOT,
        batch_rows=50,
        progress=lambda _message: None,
    )
    with psycopg.connect(postgres_scope.url, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE silicon_migration_table_progress, silicon_migration_progress"
        )
    with pytest.raises(SqliteToPostgresMigrationError, match="not empty"):
        migrate(
            source_path=source,
            target_url=postgres_scope.url,
            work_dir=work_dir,
            root_dir=REPO_ROOT,
            batch_rows=50,
            progress=lambda _message: None,
        )


def test_crash_during_a_table_copy_rolls_back_and_resumes(
    tmp_path: Path, postgres_scope: ScopedPostgres
):
    source = tmp_path / "source.db"
    expected_counts = _create_source(source)
    work_dir = tmp_path / "migration"

    def crash_verifying_notebooks(message: str) -> None:
        # Fires after COPY wrote the rows into the open transaction but before the
        # per-table verify/commit, so the partial table must roll back.
        if message.startswith("VERIFY ") and message.endswith(" notebooks"):
            raise RuntimeError("crash mid-copy")

    with pytest.raises(SqliteToPostgresMigrationError):
        migrate(
            source_path=source,
            target_url=postgres_scope.url,
            work_dir=work_dir,
            root_dir=REPO_ROOT,
            batch_rows=1,
            progress=crash_verifying_notebooks,
        )

    with psycopg.connect(postgres_scope.url, row_factory=dict_row) as connection:
        assert (
            int(connection.execute("SELECT COUNT(*) AS n FROM notebooks").fetchone()["n"])
            == 0
        )
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM silicon_migration_table_progress "
                    "WHERE table_name='notebooks'"
                ).fetchone()["n"]
            )
            == 0
        )

    result = migrate(
        source_path=source,
        target_url=postgres_scope.url,
        work_dir=work_dir,
        root_dir=REPO_ROOT,
        batch_rows=1,
        progress=lambda _message: None,
    )
    assert result.total_rows > 0
    with psycopg.connect(postgres_scope.url, row_factory=dict_row) as connection:
        for table, expected in expected_counts.items():
            actual = connection.execute(
                f'SELECT COUNT(*) AS value FROM "{table}"'
            ).fetchone()["value"]
            assert int(actual) == expected


def test_crash_during_index_rebuild_resumes_idempotently(
    tmp_path: Path, postgres_scope: ScopedPostgres
):
    source = tmp_path / "source.db"
    expected_counts = _create_source(source)
    work_dir = tmp_path / "migration"

    def crash_third_index(message: str) -> None:
        # Two indexes have committed by the third; resume must skip them via
        # to_regclass rather than fail on a duplicate CREATE INDEX.
        if message.startswith("INDEX 3/"):
            raise RuntimeError("crash mid-finalize")

    with pytest.raises(SqliteToPostgresMigrationError):
        migrate(
            source_path=source,
            target_url=postgres_scope.url,
            work_dir=work_dir,
            root_dir=REPO_ROOT,
            batch_rows=50,
            progress=crash_third_index,
        )
    with psycopg.connect(postgres_scope.url, row_factory=dict_row) as connection:
        assert (
            connection.execute(
                "SELECT finalized FROM silicon_migration_progress"
            ).fetchone()["finalized"]
            is False
        )

    result = migrate(
        source_path=source,
        target_url=postgres_scope.url,
        work_dir=work_dir,
        root_dir=REPO_ROOT,
        batch_rows=50,
        progress=lambda _message: None,
    )
    assert result.total_rows > 0
    with psycopg.connect(postgres_scope.url, row_factory=dict_row) as connection:
        assert (
            connection.execute(
                "SELECT finalized FROM silicon_migration_progress"
            ).fetchone()["finalized"]
            is True
        )
        for table, expected in expected_counts.items():
            actual = connection.execute(
                f'SELECT COUNT(*) AS value FROM "{table}"'
            ).fetchone()["value"]
            assert int(actual) == expected
