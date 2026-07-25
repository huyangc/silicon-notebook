from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator
from app.repositories.postgres.schema_manifest import (
    POSTGRES_BUSINESS_TABLES,
    POSTGRES_BYTEA_COLUMNS,
    POSTGRES_EMPTY_JSON_LIST_SENTINELS,
    POSTGRES_EMPTY_TIME_SENTINELS,
    POSTGRES_JSON_COLUMNS,
    SQLITE_RETIRED_TABLES,
)
from tests.postgres.conftest import (
    _database_catalog,
    _url_with_search_path,
    _validate_database_catalog,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = REPO_ROOT / "backend" / "tests" / "fixtures" / "postgres_schema_contract.json"
MIGRATIONS_PATH = (
    REPO_ROOT / "backend" / "app" / "repositories" / "postgres" / "migrations"
)

# These are reviewed application-owned JSON values. Keeping the classification
# explicit prevents a new TEXT column from silently becoming jsonb merely
# because its name happens to contain "json".
JSON_COLUMNS = POSTGRES_JSON_COLUMNS
BYTEA_COLUMNS = POSTGRES_BYTEA_COLUMNS
EMPTY_JSON_LIST_SENTINELS = POSTGRES_EMPTY_JSON_LIST_SENTINELS
EMPTY_TIME_SENTINELS = POSTGRES_EMPTY_TIME_SENTINELS

EXPECTED_FTS_ROOTS = {"chunks_fts", "kg_objects_fts", "memory_items_fts"}
EXPECTED_ORDINARY_TABLE_COUNT = 60
EXPECTED_REBUILT_TABLE_COUNT = 17
TEST_SCHEMA_PATTERN = re.compile(r"^sn_test_[0-9a-f]{32}$")

CHECK_CONSTRAINTS = {
    "agent_profiles": {
        "ck_agent_profiles_status": "status IN ('active', 'revoked')",
    },
    "memory_embeddings": {
        "ck_memory_embeddings_dimension": "dimension > 0",
    },
    "memory_items": {
        "ck_memory_items_origin": "origin IN ('ask_answer', 'external_agent')",
        "ck_memory_items_promotion_state": (
            "promotion_state IN ('none', 'proposed', 'approved', 'rejected')"
        ),
        "ck_memory_items_status": (
            "status IN ('candidate', 'confirmed', 'rejected', 'deprecated')"
        ),
    },
    "memory_provenance": {
        "ck_memory_provenance_origin": "origin IN ('ask_answer', 'external_agent')",
    },
    "memory_revisions": {
        "ck_memory_revisions_promotion_state": (
            "promotion_state IN ('none', 'proposed', 'approved', 'rejected')"
        ),
        "ck_memory_revisions_revision": "revision > 0",
        "ck_memory_revisions_status": (
            "status IN ('candidate', 'confirmed', 'rejected', 'deprecated')"
        ),
    },
    "model_service_status": {
        "ck_model_service_status_status": "status IN ('ok', 'error')",
        "ck_model_service_status_trigger": (
            "trigger IN ('manual_test', 'observed_failure')"
        ),
    },
    "system_model_service_status": {
        "ck_system_model_service_status_status": "status IN ('ok', 'error')",
        "ck_system_model_service_status_trigger": (
            "trigger IN ('manual_test', 'observed_failure', 'recovery_probe')"
        ),
    },
    "notebook_bases": {
        "ck_notebook_bases_distinct": "notebook_id <> base_notebook_id",
    },
}

ROWID_ORDER_EVIDENCE = {
    "answers": [
        "app.repositories.sqlite.ask_state_store:AskStateStore.get_conversation",
        "app.repositories.sqlite.ask_state_store:AskStateStore.list_conversations",
    ],
    "chunks": [
        "app.repositories.sqlite.chunk_store:ChunkStore.language_probe_rows",
    ],
    "concept_merge_candidates": [
        "app.repositories.sqlite.governance_store:GovernanceStore.pending_merges_batch",
    ],
    "extraction_runs": [
        "app.repositories.sqlite.maintenance:SQLiteMaintenanceAdapter.count_sources_missing_kg",
        "app.repositories.sqlite.source_store:SourceStore.source_from_row",
        "app.repositories.sqlite.source_store:SourceStore.sources_from_rows",
    ],
    "kg_build_jobs": [
        "app.repositories.sqlite.kg_build_job_store:KgBuildJobStore.latest_on",
    ],
    "knowledge_objects": [
        "app.repositories.sqlite.unified_kg_store:UnifiedKgStore.seed_payload_rows",
        "app.repositories.sqlite.unified_kg_store:UnifiedKgStore.stream_seed_rows",
    ],
    "source_elements": [
        "app.repositories.sqlite.source_store:SourceStore.notebook_element_sample",
    ],
}


def _is_time_column(table: str, column: str) -> bool:
    qualified = f"{table}.{column}"
    return column.endswith("_at") or qualified == "knowledge_objects.last_reviewed"


def _sqlite_default(raw: object, *, qualified: str, postgres_type: str) -> Any:
    if raw is None or str(raw).upper() == "NULL":
        return None
    value = str(raw)
    if value.startswith("'") and value.endswith("'"):
        value = value[1:-1].replace("''", "'")
    elif re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    elif re.fullmatch(r"-?(?:[0-9]+\.[0-9]*|[0-9]*\.[0-9]+)", value):
        return float(value)
    if postgres_type == "jsonb":
        if value == "" and qualified in EMPTY_JSON_LIST_SENTINELS:
            return []
        return json.loads(value)
    if qualified in EMPTY_TIME_SENTINELS and value == "":
        return None
    return value


def _postgres_type(table: str, column: str, sqlite_type: str) -> str:
    qualified = f"{table}.{column}"
    if qualified in JSON_COLUMNS:
        return "jsonb"
    if qualified in BYTEA_COLUMNS:
        return "bytea"
    if _is_time_column(table, column):
        return "timestamp with time zone"
    return {
        "TEXT": "text",
        "INTEGER": "bigint",
        "REAL": "double precision",
        "BLOB": "bytea",
    }[sqlite_type.upper()]


def _partial_predicate(index_sql: str | None) -> str | None:
    if not index_sql:
        return None
    match = re.search(r"\bWHERE\b(?P<predicate>.*)$", index_sql, re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    return " ".join(match.group("predicate").split())


def _canonical_predicate(predicate: str | None) -> str | None:
    if predicate is None:
        return None
    value = re.sub(r"::(?:text|character varying)", "", predicate, flags=re.I)
    value = value.replace("<>", "!=")
    value = re.sub(
        r"([a-z_][a-z0-9_]*)\s*!=\s*ALL\s*\(ARRAY\[(.*?)\]\)",
        lambda match: f"{match.group(1)} NOT IN ({match.group(2)})",
        value,
        flags=re.I,
    )
    # pg_get_expr adds grouping parentheses around simple boolean terms.
    previous = None
    while value != previous:
        previous = value
        value = re.sub(r"\(([^()]+)\)", r"\1", value)
    value = re.sub(r"\s*!=\s*", " != ", value)
    value = re.sub(r"\s*=\s*", " = ", value)
    value = re.sub(r"\s*,\s*", ", ", value)
    return " ".join(value.upper().split())


def _check_expressions(create_sql: str) -> list[str]:
    expressions: list[str] = []
    cursor = 0
    pattern = re.compile(r"\bCHECK\s*\(", re.IGNORECASE)
    while match := pattern.search(create_sql, cursor):
        start = match.end()
        depth = 1
        quote = False
        index = start
        while index < len(create_sql) and depth:
            char = create_sql[index]
            if char == "'":
                if quote and index + 1 < len(create_sql) and create_sql[index + 1] == "'":
                    index += 2
                    continue
                quote = not quote
            elif not quote and char == "(":
                depth += 1
            elif not quote and char == ")":
                depth -= 1
            index += 1
        if depth:
            raise AssertionError("unbalanced SQLite CHECK constraint")
        expressions.append(create_sql[start : index - 1])
        cursor = index
    return expressions


def _canonical_check(expression: str) -> str:
    value = expression.strip()
    if value.upper().startswith("CHECK"):
        match = re.fullmatch(r"CHECK\s*\((.*)\)", value, re.IGNORECASE | re.DOTALL)
        assert match is not None, value
        value = match.group(1)
    value = re.sub(r"::(?:text|character varying|bigint|integer)", "", value, flags=re.I)
    previous = None
    while value != previous:
        previous = value
        value = re.sub(r"^\((.*)\)$", r"\1", value.strip(), flags=re.DOTALL)
    value = re.sub(
        r"([a-z_][a-z0-9_]*)\s*=\s*ANY\s*\(ARRAY\[(.*?)\]\)",
        lambda match: f"{match.group(1)} IN ({match.group(2)})",
        value,
        flags=re.I,
    )
    previous = None
    while value != previous:
        previous = value
        value = re.sub(r"^\((.*)\)$", r"\1", value.strip(), flags=re.DOTALL)
    value = value.replace("!=", "<>")
    value = re.sub(r"\s*<>\s*", " <> ", value)
    value = re.sub(r"\s*=\s*", " = ", value)
    value = re.sub(r"\s*>\s*", " > ", value)
    value = re.sub(r"\s*,\s*", ", ", value)
    return " ".join(value.upper().split())


def _sqlite_explicit_indexes(conn, tables: list[str]) -> list[dict[str, Any]]:
    indexes: list[dict[str, Any]] = []
    for table in tables:
        for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
            name = str(row["name"])
            if name.startswith("sqlite_"):
                continue
            keys = []
            for key in conn.execute(f'PRAGMA index_xinfo("{name}")').fetchall():
                if not bool(key["key"]):
                    continue
                column = key["name"]
                if column is None:
                    if name != "idx_knowhow_cells_column_normalized_anchor_row":
                        raise AssertionError(f"unreviewed SQLite expression index: {name}")
                    column = "__js_trim_content_md__"
                keys.append({"column": str(column), "descending": bool(key["desc"])})
            sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()
            indexes.append(
                {
                    "name": name,
                    "table": table,
                    "unique": bool(row["unique"]),
                    "keys": keys,
                    "predicate": _partial_predicate(str(sql_row["sql"]) if sql_row else None),
                }
            )
    return sorted(indexes, key=lambda item: item["name"])


def _sqlite_schema_contract(conn) -> dict[str, Any]:
    table_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    fts_roots = {
        str(row["name"])
        for row in table_rows
        if str(row["sql"] or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE")
    }
    rebuilt_tables = {
        str(row["name"])
        for row in table_rows
        if row["name"] in fts_roots
        or any(str(row["name"]).startswith(f"{root}_") for root in fts_roots)
    }
    sqlite_internal_tables = {
        str(row["name"])
        for row in table_rows
        if str(row["name"]).startswith("sqlite_")
    }
    ordinary_tables = [
        str(row["name"])
        for row in table_rows
        if row["name"] not in rebuilt_tables
        and row["name"] not in sqlite_internal_tables
    ]
    sqlite_checks = {
        str(row["name"]): sorted(
            _canonical_check(expression)
            for expression in _check_expressions(str(row["sql"] or ""))
        )
        for row in table_rows
        if row["name"] in ordinary_tables
        and _check_expressions(str(row["sql"] or ""))
    }

    tables: dict[str, Any] = {}
    for table in ordinary_tables:
        columns = []
        primary_key_parts: list[tuple[int, str]] = []
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
            column = str(row["name"])
            qualified = f"{table}.{column}"
            pk_position = int(row["pk"])
            postgres_type = _postgres_type(table, column, str(row["type"]))
            if pk_position:
                primary_key_parts.append((pk_position, column))
            columns.append(
                {
                    "name": column,
                    "sqlite_type": str(row["type"]).upper(),
                    "postgres_type": postgres_type,
                    "nullable": (
                        qualified in EMPTY_TIME_SENTINELS
                        or not bool(row["notnull"] or pk_position)
                    ),
                    "sqlite_not_null": bool(row["notnull"]),
                    "default": _sqlite_default(
                        row["dflt_value"],
                        qualified=qualified,
                        postgres_type=postgres_type,
                    ),
                }
            )

        foreign_keys = [
            {
                "columns": [str(row["from"])],
                "references_table": str(row["table"]),
                "references_columns": [str(row["to"])],
                "on_update": str(row["on_update"]).upper(),
                "on_delete": str(row["on_delete"]).upper(),
            }
            for row in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        ]
        foreign_keys.sort(
            key=lambda item: (
                item["columns"],
                item["references_table"],
                item["references_columns"],
            )
        )

        unique_keys = []
        for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
            if not bool(row["unique"]) or str(row["origin"]) == "pk":
                continue
            index_name = str(row["name"])
            index_columns = [
                str(info["name"])
                for info in conn.execute(f'PRAGMA index_info("{index_name}")').fetchall()
            ]
            index_sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            unique_keys.append(
                {
                    "columns": index_columns,
                    "predicate": _partial_predicate(
                        str(index_sql_row["sql"])
                        if index_sql_row is not None and index_sql_row["sql"] is not None
                        else None
                    ),
                }
            )
        unique_keys.sort(key=lambda item: (item["columns"], item["predicate"] or ""))

        tables[table] = {
            "classification": "replicated",
            "columns": columns,
            "primary_key": [name for _position, name in sorted(primary_key_parts)],
            "unique_keys": unique_keys,
            "foreign_keys": foreign_keys,
        }

    return {
        "sqlite_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "sqlite_table_count": len(table_rows),
        "sqlite_internal_tables": sorted(sqlite_internal_tables),
        "postgres_version": 9,
        "ordinary_table_count": len(ordinary_tables),
        "rebuilt_table_count": len(rebuilt_tables),
        "rebuilt": {
            "fts_virtual_roots": sorted(fts_roots),
            "fts_internal_tables": sorted(rebuilt_tables - fts_roots),
        },
        "postgres_internal_tables": ["silicon_schema_migrations"],
        "postgres_extensions": ["pg_trgm"],
        "postgres_extension_schemas": {"pg_trgm": "public"},
        "business_check_constraints": {
            table: {
                name: _canonical_check(expression)
                for name, expression in constraints.items()
            }
            for table, constraints in CHECK_CONSTRAINTS.items()
        },
        "sqlite_check_expressions": sqlite_checks,
        "sqlite_explicit_indexes": _sqlite_explicit_indexes(conn, ordinary_tables),
        "ordinal_tables": sorted(ROWID_ORDER_EVIDENCE),
        "ordinal_evidence": ROWID_ORDER_EVIDENCE,
        "normalizations": {
            "sqlite_empty_json_array_to_postgres_array": sorted(
                EMPTY_JSON_LIST_SENTINELS
            ),
            "sqlite_empty_time_to_postgres_null": sorted(EMPTY_TIME_SENTINELS),
            "postgres_null_time_to_domain_empty": sorted(EMPTY_TIME_SENTINELS),
            "sqlite_rowid_to_postgres_ordinal": sorted(ROWID_ORDER_EVIDENCE),
            "postgres_ordinal_sequence_reseed_after_copy": sorted(
                ROWID_ORDER_EVIDENCE
            ),
        },
        "tables": tables,
    }


def _fresh_sqlite_contract(tmp_path: Path) -> dict[str, Any]:
    db_path = tmp_path / "fresh-v31.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    database = SqliteDatabase(settings, REPO_ROOT)
    try:
        assert SqliteMigrator(database, settings).migrate() == list(
            range(1, SCHEMA_VERSION + 1)
        )
        return _sqlite_schema_contract(database.connect())
    finally:
        database.close_local()


def _reviewed_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_fresh_sqlite_v31_matches_reviewed_postgres_contract(tmp_path):
    actual = _fresh_sqlite_contract(tmp_path)
    if os.environ.get("UPDATE_POSTGRES_SCHEMA_CONTRACT") == "1":
        CONTRACT_PATH.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    expected = _reviewed_contract()
    assert actual == expected
    assert actual["ordinary_table_count"] == EXPECTED_ORDINARY_TABLE_COUNT
    assert actual["rebuilt_table_count"] == EXPECTED_REBUILT_TABLE_COUNT
    assert set(actual["rebuilt"]["fts_virtual_roots"]) == EXPECTED_FTS_ROOTS
    expected_check_expressions = {
        table: sorted(_canonical_check(expression) for expression in values.values())
        for table, values in CHECK_CONSTRAINTS.items()
    }
    assert actual["sqlite_check_expressions"] == expected_check_expressions
    assert len(actual["sqlite_explicit_indexes"]) == 84
    assert set(actual["ordinal_tables"]) == set(ROWID_ORDER_EVIDENCE)
    assert actual["sqlite_table_count"] == (
        actual["ordinary_table_count"]
        + actual["rebuilt_table_count"]
        + len(actual["sqlite_internal_tables"])
    )
    assert set(actual["tables"]) == {
        name for name, facts in expected["tables"].items() if facts["classification"] == "replicated"
    }


def _postgres_schema_facts(conn) -> dict[str, Any]:
    tables = [
        str(row["table_name"])
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_type='BASE TABLE' "
            "ORDER BY table_name"
        ).fetchall()
    ]
    columns: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT table_name, column_name, data_type, is_nullable, column_default, "
        "collation_name, "
        "ordinal_position FROM information_schema.columns "
        "WHERE table_schema=current_schema() ORDER BY table_name, ordinal_position"
    ).fetchall():
        columns.setdefault(str(row["table_name"]), []).append(dict(row))

    primary_keys: dict[str, list[str]] = {}
    foreign_keys: dict[str, list[dict[str, Any]]] = {}
    constraints = conn.execute(
        "SELECT c.conname, c.contype, child.relname AS table_name, "
        "parent.relname AS references_table, c.confupdtype, c.confdeltype, "
        "ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ord) "
        "      JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum "
        "      ORDER BY k.ord) AS columns, "
        "ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(attnum, ord) "
        "      JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.attnum "
        "      ORDER BY k.ord) AS references_columns "
        "FROM pg_constraint c "
        "JOIN pg_class child ON child.oid=c.conrelid "
        "JOIN pg_namespace n ON n.oid=child.relnamespace "
        "LEFT JOIN pg_class parent ON parent.oid=c.confrelid "
        "WHERE n.nspname=current_schema() AND c.contype IN ('p','f') "
        "ORDER BY child.relname, c.conname"
    ).fetchall()
    action = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
    for row in constraints:
        table = str(row["table_name"])
        if row["contype"] == "p":
            primary_keys[table] = list(row["columns"])
        else:
            foreign_keys.setdefault(table, []).append(
                {
                    "columns": list(row["columns"]),
                    "references_table": str(row["references_table"]),
                    "references_columns": list(row["references_columns"]),
                    "on_update": action[str(row["confupdtype"])],
                    "on_delete": action[str(row["confdeltype"])],
                }
            )
    for values in foreign_keys.values():
        values.sort(
            key=lambda item: (
                item["columns"], item["references_table"], item["references_columns"]
            )
        )

    unique_keys: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT table_name, columns, predicate FROM ("
        " SELECT t.relname AS table_name, i.indexrelid, "
        " ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum, ord) "
        "       JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum "
        "       WHERE k.attnum > 0 ORDER BY k.ord) AS columns, "
        " pg_get_expr(i.indpred, i.indrelid) AS predicate "
        " FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid "
        " JOIN pg_namespace n ON n.oid=t.relnamespace "
        " WHERE n.nspname=current_schema() AND i.indisunique AND NOT i.indisprimary"
        ") q ORDER BY table_name, columns::text, predicate"
    ).fetchall():
        unique_keys.setdefault(str(row["table_name"]), []).append(
            {
                "columns": list(row["columns"]),
                "predicate": (
                    " ".join(str(row["predicate"]).split())
                    if row["predicate"] is not None
                    else None
                ),
            }
        )

    return {
        "tables": tables,
        "columns": columns,
        "primary_keys": primary_keys,
        "foreign_keys": foreign_keys,
        "unique_keys": unique_keys,
    }


def _postgres_explicit_indexes(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT idx.relname AS name, tbl.relname AS table_name, i.indisunique, "
        "pg_get_expr(i.indpred, i.indrelid) AS predicate, "
        "ARRAY(SELECT pg_get_indexdef(i.indexrelid, position, true) "
        "      FROM generate_series(1, i.indnkeyatts) AS position "
        "      ORDER BY position) AS keys, "
        "ARRAY(SELECT c.collname FROM unnest(i.indcollation) WITH ORDINALITY x(collation_oid, position) "
        "      LEFT JOIN pg_collation c ON c.oid=x.collation_oid "
        "      ORDER BY position) AS collations, "
        "i.indoption::smallint[] AS options "
        "FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid "
        "JOIN pg_class tbl ON tbl.oid=i.indrelid "
        "JOIN pg_namespace n ON n.oid=tbl.relnamespace "
        "WHERE n.nspname=current_schema() ORDER BY idx.relname"
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["name"])
        keys = []
        for position, (key, option) in enumerate(
            zip(row["keys"], row["options"], strict=True)
        ):
            if name == "idx_knowhow_cells_column_normalized_anchor_row" and position == 1:
                column = "__js_trim_content_md__"
            else:
                column = re.sub(r"\s+(?:ASC|DESC)(?:\s+NULLS\s+(?:FIRST|LAST))?$", "", str(key), flags=re.I)
                column = column.strip('"')
            keys.append({"column": column, "descending": bool(int(option) & 1)})
        result[name] = {
            "name": name,
            "table": str(row["table_name"]),
            "unique": bool(row["indisunique"]),
            "keys": keys,
            "collations": list(row["collations"]),
            "predicate": (
                " ".join(str(row["predicate"]).split())
                if row["predicate"] is not None
                else None
            ),
        }
    return result


def _normalize_postgres_default(raw: object, postgres_type: str) -> Any:
    if raw is None:
        return None
    value = str(raw)
    if postgres_type == "jsonb":
        match = re.fullmatch(r"'(.*)'::jsonb", value, re.DOTALL)
        assert match is not None, value
        return json.loads(match.group(1).replace("''", "'"))
    if postgres_type == "text":
        match = re.fullmatch(r"'(.*)'::text", value, re.DOTALL)
        assert match is not None, value
        return match.group(1).replace("''", "'")
    if postgres_type == "bigint":
        match = re.fullmatch(r"'?(-?[0-9]+)'?(?:::(?:smallint|integer|bigint))?", value)
        assert match is not None, value
        return int(match.group(1))
    if postgres_type == "double precision":
        return float(re.sub(r"::double precision$", "", value))
    raise AssertionError(f"unexpected default {value!r} for {postgres_type}")


def _assert_packaged_postgres_schema_has_bidirectional_semantic_parity(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    contract = _reviewed_contract()
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate() == contract["postgres_version"]
    with postgres_database.connect() as conn:
        facts = _postgres_schema_facts(conn)
        server_encoding = conn.execute(
            "SELECT current_setting('server_encoding') AS value"
        ).fetchone()["value"]

    assert server_encoding == "UTF8"
    expected_business = set(contract["tables"])
    expected_internal = set(contract["postgres_internal_tables"])
    assert set(facts["tables"]) == expected_business | expected_internal
    assert expected_business.isdisjoint(expected_internal)

    for table, expected in contract["tables"].items():
        actual_columns = facts["columns"][table]
        expected_ordinal = table in set(contract["ordinal_tables"])
        assert [row["column_name"] for row in actual_columns] == [
            column["name"] for column in expected["columns"]
        ] + (["ordinal"] if expected_ordinal else [])
        for actual, column in zip(
            actual_columns[: len(expected["columns"])], expected["columns"], strict=True
        ):
            assert actual["data_type"] == column["postgres_type"], (table, column["name"])
            assert actual["collation_name"] == (
                "C" if column["postgres_type"] == "text" else None
            ), (table, column["name"], actual["collation_name"])
            assert (actual["is_nullable"] == "YES") == column["nullable"], (
                table,
                column["name"],
            )
            assert _normalize_postgres_default(
                actual["column_default"], column["postgres_type"]
            ) == column["default"], (table, column["name"])
        assert facts["primary_keys"].get(table, []) == expected["primary_key"]
        assert facts["foreign_keys"].get(table, []) == expected["foreign_keys"]
        actual_unique = [
            {**item, "predicate": _canonical_predicate(item["predicate"])}
            for item in facts["unique_keys"].get(table, [])
        ]
        expected_unique = [
            {**item, "predicate": _canonical_predicate(item["predicate"])}
            for item in expected["unique_keys"]
        ]
        ordinal_unique = [
            item for item in actual_unique if item["columns"] == ["ordinal"]
        ]
        actual_unique = [
            item for item in actual_unique if item["columns"] != ["ordinal"]
        ]
        assert ordinal_unique == (
            [{"columns": ["ordinal"], "predicate": None}]
            if expected_ordinal
            else []
        )
        assert actual_unique == expected_unique

    with postgres_database.connect() as conn:
        extension_schemas = {
            str(row["extname"]): str(row["nspname"])
            for row in conn.execute(
                "SELECT e.extname, n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid=e.extnamespace "
                "WHERE e.extname='pg_trgm'"
            ).fetchall()
        }
        vector_extension = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname='vector'"
        ).fetchone()
        ordinal_columns = conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable, is_identity, "
            "identity_generation FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND is_identity='YES'"
        ).fetchall()
        business_constraints = conn.execute(
            "SELECT child.relname AS table_name, c.conname, c.contype, c.condeferrable, "
            "pg_get_constraintdef(c.oid, true) AS definition "
            "FROM pg_constraint c JOIN pg_class child ON child.oid=c.conrelid "
            "JOIN pg_namespace n ON n.oid=child.relnamespace "
            "WHERE n.nspname=current_schema() AND child.relname <> 'silicon_schema_migrations' "
            "ORDER BY child.relname, c.conname"
        ).fetchall()
        index_rows = conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname=current_schema() ORDER BY indexname"
        ).fetchall()
        explicit_indexes = _postgres_explicit_indexes(conn)
    assert set(extension_schemas) == set(contract["postgres_extensions"])
    assert extension_schemas == contract["postgres_extension_schemas"]
    assert vector_extension is None
    assert {
        str(row["table_name"]): {
            "column": str(row["column_name"]),
            "type": str(row["data_type"]),
            "nullable": str(row["is_nullable"]),
            "identity": str(row["is_identity"]),
            "generation": str(row["identity_generation"]),
        }
        for row in ordinal_columns
    } == {
        table: {
            "column": "ordinal",
            "type": "bigint",
            "nullable": "NO",
            "identity": "YES",
            "generation": "BY DEFAULT",
        }
        for table in contract["ordinal_tables"]
    }
    assert all(
        str(row["conname"]).startswith(("pk_", "fk_", "uq_", "ck_"))
        for row in business_constraints
    )
    assert not any(bool(row["condeferrable"]) for row in business_constraints)
    actual_checks: dict[str, dict[str, str]] = {}
    for row in business_constraints:
        if row["contype"] == "c":
            actual_checks.setdefault(str(row["table_name"]), {})[
                str(row["conname"])
            ] = _canonical_check(str(row["definition"]))
    assert actual_checks == contract["business_check_constraints"]
    index_definitions = {str(row["indexname"]): str(row["indexdef"]) for row in index_rows}
    for name in (
        "idx_chunks_text_trgm",
        "idx_knowledge_objects_name_trgm",
        "idx_memory_items_title_trgm",
        "idx_memory_items_content_md_trgm",
        "idx_memory_items_tags_trgm",
    ):
        assert "USING gin" in index_definitions[name]
        assert "gin_trgm_ops" in index_definitions[name]
    assert len(contract["sqlite_explicit_indexes"]) == 84
    for expected_index in contract["sqlite_explicit_indexes"]:
        actual_index = explicit_indexes[expected_index["name"]]
        assert actual_index["table"] == expected_index["table"]
        assert actual_index["unique"] == expected_index["unique"]
        assert actual_index["keys"] == expected_index["keys"]
        assert _canonical_predicate(actual_index["predicate"]) == _canonical_predicate(
            expected_index["predicate"]
        )
    assert all(
        collation in (None, "C")
        for index in explicit_indexes.values()
        for collation in index["collations"]
    )
    for name in (
        "idx_knowhow_cells_column_normalized_anchor_row",
        "idx_knowledge_objects_name_trgm",
        "idx_memory_items_tags_trgm",
    ):
        assert "C" in explicit_indexes[name]["collations"], name


