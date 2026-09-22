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


def test_export_writes_the_target_watermark(seeded, export_settings, tmp_path):
    """Taken UNSCOPED on purpose: a ``--notebook`` export covers a subset and
    deliberately leaves the watermark alone (PR-3b), so the fixture's own
    scoped package is not the one that writes this row."""
    # The fixture's own export was SCOPED, so it must have left no watermark
    # row at all -- the contrast that makes the assertions below meaningful.
    with seeded["repo"]._connect() as db:
        assert db.execute(
            "SELECT 1 FROM sync_export_state WHERE target_env=%s", (TARGET_ENV,)
        ).fetchone() is None

    report = export_notebooks(
        export_settings,
        target_env=TARGET_ENV,
        out_dir=tmp_path / "watermark-out",
        notebook_ids=None,
        source_env=SOURCE_ENV,
    )

    with seeded["repo"]._connect() as db:
        row = db.execute(
            "SELECT * FROM sync_export_state WHERE target_env=%s", (TARGET_ENV,)
        ).fetchone()

    assert row is not None
    assert row["exported_through_seq"] == 0
    assert row["package_id"] == report.package_id
    assert row["exported_at"].tzinfo is not None
    # The capture gate is closed on a freshly migrated database, so this
    # watermark is explicitly NOT one an incremental window may resume from.
    assert row["captured"] is False
    # The snapshot text IS recorded either way -- it describes this export's
    # read window, which is a fact about this run, not a promise about the log.
    assert ":" in str(row["exported_snapshot"])


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


def test_current_snapshot_returns_this_transactions_snapshot(seeded):
    """``_Source.current_snapshot`` 在导出自己的读事务里取回 PG 原生的
    ``pg_snapshot`` 文本(``xmin:xmax:xip``),而且它与 ``snapshot_xmin`` 的
    解析是一对:解析出的 xmin 必须与 PG 自己的 ``pg_snapshot_xmin`` 相等,
    并且不大于 xmax——增量导出的补偿窗口就以这个下界做 ``txid >= xmin`` 的
    范围扫描(§7)。"""
    import re

    from app.migration.sync.export import _Source

    source = _Source(seeded["settings"], Path(__file__).resolve().parents[3])
    try:
        with source.read() as conn:
            snapshot = source.current_snapshot(conn)
            native_xmin = conn.execute(
                "SELECT pg_snapshot_xmin(%s::pg_snapshot) AS xmin", (snapshot,)
            ).fetchone()["xmin"]
            native_xmax = conn.execute(
                "SELECT pg_snapshot_xmax(%s::pg_snapshot) AS xmax", (snapshot,)
            ).fetchone()["xmax"]
    finally:
        source.close()

    assert isinstance(snapshot, str)
    # re.ASCII on purpose, same as snapshot_xmin's own check: \d without it
    # also matches every non-ASCII decimal script, which would make this
    # shape assertion weaker than the parser it is meant to describe.
    assert re.fullmatch(r"\d+:\d+:(?:\d+(?:,\d+)*)?", snapshot, re.ASCII), snapshot
    parsed = _Source.snapshot_xmin(snapshot)
    assert parsed == int(native_xmin)
    assert parsed <= int(native_xmax)


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


# --------------------------------- knowledge_object_sources/community_members


