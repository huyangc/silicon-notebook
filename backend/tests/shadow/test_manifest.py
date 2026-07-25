from __future__ import annotations

import json
import os
import sqlite3
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.shadow.manifest import (
    COLUMN_TRANSFORMS,
    MANIFEST,
    RUNNING_SCHEMA_PAIR,
    manifest_contract,
    validate_manifest,
    validate_postgres_connection,
    validate_postgres_schema,
    validate_sqlite_schema,
)
from app.migration.shadow.types import (
    Manifest,
    ReplicationKeyKind,
    SchemaPair,
    TableClass,
    TableSpec,
)
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = (
    REPO_ROOT / "backend" / "tests" / "fixtures" / "shadow_manifest_contract.json"
)
POSTGRES_SCHEMA_CONTRACT_PATH = (
    REPO_ROOT / "backend" / "tests" / "fixtures" / "postgres_schema_contract.json"
)
EXPECTED_FTS_ROOTS = {"chunks_fts", "kg_objects_fts", "memory_items_fts"}
EXPECTED_SHADOW_KEYS = {
    "community_members": ("notebook_id", "level", "canonical_id"),
    "kg_canonical_scratch": ("seed", "notebook_id", "run_id"),
    "kg_cluster_scratch": ("object_id", "notebook_id", "run_id"),
    "knowledge_object_sources": ("object_id", "source_id"),
}


@pytest.fixture
def fresh_sqlite(tmp_path: Path):
    path = tmp_path / "fresh-v32.db"
    settings = Settings(database_url=f"sqlite:///{path}")
    database = SqliteDatabase(settings, REPO_ROOT)
    try:
        assert SqliteMigrator(database, settings).migrate() == list(
            range(1, SCHEMA_VERSION + 1)
        )
        with database.connect() as conn:
            yield conn
    finally:
        database.close_local()


def _reviewed_postgres_contract() -> dict[str, object]:
    return json.loads(POSTGRES_SCHEMA_CONTRACT_PATH.read_text(encoding="utf-8"))


def test_manifest_matches_reviewed_fixture_and_current_running_pair():
    expected = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert manifest_contract(MANIFEST) == expected
    assert RUNNING_SCHEMA_PAIR == SchemaPair(
        sqlite_version=32, postgres_version=10, epoch=1
    )
    # The plan's old (24, 2) COPY staging pair predates five current tables and
    # is deliberately not advertised as compatible. SQLite v31 added capture
    # state and v32 adds the report understanding contract.
    assert RUNNING_SCHEMA_PAIR != SchemaPair(
        sqlite_version=24, postgres_version=2, epoch=1
    )


def test_manifest_is_total_for_fresh_sqlite_v32(fresh_sqlite):
    report = validate_sqlite_schema(
        fresh_sqlite,
        MANIFEST,
        expected_pair=RUNNING_SCHEMA_PAIR,
    )
    assert report.ordinary_tables == frozenset(MANIFEST.replicated_names)
    assert report.rebuilt_roots == frozenset(EXPECTED_FTS_ROOTS)
    assert report.schema_version == 32


def test_manifest_keys_ranks_transforms_blobs_and_paths_are_reviewed(fresh_sqlite):
    validate_manifest(MANIFEST)
    specs = {spec.name: spec for spec in MANIFEST.replicated}
    assert len(specs) == 60
    assert len({spec.copy_rank for spec in specs.values()}) == 60
    assert {
        name: spec.replication_key
        for name, spec in specs.items()
        if spec.key_kind is ReplicationKeyKind.SHADOW_UNIQUE
    } == EXPECTED_SHADOW_KEYS
    assert all(
        specs[name].table_class is TableClass.REPLICATED
        for name in EXPECTED_SHADOW_KEYS
    )
    assert specs["sources"].path_columns == ("file_path",)
    assert {
        (spec.name, column) for spec in specs.values() for column in spec.blob_columns
    } == {
        ("chunk_embeddings", "vector"),
        ("element_embeddings", "vector"),
        ("knowledge_embeddings", "vector"),
        ("memory_embeddings", "vector"),
        ("relation_embeddings", "vector"),
    }
    assert {"identity", "timestamptz", "jsonb", "boolean", "bytea"} <= set(
        COLUMN_TRANSFORMS
    )