@pytest.mark.postgres_integration
def test_packaged_postgres_schema_has_bidirectional_semantic_parity(postgres_database):
    _assert_packaged_postgres_schema_has_bidirectional_semantic_parity(
        postgres_database
    )


@pytest.mark.postgres_integration
def test_schema_parity_on_utf8_database_with_non_c_default_collation(
    postgres_non_c_database,
):
    with postgres_non_c_database.connect() as conn:
        row = conn.execute(
            "SELECT current_database() AS database, "
            "current_setting('server_encoding') AS encoding, "
            "to_jsonb(d) AS catalog FROM pg_database AS d "
            "WHERE datname=current_database()"
        ).fetchone()
    catalog = _database_catalog(row)
    _validate_database_catalog(catalog, expected="non-c")
    assert catalog.encoding == "UTF8"
    assert catalog.provider == "i"
    assert catalog.provider_locale == "en-US"
    _assert_packaged_postgres_schema_has_bidirectional_semantic_parity(
        postgres_non_c_database
    )


@pytest.mark.postgres_integration
def test_packaged_migrations_are_idempotent_from_empty_schema(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST

    migrator = PostgresMigrator(postgres_database)
    assert migrator.current_version() == 0
    assert migrator.migrate() == 10
    assert migrator.migrate() == 10
    assert migrator.current_version() == 10
    assert POSTGRES_SCHEMA_MANIFEST.sqlite_version == 31
    assert POSTGRES_SCHEMA_MANIFEST.postgres_version == 10


@pytest.mark.postgres_integration
def test_packaged_migration_checksum_drift_is_rejected(postgres_database, tmp_path):
    from app.repositories.postgres.migrator import PostgresMigrator, load_migrations

    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate() == 10

    copied = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_PATH, copied)
    first = copied / "0001_initial.sql"
    first.write_text(first.read_text(encoding="utf-8") + "\n-- drift\n", encoding="utf-8")
    changed = PostgresMigrator(postgres_database, migrations=load_migrations(copied))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        changed.migrate()


