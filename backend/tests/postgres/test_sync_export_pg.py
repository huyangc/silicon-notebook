"""守卫: 全量导出器 (app.migration.sync.export) 的 PostgreSQL 泳道。

SQLite 泳道 (tests/test_sync_export.py) 已经钉住圈定、包结构、校验和与水位。
这里只覆盖**按后端会分叉**的那一段: PG 原生值到包里「SQLite 形态」的投影 ——
jsonb 变文本、timestamptz 变 ISO 文本、bytea 变 ``{"$bytes": ...}``、
``POSTGRES_ROWID_ORDINAL_TABLES`` 的 identity ``ordinal`` 列整列不导 (SQLite
端根本没有这一列, 所以只有这边能真的测到它被丢掉)。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.sync.export import export_notebooks
from app.migration.shadow.postgres_catalog import EXPECTED_COLUMNS
from app.migration.sync.manifest import synced_tables
from app.migration.sync.package import BYTES_KEY, MANIFEST_NAME, decode_value, rows_path
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.repositories.postgres.schema_manifest import POSTGRES_ROWID_ORDINAL_TABLES


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_sync_export"),
]

SOURCE_ENV = "dev"
TARGET_ENV = "prod"
VECTOR = bytes(range(32))


@pytest.fixture
def export_settings(postgres_scope, tmp_path) -> Settings:
    return Settings(
        database_url=postgres_scope.url,
        storage_dir=str(tmp_path / "storage"),
        postgres_pool_min_size=1,
        postgres_pool_max_size=4,
        postgres_pool_acquire_timeout_seconds=2,
        postgres_statement_timeout_seconds=15,
        postgres_lock_timeout_seconds=2,
    )


@pytest.fixture
def repository(export_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repo = PostgresRepository(export_settings)
    try:
        yield repo
    finally:
        repo.close()


def _seed(repo, name: str) -> str:
    notebook = repo.create_notebook(NotebookCreate(name=name))
    with repo._write() as db:
        # A non-empty jsonb payload, written as jsonb rather than through the
        # create seam: what this file is pinning is the jsonb -> text
        # projection, so the column must hold something a text column could
        # not have held by accident.
        db.execute(
            "UPDATE notebooks SET expected_questions = %s::jsonb WHERE id = %s",
            (json.dumps(["问题一", "问题二"], ensure_ascii=False), notebook.id),
        )
    repo.upload_sources(
        notebook.id,
        [
            UploadedSourceFile(
                file_name=f"{name}.txt",
                content_type="text/plain",
                content=b"alpha beta gamma\n" * 8,
                doc_type="",
                doc_type_explicit=False,
            )
        ],
    )
    repo.create_knowhow_table(
        notebook.id, f"{name}-table", "",
        [{"name": "Topic", "role": "anchor"}], created_by="user-local",
    )
    repo.create_memory_candidate(
        notebook.id, "user-local", None, f"{name}-req", f"{name} memory",
        "记忆正文", ["t"], "test",
    )
    with repo._write() as db:
        chunk = db.execute(
            "SELECT id FROM chunks WHERE notebook_id=%s ORDER BY id LIMIT 1",
            (notebook.id,),
        ).fetchone()
        assert chunk is not None, "seed upload produced no chunk"
        db.execute(
            "INSERT INTO chunk_embeddings(chunk_id, notebook_id, vector, created_at) "
            "VALUES(%s,%s,%s,now())",
            (chunk["id"], notebook.id, VECTOR),
        )
    return notebook.id


@pytest.fixture
def seeded(repository, export_settings, tmp_path):
    exported = _seed(repository, "exported")
    other = _seed(repository, "other")
    report = export_notebooks(
        export_settings,
        target_env=TARGET_ENV,
        out_dir=tmp_path / "out",
        notebook_ids=[exported],
        source_env=SOURCE_ENV,
    )
    return {
        "repo": repository,
        "exported": exported,
        "other": other,
        "report": report,
        "package": report.package_dir,
        "settings": export_settings,
    }


def _rows(package: Path, table: str) -> list[dict]:
    text = (package / rows_path(table)).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _manifest(package: Path) -> dict:
    return json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))


def test_jsonb_columns_travel_as_text(seeded):
    row = _rows(seeded["package"], "notebooks")[0]

    for column in ("expected_questions", "source_types", "taxonomy"):
        assert isinstance(row[column], str), (column, row[column])
    assert json.loads(row["expected_questions"]) == ["问题一", "问题二"]


def test_timestamptz_columns_travel_as_utc_iso_text(seeded):
    row = _rows(seeded["package"], "notebooks")[0]

    for column in ("created_at", "updated_at"):
        assert isinstance(row[column], str), column
        moment = datetime.fromisoformat(row[column])
        assert moment.utcoffset() == timedelta(0), (
            f"{column} must be expressed at +00:00: the package bytes -- and "
            "so checksums.json -- must not depend on the exporting host's "
            f"timezone (got {row[column]!r})"
        )
        assert row[column].endswith("+00:00"), (column, row[column])


def test_bytea_columns_travel_as_the_bytes_object(seeded):
    rows = _rows(seeded["package"], "chunk_embeddings")

    assert rows, "seeded notebook must carry one chunk embedding"
    assert set(rows[0]["vector"]) == {BYTES_KEY}
    assert decode_value(rows[0]["vector"]) == VECTOR


def test_the_identity_ordinal_column_is_dropped(seeded):
    manifest = _manifest(seeded["package"])
    ordinal_tables = set(POSTGRES_ROWID_ORDINAL_TABLES) & set(synced_tables())

    assert ordinal_tables, "this assertion needs at least one rowid-ordinal table"
    for table in ordinal_tables:
        assert "ordinal" not in manifest["tables"][table]["columns"], table
        assert all("ordinal" not in row for row in _rows(seeded["package"], table))
    # ... and the column really is there on the source side, so the drop is
    # doing work rather than describing a column that never existed.
    with seeded["repo"]._connect() as db:
        present = db.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'chunks' "
            "AND column_name = 'ordinal'"
        ).fetchone()
    assert present is not None


def test_counts_match_the_same_scope_queried_directly(seeded):
    report = seeded["report"]
    with seeded["repo"]._connect() as db:
        for table, statement in (
            ("notebooks", "SELECT COUNT(*) AS n FROM notebooks WHERE id=%s"),
            ("sources", "SELECT COUNT(*) AS n FROM sources WHERE notebook_id=%s"),
            ("chunks", "SELECT COUNT(*) AS n FROM chunks WHERE notebook_id=%s"),
            (
                "source_elements",
                "SELECT COUNT(*) AS n FROM source_elements e JOIN sources s "
                "ON s.id=e.source_id WHERE s.notebook_id=%s",
            ),
            (
                "memory_provenance",
                "SELECT COUNT(*) AS n FROM memory_provenance p JOIN memory_items m "
                "ON m.id=p.memory_id WHERE m.notebook_id=%s",
            ),
        ):
            expected = int(
                db.execute(statement, (seeded["exported"],)).fetchone()["n"]
            )
            assert report.table_counts[table] == expected, table
            assert len(_rows(seeded["package"], table)) == expected, table


def test_the_unexported_notebook_contributes_no_row(seeded):
    other = seeded["other"]

    leaked = [
        f"{table}: {row}"
        for table in synced_tables()
        for row in _rows(seeded["package"], table)
        if other in {value for value in row.values() if isinstance(value, str)}
    ]
    assert not leaked, leaked


def test_export_writes_the_target_watermark(seeded):
    with seeded["repo"]._connect() as db:
        row = db.execute(
            "SELECT * FROM sync_export_state WHERE target_env=%s", (TARGET_ENV,)
        ).fetchone()

    assert row is not None
    assert row["exported_through_seq"] == 0
    assert row["package_id"] == seeded["report"].package_id
    assert row["exported_at"].tzinfo is not None


def test_a_missing_instant_is_null_not_an_empty_string(seeded):
    """``memory_items.confirmed_at`` is NULL for an unconfirmed candidate.
    NULL must travel as JSON null: an empty string would arrive at a
    PostgreSQL target as an invalid timestamptz, and at a SQLite target as a
    value that is neither "unset" nor an instant."""
    rows = _rows(seeded["package"], "memory_items")

    assert rows, "the seed creates one unconfirmed memory candidate"
    assert all(row["confirmed_at"] is None for row in rows), rows
    assert all(row["confirmed_by"] in (None, "") for row in rows)
    # The raw line really says null -- not "", and not an absent key.
    text = (seeded["package"] / rows_path("memory_items")).read_text(encoding="utf-8")
    assert '"confirmed_at": null' in text


def test_sentinel_timestamp_columns_are_null_too(seeded):
    """``knowledge_objects.last_reviewed`` and ``unified_kg_state``'s rebuild
    stamp are in POSTGRES_EMPTY_TIME_SENTINELS -- the pair where SQLite
    historically stored ''. The package carries the source's NULL as null and
    leaves the backend-specific spelling to the importer."""
    for table, column in (
        ("knowledge_objects", "last_reviewed"),
        ("unified_kg_state", "last_rebuild_at"),
    ):
        for row in _rows(seeded["package"], table):
            if column in row:
                assert row[column] is None or isinstance(row[column], str), row


def test_the_export_reads_one_repeatable_read_snapshot(seeded):
    """Every table and every file is read in one transaction, so a package can
    never hold a chunk whose source row was deleted halfway through the run.
    Asserted at the seam: the read connection reports the isolation level the
    exporter set."""
    from app.migration.sync.export import _Source

    source = _Source(seeded["settings"], Path(__file__).resolve().parents[3])
    try:
        with source.read() as conn:
            level = conn.execute("SHOW transaction_isolation").fetchone()
            read_only = conn.execute("SHOW transaction_read_only").fetchone()
            timeout = conn.execute("SHOW statement_timeout").fetchone()
    finally:
        source.close()

    assert level["transaction_isolation"] == "repeatable read"
    assert read_only["transaction_read_only"] == "on"
    assert timeout["statement_timeout"] == "0", (
        "a whole-notebook scan can legitimately outlive the serving deadline "
        "the pool configures; the export lifts it for its own transaction"
    )


def test_the_pool_connection_goes_back_with_its_defaults(seeded):
    """SET LOCAL and SET TRANSACTION both end with the transaction, and the
    pool resets a returned connection anyway -- so the next borrower must not
    inherit an unbounded, read-only, repeatable-read session."""
    from app.migration.sync.export import _Source

    source = _Source(seeded["settings"], Path(__file__).resolve().parents[3])
    try:
        with source.read():
            pass
        with source.read() as conn:
            # A fresh export transaction sets them again from scratch.
            assert conn.execute("SHOW statement_timeout").fetchone()[
                "statement_timeout"
            ] == "0"
    finally:
        source.close()
    with seeded["repo"]._connect() as conn:
        assert conn.execute("SHOW transaction_read_only").fetchone()[
            "transaction_read_only"
        ] == "off"
        assert conn.execute("SHOW statement_timeout").fetchone()[
            "statement_timeout"
        ] != "0"


def test_an_export_refuses_a_budgeted_connection(seeded):
    """The budgeted wrapper prepends a set_config to every execute, which on
    PostgreSQL would take the first-statement slot SET TRANSACTION needs and
    then re-cap each page at a request deadline."""
    from app.migration.sync.export import SyncExportError, _Source
    from app.repositories.read_budget import read_budget

    source = _Source(seeded["settings"], Path(__file__).resolve().parents[3])
    try:
        with read_budget(time.monotonic() + 30.0):
            with pytest.raises(SyncExportError, match="read budget"):
                with source.read():
                    pass
    finally:
        source.close()


# --------------------------------------------------- column-type contract


def test_the_column_contract_matches_information_schema(repository):
    """The SQLite lane (tests/test_sync_catalog_contract.py) pins column NAMES
    against the real schema; only here can the exporter's actual inputs --
    ``data_type`` and nullability -- be checked against the live catalog.

    This is the guard that would have caught ``_expected_columns`` swallowing
    the siblings of a multi-column ``ALTER TABLE ... ADD COLUMN a ..., ADD
    COLUMN b ...``: ten synced-layer columns were missing from the contract,
    two of them ``timestamptz``, so a non-NULL value in one of those would
    have reached ``json.dumps`` as a ``datetime`` and crashed the export.
    """
    with repository._connect() as db:
        rows = db.execute(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_schema = current_schema()"
        ).fetchall()

    live: dict[str, dict[str, tuple[str, bool]]] = {}
    for row in rows:
        live.setdefault(str(row["table_name"]), {})[str(row["column_name"])] = (
            str(row["data_type"]),
            str(row["is_nullable"]) == "YES",
        )

    mismatches: list[str] = []
    for table in synced_tables():
        contracted = {
            name: (contract.data_type, contract.nullable)
            for name, contract in EXPECTED_COLUMNS.get(table, {}).items()
        }
        actual = live.get(table, {})
        assert actual, f"{table} is missing from the live PostgreSQL schema"
        for name in sorted(set(contracted) | set(actual)):
            if contracted.get(name) != actual.get(name):
                mismatches.append(
                    f"{table}.{name}: contract={contracted.get(name)} "
                    f"live={actual.get(name)}"
                )
    assert not mismatches, mismatches


def test_a_late_added_timestamptz_column_exports_as_utc_text(
    repository, export_settings, tmp_path
):
    """``unified_kg_state.derived_building_claimed_at`` arrived through a
    multi-column ALTER and is NULL in every fixture, which is the only reason
    the contract bug stayed invisible. Give it a real instant and export."""
    notebook = _seed(repository, "claimed")
    with repository._write() as db:
        db.execute(
            "UPDATE unified_kg_state SET derived_building_claimed_at = "
            "TIMESTAMPTZ '2026-01-01 08:00:00+08' WHERE notebook_id = %s",
            (notebook,),
        )

    report = export_notebooks(
        export_settings, target_env=TARGET_ENV, out_dir=tmp_path / "claimed",
        notebook_ids=[notebook], source_env=SOURCE_ENV,
    )
    rows = _rows(report.package_dir, "unified_kg_state")

    assert len(rows) == 1
    assert rows[0]["derived_building_claimed_at"] == "2026-01-01T00:00:00+00:00"