def test_transform_pipelines_cover_every_typed_postgres_column():
    tables = _reviewed_postgres_contract()["tables"]
    specs = {spec.name: spec for spec in MANIFEST.replicated}
    type_name = {
        "bytea": "bytea",
        "jsonb": "jsonb",
        "timestamp with time zone": "timestamptz",
        "boolean": "boolean",
    }
    transform_order = ("bytea", "jsonb", "timestamptz", "boolean")
    for table, facts in tables.items():
        selected = {
            type_name[column["postgres_type"]]
            for column in facts["columns"]
            if column["postgres_type"] in type_name
        }
        expected = "+".join(name for name in transform_order if name in selected)
        assert specs[table].sqlite_to_postgres == (expected or "identity"), table
        assert specs[table].postgres_to_sqlite == (expected or "identity"), table


def test_declared_primary_keys_and_fk_copy_order_match_current_contract():
    contract = _reviewed_postgres_contract()
    tables = contract["tables"]
    specs = {spec.name: spec for spec in MANIFEST.replicated}
    assert set(specs) == set(tables)
    for name, facts in tables.items():
        spec = specs[name]
        if spec.key_kind is ReplicationKeyKind.DECLARED_PK:
            assert list(spec.replication_key) == facts["primary_key"], name
        for foreign_key in facts["foreign_keys"]:
            parent = foreign_key["references_table"]
            assert specs[parent].copy_rank < spec.copy_rank, (parent, name)


def test_postgres_contract_reverse_totality_without_a_live_server():
    contract = _reviewed_postgres_contract()
    report = validate_postgres_schema(
        tables=contract["tables"],
        schema_version=int(contract["postgres_version"]),
        manifest=MANIFEST,
        expected_pair=RUNNING_SCHEMA_PAIR,
    )
    assert report.ordinary_tables == frozenset(MANIFEST.replicated_names)
    assert report.schema_version == 10


@pytest.mark.parametrize("failure", ["missing", "nullable"])
def test_postgres_contract_rejects_missing_or_nullable_replication_key(failure):
    contract = _reviewed_postgres_contract()
    tables = deepcopy(contract["tables"])
    columns = tables["users"]["columns"]
    if failure == "missing":
        tables["users"]["columns"] = [
            column for column in columns if column["name"] != "id"
        ]
        message = "missing PostgreSQL replication key"
    else:
        next(column for column in columns if column["name"] == "id")["nullable"] = True
        message = "nullable PostgreSQL replication key"
    with pytest.raises(ValueError, match=message):
        validate_postgres_schema(
            tables=tables,
            schema_version=10,
            manifest=MANIFEST,
            expected_pair=RUNNING_SCHEMA_PAIR,
        )


def test_sqlite_reverse_totality_rejects_an_unknown_table(fresh_sqlite):
    fresh_sqlite.execute("CREATE TABLE unreviewed_business_table(id TEXT PRIMARY KEY)")
    with pytest.raises(ValueError, match="unknown=.*unreviewed_business_table"):
        validate_sqlite_schema(
            fresh_sqlite, MANIFEST, expected_pair=RUNNING_SCHEMA_PAIR
        )


def test_sqlite_fts_prefix_does_not_hide_an_unknown_ordinary_table(fresh_sqlite):
    fresh_sqlite.execute(
        "CREATE TABLE chunks_fts_unreviewed_business(id TEXT PRIMARY KEY)"
    )
    with pytest.raises(ValueError, match="unknown=.*chunks_fts_unreviewed_business"):
        validate_sqlite_schema(
            fresh_sqlite, MANIFEST, expected_pair=RUNNING_SCHEMA_PAIR
        )


def test_sqlite_shadow_prefix_does_not_hide_an_unknown_ordinary_table(fresh_sqlite):
    fresh_sqlite.execute("CREATE TABLE shadow_unreviewed_business(id TEXT PRIMARY KEY)")
    with pytest.raises(ValueError, match="unknown=.*shadow_unreviewed_business"):
        validate_sqlite_schema(
            fresh_sqlite, MANIFEST, expected_pair=RUNNING_SCHEMA_PAIR
        )


def test_sqlite_shadow_internal_tables_require_explicit_manifest_classification(
    fresh_sqlite,
):
    fresh_sqlite.execute(
        "CREATE TABLE shadow_explicit_control(singleton INTEGER PRIMARY KEY)"
    )
    internal = TableSpec(
        name="shadow_explicit_control",
        table_class=TableClass.SHADOW_INTERNAL,
        replication_key=(),
        key_kind=ReplicationKeyKind.DECLARED_PK,
        copy_rank=66,
    )
    manifest = Manifest(RUNNING_SCHEMA_PAIR, (*MANIFEST.tables, internal))
    report = validate_sqlite_schema(
        fresh_sqlite, manifest, expected_pair=RUNNING_SCHEMA_PAIR
    )
    assert "shadow_explicit_control" not in report.ordinary_tables