@pytest.mark.postgres_integration
def test_pg_trgm_is_shared_outside_disposable_schema_lifetimes(postgres_scope):
    import psycopg
    from psycopg import sql

    from app.repositories.postgres.database import PostgresDatabase
    from app.repositories.postgres.migrator import PostgresMigrator

    schemas = [f"sn_test_{uuid.uuid4().hex}" for _ in range(2)]
    assert all(TEST_SCHEMA_PATTERN.fullmatch(schema) for schema in schemas)
    databases = []
    with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
        for schema in schemas:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        for schema in schemas:
            settings = Settings(
                database_url=_url_with_search_path(postgres_scope.base_url, schema),
                postgres_pool_min_size=1,
                postgres_pool_max_size=1,
            )
            databases.append(PostgresDatabase(settings, REPO_ROOT))

        barrier = threading.Barrier(2)
        versions: list[int] = []
        failures: list[BaseException] = []

        def migrate(database) -> None:
            try:
                barrier.wait(timeout=5)
                versions.append(PostgresMigrator(database).migrate())
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        workers = [
            threading.Thread(target=migrate, args=(database,)) for database in databases
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        assert not any(worker.is_alive() for worker in workers)
        assert failures == []
        assert sorted(versions) == [9, 9]

        with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
            extension_schema = conn.execute(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid=e.extnamespace "
                "WHERE e.extname='pg_trgm'"
            ).fetchone()[0]
            assert extension_schema == "public"
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schemas[0]))
            )

        with databases[1].connect() as conn:
            remaining = conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname=current_schema() "
                "AND indexname='idx_chunks_text_trgm'"
            ).fetchone()
            extension_schema = conn.execute(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid=e.extnamespace "
                "WHERE e.extname='pg_trgm'"
            ).fetchone()["nspname"]
        assert remaining == {"indexname": "idx_chunks_text_trgm"}
        assert extension_schema == "public"
        assert PostgresMigrator(databases[1]).migrate() == 10
    finally:
        for database in databases:
            database.close()
        with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
            for schema in schemas:
                if TEST_SCHEMA_PATTERN.fullmatch(schema) is None:
                    raise RuntimeError("refusing to drop an unvalidated PostgreSQL schema")
                exists = conn.execute(
                    "SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)
                ).fetchone()
                if exists is not None:
                    conn.execute(
                        sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                    )