def test_two_no_sqlite_pk_tables_keyset_page_correctly_on_postgres(
    repository, export_settings, tmp_path, monkeypatch
):
    """knowledge_object_sources and community_members get a REAL PostgreSQL
    PRIMARY KEY from 0064 (unlike the SQLite lane, which cannot add one to an
    existing table in place and backs their ``TableSyncSpec.key`` with a
    UNIQUE index instead -- see tests/test_sync_export.py). Confirm the
    ordinary catalog-primary-key keyset path (``_scan``) pages them
    correctly now that PR-3a removed their old one-notebook-at-a-time
    streaming branch. Shrinks the page instead of seeding thousands of rows,
    the same trick tests/test_sync_export.py's
    ``test_keyset_paging_covers_every_row_exactly_once`` uses: what needs
    proving is that the ``(pk...) > (last pk...)`` cursor neither skips a row
    at a page boundary nor repeats one, not that PostgreSQL can hold 1000+
    rows."""
    from app.migration.sync import database as database_module

    notebook = _seed(repository, "paged")
    with repository._write() as db:
        source_id = db.execute(
            "SELECT id FROM sources WHERE notebook_id=%s LIMIT 1", (notebook,)
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,"
            "owner,payload,evidence,source_id,created_at,updated_at,"
            "last_reviewed) VALUES(%s,%s,'concept','approved','','{}','[]','',"
            "now(),now(),NULL)",
            ("ko-paged", notebook),
        )
        for index in range(25):
            db.execute(
                "INSERT INTO knowledge_object_sources(object_id, source_id, "
                "notebook_id) VALUES (%s, %s, %s)",
                ("ko-paged", f"{source_id}-page-{index:03d}", notebook),
            )
            db.execute(
                "INSERT INTO community_members(canonical_id, notebook_id, "
                "level, community_id, canonical_name, centrality) "
                "VALUES (%s, %s, 0, %s, %s, 0.0)",
                (f"can-page-{index:03d}", notebook, f"comm-page-{index:03d}", "n"),
            )

    monkeypatch.setattr(database_module, "_PAGE_ROWS", 4)
    report = export_notebooks(
        export_settings, target_env=TARGET_ENV, out_dir=tmp_path / "paged",
        notebook_ids=[notebook], source_env=SOURCE_ENV,
    )

    for table, key_of in (
        ("knowledge_object_sources", lambda row: (row["object_id"], row["source_id"])),
        ("community_members", lambda row: (row["community_id"], row["canonical_id"])),
    ):
        rows = _rows(report.package_dir, table)
        keys = [key_of(row) for row in rows]
        assert len(keys) == 25 > 4, "the page size must be smaller than the row count"
        assert len(set(keys)) == len(keys), (
            f"{table}: a row was written twice across a page boundary"
        )
        assert keys == sorted(keys), f"{table}: keyset pages must come out in key order"
        assert report.table_counts[table] == 25


# ---------------------------------------------------------- capture watermark


def test_captured_through_seq_reads_the_change_log_high_water_mark_on_postgres(
    repository, export_settings, tmp_path
):
    """SQLite lane (tests/test_sync_export.py) already covers the read
    itself; this only pins that the PostgreSQL gate (a real ``boolean``
    column, not SQLite's 0/1 INTEGER) and its ``bigint GENERATED ... AS
    IDENTITY`` seq work the same way through ``_Source``."""
    notebook = _seed(repository, "capture-watermark")
    with repository._write() as db:
        db.execute(
            "INSERT INTO sync_capture_control (singleton, enabled, enabled_at) "
            "VALUES (1, true, now())"
        )
        for _ in range(5):
            db.execute(
                "INSERT INTO sync_change_log "
                "(table_name, key_json, operation, changed_at) "
                "VALUES ('notebooks', '{}'::jsonb, 'upsert', now())"
            )

    report = export_notebooks(
        export_settings,
        target_env=TARGET_ENV,
        out_dir=tmp_path / "out",
        notebook_ids=None,
        source_env=SOURCE_ENV,
    )

    assert report.captured_through_seq == 5
    with repository._connect() as db:
        row = db.execute(
            "SELECT exported_through_seq, captured FROM sync_export_state "
            "WHERE target_env=%s",
            (TARGET_ENV,),
        ).fetchone()
    assert row["exported_through_seq"] == 5
    assert row["captured"] is True

    # ...and a SCOPED export taken afterwards leaves that row exactly as it
    # is: its coverage is a chosen subset, so its watermark would hide every
    # other notebook's changes below the next window's floor.
    with repository._write() as db:
        db.execute(
            "INSERT INTO sync_change_log "
            "(table_name, key_json, operation, changed_at) "
            "VALUES ('notebooks', '{}'::jsonb, 'upsert', now())"
        )
    export_notebooks(
        export_settings,
        target_env=TARGET_ENV,
        out_dir=tmp_path / "out",
        notebook_ids=[notebook],
        source_env=SOURCE_ENV,
    )
    with repository._connect() as db:
        after = db.execute(
            "SELECT exported_through_seq FROM sync_export_state WHERE target_env=%s",
            (TARGET_ENV,),
        ).fetchone()
    assert after["exported_through_seq"] == 5


# ------------------------------------------- unique-surface probe (PG shapes)


@pytest.fixture
def probe_source(seeded):
    """A ``_Source`` on the seeded scope, for calling the catalog probes
    directly. The unique-surface rules are pure catalog reads, so they do not
    need a whole export run to exercise -- but they DO need PostgreSQL, since
    three of the four shapes below (INCLUDE payload columns, expression key
    columns, an index left NOT VALID) have no SQLite equivalent."""
    from app.migration.sync.export import _Source

    source = _Source(seeded["settings"], Path(__file__).resolve().parents[3])
    try:
        yield source
    finally:
        source.close()