@pytest.mark.parametrize(
    "expected_pair",
    [
        SchemaPair(sqlite_version=32, postgres_version=10, epoch=2),
        SchemaPair(sqlite_version=32, postgres_version=9, epoch=1),
        SchemaPair(sqlite_version=31, postgres_version=10, epoch=1),
    ],
)
def test_sqlite_requires_the_complete_manifest_schema_pair(fresh_sqlite, expected_pair):
    with pytest.raises(ValueError, match="does not match manifest"):
        validate_sqlite_schema(fresh_sqlite, MANIFEST, expected_pair=expected_pair)


@pytest.mark.parametrize(
    "expected_pair",
    [
        SchemaPair(sqlite_version=32, postgres_version=10, epoch=2),
        SchemaPair(sqlite_version=32, postgres_version=9, epoch=1),
        SchemaPair(sqlite_version=31, postgres_version=10, epoch=1),
    ],
)
def test_postgres_requires_the_complete_manifest_schema_pair(expected_pair):
    contract = _reviewed_postgres_contract()
    with pytest.raises(ValueError, match="does not match manifest"):
        validate_postgres_schema(
            tables=contract["tables"],
            schema_version=10,
            manifest=MANIFEST,
            expected_pair=expected_pair,
        )


def test_live_postgres_validator_rejects_pair_before_catalog_access():
    class NoCatalogAccess:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("catalog must not be queried for an invalid pair")

    with pytest.raises(ValueError, match="does not match manifest"):
        validate_postgres_connection(
            NoCatalogAccess(),
            MANIFEST,
            schema_version=10,
            expected_pair=SchemaPair(31, 10, 2),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("fk", "foreign-key copy-rank inversion"),
        ("bytea", "transform classification drift"),
        ("jsonb", "transform classification drift"),
        ("timestamptz", "transform classification drift"),
        ("boolean", "transform classification drift"),
        ("path", "unclassified PostgreSQL filesystem-like path"),
    ],
)
def test_postgres_contract_rejects_unreviewed_topology_and_storage(mutation, message):
    contract = _reviewed_postgres_contract()
    tables = deepcopy(contract["tables"])
    if mutation == "fk":
        tables["chunks"]["foreign_keys"][0]["references_table"] = "user_profiles"
    elif mutation == "bytea":
        next(
            column
            for column in tables["chunk_embeddings"]["columns"]
            if column["name"] == "vector"
        )["postgres_type"] = "text"
    elif mutation == "jsonb":
        next(
            column
            for column in tables["answers"]["columns"]
            if column["name"] == "payload"
        )["postgres_type"] = "text"
    elif mutation == "timestamptz":
        next(
            column
            for column in tables["app_settings"]["columns"]
            if column["name"] == "updated_at"
        )["postgres_type"] = "text"
    elif mutation == "boolean":
        next(
            column
            for column in tables["app_settings"]["columns"]
            if column["name"] == "value"
        )["postgres_type"] = "boolean"
    else:
        tables["app_settings"]["columns"].append(
            {"name": "export_path", "postgres_type": "text", "nullable": True}
        )
    with pytest.raises(ValueError, match=message):
        validate_postgres_schema(
            tables=tables,
            schema_version=10,
            manifest=MANIFEST,
            expected_pair=RUNNING_SCHEMA_PAIR,
        )


def test_postgres_contract_rejects_unclassified_bytea_column():
    specs = list(MANIFEST.tables)
    index = next(
        index for index, spec in enumerate(specs) if spec.name == "chunk_embeddings"
    )
    specs[index] = replace(specs[index], blob_columns=())
    unclassified = Manifest(RUNNING_SCHEMA_PAIR, tuple(specs))
    contract = _reviewed_postgres_contract()
    with pytest.raises(ValueError, match="BLOB/bytea classification drift"):
        validate_postgres_schema(
            tables=contract["tables"],
            schema_version=10,
            manifest=unclassified,
            expected_pair=RUNNING_SCHEMA_PAIR,
        )