def test_reviewed_json_and_binary_mappings_cover_only_real_columns(tmp_path):
    from app.repositories.postgres.schema_manifest import POSTGRES_ROWID_ORDINAL_TABLES

    contract = _fresh_sqlite_contract(tmp_path)
    qualified = {
        f"{table}.{column['name']}"
        for table, facts in contract["tables"].items()
        for column in facts["columns"]
    }
    assert JSON_COLUMNS <= qualified
    assert BYTEA_COLUMNS <= qualified
    assert EMPTY_JSON_LIST_SENTINELS <= qualified
    assert EMPTY_TIME_SENTINELS <= qualified
    assert set(POSTGRES_ROWID_ORDINAL_TABLES) == set(ROWID_ORDER_EVIDENCE)
    assert set(POSTGRES_BUSINESS_TABLES) == set(contract["tables"])
    assert set(SQLITE_RETIRED_TABLES).isdisjoint(POSTGRES_BUSINESS_TABLES)
    assert contract["ordinal_evidence"] == ROWID_ORDER_EVIDENCE
    fingerprint = hashlib.sha256(
        "\n".join(sorted(contract["tables"])).encode("utf-8")
    ).hexdigest()
    assert fingerprint == "87b641597f5fd2d5fab4b235f9c0d1c00a203d4ba1063a96b8943813967d35fc"


