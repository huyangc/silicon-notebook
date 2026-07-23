from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from app.core.config import Settings
from app.migration.sqlite_to_postgres import (
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

    with pytest.raises(SqliteToPostgresMigrationError, match="not empty"):
        preflight(
            source_path=source,
            target_url=postgres_scope.url,
            root_dir=REPO_ROOT,
        )