@pytest.mark.parametrize(
    ("table", "insert_sql", "values"),
    [
        (
            "community_members",
            "INSERT INTO community_members"
            "(notebook_id,level,canonical_id,community_id) VALUES (?,?,?,?)",
            ("n", 1, "c", "community"),
        ),
        (
            "kg_canonical_scratch",
            "INSERT INTO kg_canonical_scratch"
            "(seed,notebook_id,run_id,canonical_id) VALUES (?,?,?,?)",
            ("seed", "n", "run", "canonical"),
        ),
        (
            "kg_cluster_scratch",
            "INSERT INTO kg_cluster_scratch"
            "(object_id,notebook_id,run_id,seed) VALUES (?,?,?,?)",
            ("object", "n", "run", "seed"),
        ),
        (
            "knowledge_object_sources",
            "INSERT INTO knowledge_object_sources"
            "(object_id,source_id,notebook_id) VALUES (?,?,?)",
            ("object", "source", "n"),
        ),
    ],
)
def test_sqlite_shadow_unique_preflight_scans_all_rows(
    fresh_sqlite, table, insert_sql, values
):
    fresh_sqlite.execute(insert_sql, values)
    fresh_sqlite.execute(insert_sql, values)
    with pytest.raises(ValueError, match=f"duplicate replication key in {table}"):
        validate_sqlite_schema(
            fresh_sqlite, MANIFEST, expected_pair=RUNNING_SCHEMA_PAIR
        )


def test_sqlite_text_primary_key_null_is_scanned_and_rejected(fresh_sqlite):
    specs = {spec.name: spec for spec in MANIFEST.replicated}
    assert specs["app_settings"].sqlite_null_guard_columns == ("key",)
    assert all(
        not spec.sqlite_null_guard_columns
        for spec in specs.values()
        if spec.key_kind is ReplicationKeyKind.SHADOW_UNIQUE
    )
    fresh_sqlite.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES (NULL,?,?)",
        ("value", "2026-07-24T00:00:00+00:00"),
    )
    with pytest.raises(ValueError, match="nullable replication key in app_settings"):
        validate_sqlite_schema(
            fresh_sqlite, MANIFEST, expected_pair=RUNNING_SCHEMA_PAIR
        )


def test_sqlite_null_guard_declaration_matches_current_schema(fresh_sqlite):
    report = validate_sqlite_schema(
        fresh_sqlite, MANIFEST, expected_pair=RUNNING_SCHEMA_PAIR
    )
    declared = {
        f"{spec.name}.{column}"
        for spec in MANIFEST.replicated
        for column in spec.sqlite_null_guard_columns
    }
    assert report.sqlite_null_guard_columns == frozenset(declared)
    assert "users.id" in declared
    assert "community_members.canonical_id" not in declared


def test_manifest_rejects_duplicate_rank_unknown_transform_and_fk_inversion(
    fresh_sqlite,
):
    specs = list(MANIFEST.tables)
    specs[1] = replace(specs[1], copy_rank=specs[0].copy_rank)
    with pytest.raises(ValueError, match="copy ranks"):
        validate_manifest(Manifest(RUNNING_SCHEMA_PAIR, tuple(specs)))

    specs = list(MANIFEST.tables)
    specs[0] = replace(specs[0], sqlite_to_postgres="mystery")
    with pytest.raises(ValueError, match="unknown transform"):
        validate_manifest(Manifest(RUNNING_SCHEMA_PAIR, tuple(specs)))

    specs = list(MANIFEST.tables)
    by_name = {spec.name: index for index, spec in enumerate(specs)}
    specs[by_name["notebooks"]] = replace(specs[by_name["notebooks"]], copy_rank=1000)
    specs[by_name["chunks_fts"]] = replace(specs[by_name["chunks_fts"]], copy_rank=15)
    inverted = Manifest(RUNNING_SCHEMA_PAIR, tuple(specs))
    with pytest.raises(ValueError, match="copy-rank inversion"):
        validate_sqlite_schema(
            fresh_sqlite,
            inverted,
            expected_pair=RUNNING_SCHEMA_PAIR,
        )


@pytest.mark.parametrize(
    ("column", "column_type"),
    [
        ("cache_blob", "BLOB"),
        ("cache_varblob", "VARBLOB"),
        ("cache_untyped", ""),
        ("export_path", "TEXT"),
    ],
)
def test_sqlite_rejects_unclassified_blob_or_path(fresh_sqlite, column, column_type):
    fresh_sqlite.execute(f"ALTER TABLE app_settings ADD COLUMN {column} {column_type}")
    with pytest.raises(ValueError, match="unclassified"):
        validate_sqlite_schema(
            fresh_sqlite, MANIFEST, expected_pair=RUNNING_SCHEMA_PAIR
        )