def test_packaged_index_migration_phases_are_exact():
    from app.repositories.postgres.migrator import load_migrations

    migrations = {migration.version: migration for migration in load_migrations(MIGRATIONS_PATH)}
    assert [(version, migrations[version].name) for version in migrations] == [
        (1, "initial"),
        (2, "integrity_indexes"),
        (3, "core_indexes"),
        (4, "knowledge_indexes"),
        (5, "memory_knowhow_governance_indexes"),
        (6, "search_gin"),
        (7, "cluster_membership_unique"),
        (8, "master_v28_features"),
        (9, "sources_file_hash_index"),
        (10, "report_understanding"),
    ]

    def index_declarations(version: int) -> list[tuple[bool, str]]:
        return [
            (bool(unique), name)
            for unique, name in re.findall(
                r"(?mi)^CREATE\s+(UNIQUE\s+)?INDEX\s+([a-z0-9_]+)",
                migrations[version].sql,
            )
        ]

    integrity_names = {
        "idx_kg_build_jobs_one_running",
        "idx_memory_answer_once",
        "idx_notebooks_share_token",
        "idx_promotion_object",
        "idx_sources_memory_id",
        "idx_users_username",
    }
    assert index_declarations(1) == []
    assert index_declarations(2) == [
        (True, name)
        for name in (
            "idx_kg_build_jobs_one_running",
            "idx_memory_answer_once",
            "idx_notebooks_share_token",
            "idx_promotion_object",
            "idx_sources_memory_id",
            "idx_users_username",
        )
    ]
    operational = [
        declaration
        for version in (3, 4, 5, 8)
        for declaration in index_declarations(version)
    ]
    assert len(operational) == 76
    assert not any(unique for unique, _name in operational)
    gin_names = {
        "idx_chunks_text_trgm",
        "idx_knowledge_objects_name_trgm",
        "idx_memory_items_title_trgm",
        "idx_memory_items_content_md_trgm",
        "idx_memory_items_tags_trgm",
    }
    gin_declarations = index_declarations(6)
    assert len(gin_declarations) == 5
    assert not any(unique for unique, _name in gin_declarations)
    assert {name for _unique, name in gin_declarations} == gin_names
    assert all("USING gin" in line for line in re.findall(
        r"(?mis)^CREATE INDEX idx_.*?;", migrations[6].sql
    ))
    cluster_unique = index_declarations(7)
    assert cluster_unique == [(True, "uq_clusters_notebook_type_member")]

    # 0008_master_v28_features packs the SQLite v24–v28 reconciliation indexes
    # (canonical scratch + knowhow change/milestone history). They mirror the
    # SQLite contract one-for-one, so they must be counted toward the parity
    # assertion below or `packaged_names` would fall short by exactly these three.
    v28_feature_indexes = index_declarations(8)
    assert v28_feature_indexes == [
        (False, "idx_kg_canonical_scratch_nb_run_seed"),
        (False, "idx_knowhow_changes_table"),
        (False, "idx_knowhow_milestones_table"),
    ]

    # 0009_sources_file_hash_index mirrors SQLite v30's sources(notebook_id,
    # file_hash) dedup lookup index — the first SQLite index added after the
    # PostgreSQL adapter landed, so it establishes the "one SQLite index ->
    # one packaged PostgreSQL migration" pattern rather than SQLite-only drift.
    v30_index = index_declarations(9)
    assert v30_index == [(False, "idx_sources_notebook_file_hash")]

    contract_names = {item["name"] for item in _reviewed_contract()["sqlite_explicit_indexes"]}
    packaged_names = (
        integrity_names
        | {name for _unique, name in operational}
        | gin_names
        | {name for _unique, name in cluster_unique}
        | {name for _unique, name in v28_feature_indexes}
        | {name for _unique, name in v30_index}
    )
    assert packaged_names == contract_names | gin_names


def test_initial_migration_guards_utf8_before_business_ddl():
    from app.repositories.postgres.migrator import load_migrations

    initial = load_migrations(MIGRATIONS_PATH)[0].sql
    guard_position = initial.index("current_setting('server_encoding')")
    first_business_ddl = initial.index("CREATE TABLE agent_access_tokens")
    assert guard_position < first_business_ddl
    assert "server_encoding must be UTF8" in initial