def test_has_unique_surface_ignores_include_payload_columns(seeded, probe_source):
    """``UNIQUE (notebook_id) INCLUDE (chunk_id)`` enforces uniqueness of
    notebook_id ALONE. The INCLUDE column rides along in the index for
    covering reads and is not part of the constraint at all, so this index
    must not answer for the pair -- reading ``indkey`` without honouring
    ``indnkeyatts`` is exactly how it would."""
    with seeded["repo"]._write() as db:
        db.execute(
            "CREATE UNIQUE INDEX uq_probe_include ON chunk_questions "
            "(notebook_id) INCLUDE (chunk_id)"
        )

    with probe_source.read() as conn:
        assert (
            probe_source._has_unique_surface(
                conn, "chunk_questions", ("notebook_id", "chunk_id")
            )
            is False
        )
        # Positive control: it DOES answer for its real key column.
        assert (
            probe_source._has_unique_surface(conn, "chunk_questions", ("notebook_id",))
            is True
        )


def test_has_unique_surface_rejects_an_expression_key_column(seeded, probe_source):
    """``UNIQUE (notebook_id, lower(chunk_id))`` guarantees the LOWERCASED
    chunk_id is unique within a notebook, which is not the same statement as
    chunk_id being unique. ``indkey`` carries a 0 for the expression column;
    the whole index has to be discarded, not just that position -- keeping
    the rest would leave ``{notebook_id}`` looking like a unique surface it
    is not."""
    with seeded["repo"]._write() as db:
        db.execute(
            "CREATE UNIQUE INDEX uq_probe_expression ON chunk_questions "
            "(notebook_id, lower(chunk_id))"
        )

    with probe_source.read() as conn:
        assert (
            probe_source._has_unique_surface(
                conn, "chunk_questions", ("notebook_id", "chunk_id")
            )
            is False
        )
        assert (
            probe_source._has_unique_surface(conn, "chunk_questions", ("notebook_id",))
            is False
        )


def test_has_unique_surface_rejects_an_invalid_index(seeded, probe_source):
    """An index left behind by a failed ``CREATE INDEX CONCURRENTLY`` is
    flagged unique in the catalog but is neither used by the planner nor
    enforced by the executor. ``indisvalid``/``indisready`` are what tell the
    two apart. (Forged here by clearing the flag directly: making a real
    CONCURRENTLY build fail mid-flight is not something a test can arrange
    deterministically. That catalog write needs superuser, which both the
    local lane and CI's ``postgres:16`` service run as; anywhere else this
    skips rather than failing for an unrelated reason.)"""
    with seeded["repo"]._connect() as db:
        if not db.execute(
            "SELECT usesuper FROM pg_user WHERE usename = current_user"
        ).fetchone()["usesuper"]:
            pytest.skip("forging an invalid index needs a superuser connection")

    with seeded["repo"]._write() as db:
        db.execute(
            "CREATE UNIQUE INDEX uq_probe_invalid ON chunk_questions "
            "(notebook_id, chunk_id)"
        )

    with probe_source.read() as conn:
        assert (
            probe_source._has_unique_surface(
                conn, "chunk_questions", ("notebook_id", "chunk_id")
            )
            is True
        ), "positive control: a healthy index of the same shape is accepted"

    with seeded["repo"]._write() as db:
        db.execute(
            "UPDATE pg_index SET indisvalid = false "
            "WHERE indexrelid = 'uq_probe_invalid'::regclass"
        )

    with probe_source.read() as conn:
        assert (
            probe_source._has_unique_surface(
                conn, "chunk_questions", ("notebook_id", "chunk_id")
            )
            is False
        )


def test_has_unique_surface_rejects_a_partial_index(seeded, probe_source):
    """Same rule as the SQLite lane's, restated on this backend because the
    catalog column is a different one (``indpred``, not ``PRAGMA
    index_list``'s ``partial``)."""
    with seeded["repo"]._write() as db:
        db.execute(
            "CREATE UNIQUE INDEX uq_probe_partial ON chunk_questions "
            "(notebook_id, chunk_id) WHERE source_id != ''"
        )

    with probe_source.read() as conn:
        assert (
            probe_source._has_unique_surface(
                conn, "chunk_questions", ("notebook_id", "chunk_id")
            )
            is False
        )