def test_sqlite_validator_supports_default_tuple_row_factory(tmp_path):
    path = tmp_path / "plain-tuples.db"
    settings = Settings(database_url=f"sqlite:///{path}")
    database = SqliteDatabase(settings, REPO_ROOT)
    try:
        SqliteMigrator(database, settings).migrate()
    finally:
        database.close_local()
    with sqlite3.connect(path) as conn:
        assert conn.row_factory is None
        report = validate_sqlite_schema(
            conn, MANIFEST, expected_pair=RUNNING_SCHEMA_PAIR
        )
    assert report.schema_version == 32
    assert report.ordinary_tables == frozenset(MANIFEST.replicated_names)


@pytest.fixture
def shadow_postgres_database():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    from app.core.config import Settings
    from app.repositories.postgres.database import PostgresDatabase
    from tests.postgres.conftest import _isolated_postgres_scope

    with _isolated_postgres_scope(url) as scope:
        settings = Settings(
            database_url=scope.url,
            postgres_pool_min_size=1,
            postgres_pool_max_size=2,
        )
        database = PostgresDatabase(settings, REPO_ROOT)
        try:
            yield database
        finally:
            database.close()


@pytest.mark.postgres_integration
def test_live_postgres_manifest_totality(shadow_postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(shadow_postgres_database).migrate() == 10
    with shadow_postgres_database.connect() as conn:
        report = validate_postgres_connection(
            conn,
            MANIFEST,
            schema_version=10,
            expected_pair=RUNNING_SCHEMA_PAIR,
        )
    assert report.ordinary_tables == frozenset(MANIFEST.replicated_names)


@pytest.mark.postgres_integration
@pytest.mark.parametrize(
    ("table", "insert_sql", "values", "key"),
    [
        (
            "community_members",
            "INSERT INTO community_members"
            "(notebook_id,level,canonical_id,community_id) VALUES (%s,%s,%s,%s)",
            ("n", 1, "c", "community"),
            ("notebook_id", "level", "canonical_id"),
        ),
        (
            "kg_canonical_scratch",
            "INSERT INTO kg_canonical_scratch"
            "(seed,notebook_id,run_id,canonical_id) VALUES (%s,%s,%s,%s)",
            ("seed", "n", "run", "canonical"),
            ("seed", "notebook_id", "run_id"),
        ),
        (
            "kg_cluster_scratch",
            "INSERT INTO kg_cluster_scratch"
            "(object_id,notebook_id,run_id,seed) VALUES (%s,%s,%s,%s)",
            ("object", "n", "run", "seed"),
            ("object_id", "notebook_id", "run_id"),
        ),
        (
            "knowledge_object_sources",
            "INSERT INTO knowledge_object_sources"
            "(object_id,source_id,notebook_id) VALUES (%s,%s,%s)",
            ("object", "source", "n"),
            ("object_id", "source_id"),
        ),
    ],
)
def test_live_postgres_shadow_unique_preflight_scans_duplicates(
    shadow_postgres_database, table, insert_sql, values, key
):
    from psycopg.errors import UniqueViolation

    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(shadow_postgres_database).migrate() == 10
    with shadow_postgres_database.write() as conn:
        conn.execute(insert_sql, values)
    duplicate_blocked = False
    try:
        with shadow_postgres_database.write() as conn:
            conn.execute(insert_sql, values)
    except UniqueViolation:
        duplicate_blocked = True

    with shadow_postgres_database.connect() as conn:
        if duplicate_blocked:
            unique_keys = {
                tuple(row["columns"])
                for row in conn.execute(
                    "SELECT ARRAY("
                    " SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY "
                    " k(attnum, ord) JOIN pg_attribute a "
                    " ON a.attrelid=i.indrelid AND a.attnum=k.attnum "
                    " WHERE k.attnum > 0 ORDER BY k.ord) AS columns "
                    "FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid "
                    "JOIN pg_namespace n ON n.oid=t.relnamespace "
                    "WHERE n.nspname=current_schema() AND t.relname=%s "
                    "AND i.indisunique",
                    (table,),
                ).fetchall()
            }
            assert key in unique_keys
        else:
            with pytest.raises(
                ValueError, match=f"duplicate replication key in {table}"
            ):
                validate_postgres_connection(
                    conn,
                    MANIFEST,
                    schema_version=10,
                    expected_pair=RUNNING_SCHEMA_PAIR,
                )
