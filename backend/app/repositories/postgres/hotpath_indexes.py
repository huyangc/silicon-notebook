"""Online-safe PostgreSQL builder for hot-path fix batch 1's six index groups
(eight indexes).

This is the sibling of ``retrieval_indexes.py`` (GIN trigram indexes) but for
eight plain btree/partial indexes across six query-family groups -- see
``backend/app/repositories/postgres/migrations/0039_hotpath_batch1_indexes.sql``
for the full "which query family does this serve" evidence per group. That
migration and this module are two independent hand-authored copies of the
same eight index shapes on purpose (a migration file cannot import Python at
apply time), so ``backend/tests/test_hotpath_indexes.py`` parses both and
cross-checks them statement-by-statement to catch drift.

Inspecting is always safe (read-only ``pg_index``/``pg_class`` catalog
queries, no advisory lock). Building is online-safe: every ``CREATE INDEX``
runs with ``CONCURRENTLY`` outside any transaction (the connection is
``autocommit=True``), one statement per index, so a single slow build never
blocks the others and never holds a transaction open against a live table
for its whole duration.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Callable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


HOTPATH_INDEX_LOCK_NAME = "silicon-notebook:postgres-hotpath-indexes:v1"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class HotpathIndexError(RuntimeError):
    """Credential-free, operator-actionable hot-path-index failure."""


@dataclass(frozen=True)
class HotpathIndexSpec:
    name: str
    table: str
    columns: tuple[str, ...]
    predicate: str  # "" for a full (non-partial) index; verbatim DDL text
    # PostgreSQL's own canonical ``pg_get_expr()`` rendering of ``predicate``,
    # "" when predicate == "". PostgreSQL rewrites some predicate syntax on
    # store (e.g. ``IN ('a','b')`` becomes ``= ANY (ARRAY['a'::text,'b'::text])``),
    # so the text a catalog read reports back can differ byte-for-byte from
    # the DDL text that created it even when the index is exactly right; this
    # field lets ``_matches_shape`` compare against what the catalog will
    # actually say instead of a shape it can never see. Empirically verified
    # against a live PostgreSQL 16 instance -- see this module's tests.
    predicate_shape: str
    serves: str  # short human description of the query family this serves

    def column_list_sql(self) -> str:
        return ", ".join(self.columns)

    def ddl(self, schema: str, *, concurrently: bool) -> sql.Composed:
        concurrently_kw = sql.SQL("CONCURRENTLY ") if concurrently else sql.SQL("")
        stmt = sql.SQL("CREATE INDEX {concurrently}IF NOT EXISTS {index} ON {schema}.{table}({columns})").format(
            concurrently=concurrently_kw,
            index=sql.Identifier(self.name),
            schema=sql.Identifier(schema),
            table=sql.Identifier(self.table),
            columns=sql.SQL(self.column_list_sql()),
        )
        if self.predicate:
            stmt = sql.SQL("{stmt} WHERE {predicate}").format(
                stmt=stmt, predicate=sql.SQL(self.predicate)
            )
        return stmt


# The six groups from the hot-path fix batch 1 production audit. Column and
# predicate text here must stay semantically identical to
# migrations/0039_hotpath_batch1_indexes.sql -- see this module's docstring
# and backend/tests/test_hotpath_indexes.py.
HOTPATH_INDEX_SPECS: tuple[HotpathIndexSpec, ...] = (
    HotpathIndexSpec(
        name="idx_clusters_nb_canonical",
        table="concept_clusters",
        columns=("notebook_id", "canonical_id"),
        predicate="",
        predicate_shape="",
        serves="concept-detail / co-mention peer-name / relation-endpoint-name lookups",
    ),
    HotpathIndexSpec(
        name="idx_clusters_nb_canonical_name_lower",
        table="concept_clusters",
        columns=("notebook_id", "lower(canonical_name)"),
        predicate="",
        predicate_shape="",
        serves="unified_kg_store.py:resolve_focal",
    ),
    HotpathIndexSpec(
        name="idx_extraction_runs_notebook",
        table="extraction_runs",
        columns=("notebook_id",),
        predicate="",
        predicate_shape="",
        serves="reverse-FK cover for notebook deletion cascade",
    ),
    HotpathIndexSpec(
        name="idx_knowledge_source_fact_elements_notebook",
        table="knowledge_source_fact_elements",
        columns=("notebook_id",),
        predicate="",
        predicate_shape="",
        serves="reverse-FK cover for notebook deletion cascade",
    ),
    HotpathIndexSpec(
        name="idx_memory_items_notebook",
        table="memory_items",
        columns=("notebook_id",),
        predicate="",
        predicate_shape="",
        serves="reverse-FK cover for notebook deletion cascade",
    ),
    HotpathIndexSpec(
        name="idx_knowledge_relations_nb_source_target_edge",
        table="knowledge_relations",
        columns=("notebook_id", "source_object_id", "target_object_id", "edge_type"),
        predicate="",
        predicate_shape="",
        serves="knowledge_store.py:in_network_relation_rows",
    ),
    HotpathIndexSpec(
        name="idx_chunks_source_ordinal",
        table="chunks",
        columns=("source_id", "ordinal"),
        predicate="",
        predicate_shape="",
        serves="search.py:chunk_section_rows (chunks_by_section)",
    ),
    HotpathIndexSpec(
        name="idx_sources_nb_hidden_type",
        table="sources",
        columns=("notebook_id", "source_type"),
        predicate="source_type IN ('memory', 'knowhow')",
        # PostgreSQL canonicalizes an ``IN (...)`` list predicate to an
        # ``= ANY (ARRAY[...])`` form on store; each element also gains an
        # explicit ``::text`` cast since ``source_type`` is ``text``.
        predicate_shape=(
            "source_type = ANY (ARRAY['memory'::text, 'knowhow'::text])"
        ),
        serves="source_store.py:hidden_source_ids",
    ),
)


def _schema(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value or ""):
        raise ValueError("PostgreSQL schema must be a simple identifier")
    return value


def _connect(database_url: str):
    if not database_url:
        raise ValueError("database URL is required")
    try:
        return psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
            application_name="silicon-notebook-hotpath-index-builder",
            connect_timeout=10,
        )
    except Exception:
        raise HotpathIndexError("postgres_connection_failed") from None


def _index_row(connection, schema: str, name: str):
    return connection.execute(
        "SELECT idx.relname AS index_name, tbl.relname AS table_name, "
        "tbl_ns.nspname AS table_schema, i.indisvalid, i.indisready, "
        "ARRAY(SELECT pg_get_indexdef(i.indexrelid,n,true) "
        "FROM generate_series(1,i.indnkeyatts) AS n ORDER BY n) AS keys, "
        "pg_get_expr(i.indpred,i.indrelid,true) AS predicate "
        "FROM pg_index i "
        "JOIN pg_class idx ON idx.oid=i.indexrelid "
        "JOIN pg_namespace ns ON ns.oid=idx.relnamespace "
        "JOIN pg_class tbl ON tbl.oid=i.indrelid "
        "JOIN pg_namespace tbl_ns ON tbl_ns.oid=tbl.relnamespace "
        "WHERE ns.nspname=%s AND idx.relname=%s",
        (schema, name),
    ).fetchone()


def _normalized_expr(value: str) -> str:
    return " ".join((value or "").lower().replace("::text", "").split())


def _matches_shape(row, spec: HotpathIndexSpec) -> bool:
    """Compare the catalog's actual key list / partial predicate against
    ``spec``, the same-shape check ``retrieval_indexes.py``'s ``_index_row``
    sibling does for GIN indexes. A same-named index on the right table that
    was hand-built with different columns or a different predicate must never
    be silently accepted as "ready" -- see this module's docstring for why
    ``predicate_shape`` (not ``predicate``) is the comparison target.
    """
    keys = tuple(_normalized_expr(str(value)) for value in row["keys"] or ())
    expected_keys = tuple(_normalized_expr(value) for value in spec.columns)
    if keys != expected_keys:
        return False
    predicate = _normalized_expr(str(row["predicate"] or ""))
    expected_predicate = _normalized_expr(spec.predicate_shape)
    return predicate == expected_predicate


def _state(connection, schema: str, spec: HotpathIndexSpec) -> dict[str, object]:
    row = _index_row(connection, schema, spec.name)
    if row is None:
        return {"name": spec.name, "serves": spec.serves, "state": "缺失"}
    if str(row["table_schema"]) != schema or str(row["table_name"]) != spec.table:
        raise HotpathIndexError(f"unexpected_index_owner:{spec.name}")
    if not _matches_shape(row, spec):
        return {"name": spec.name, "serves": spec.serves, "state": "UNEXPECTED"}
    if not bool(row["indisvalid"]) or not bool(row["indisready"]):
        return {"name": spec.name, "serves": spec.serves, "state": "INVALID"}
    return {"name": spec.name, "serves": spec.serves, "state": "存在"}


def inspect_hotpath_indexes(database_url: str, *, schema: str = "public") -> dict:
    """Read-only pg_index/pg_class check. Never takes the build advisory lock."""
    schema = _schema(schema)
    with _connect(database_url) as connection:
        states = [_state(connection, schema, spec) for spec in HOTPATH_INDEX_SPECS]
    return {"schema": schema, "indexes": states}


def install_hotpath_indexes(
    database_url: str,
    *,
    schema: str = "public",
    lock_timeout_seconds: int = 5,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Build every missing index with ``CREATE INDEX CONCURRENTLY``, one
    statement per index, outside any transaction.

    An ``INVALID`` index (a prior ``CONCURRENTLY`` build that failed midway,
    leaving a catalog row PostgreSQL will never finish on its own) is never
    auto-dropped here -- unlike ``retrieval_indexes.py``'s GIN builder, this
    one only reports operator-actionable guidance
    (``DROP INDEX CONCURRENTLY <name>;`` then rerun) and fails the whole run
    with exit code 1, so an operator always makes that destructive call
    explicitly rather than a script silently doing it in the background of
    an unrelated missing-index build.
    """
    schema = _schema(schema)
    if not isinstance(lock_timeout_seconds, int) or isinstance(lock_timeout_seconds, bool):
        raise ValueError("lock timeout must be an integer")
    if not 1 <= lock_timeout_seconds <= 300:
        raise ValueError("lock timeout must be in 1..300 seconds")
    emit = progress or (lambda _message: None)
    with _connect(database_url) as connection:
        try:
            locked = bool(
                connection.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked",
                    (HOTPATH_INDEX_LOCK_NAME,),
                ).fetchone()["locked"]
            )
            if not locked:
                raise HotpathIndexError("hotpath_index_build_already_running")
            connection.execute(
                "SELECT set_config('statement_timeout','0',false),"
                "set_config('lock_timeout',%s,false)",
                (f"{lock_timeout_seconds}s",),
            )
            invalid_names: list[str] = []
            current_spec_name: str | None = None
            for spec in HOTPATH_INDEX_SPECS:
                current_spec_name = spec.name
                state = _state(connection, schema, spec)
                if state["state"] == "存在":
                    emit(f"{spec.name}: already ready")
                    continue
                if state["state"] == "UNEXPECTED":
                    # A same-named index on the right table but a different
                    # column list or predicate is fail-closed, never repaired
                    # or dropped as if it were this tool's own interrupted
                    # artifact -- see _matches_shape's docstring.
                    raise HotpathIndexError(f"unexpected_index_definition:{spec.name}")
                if state["state"] == "INVALID":
                    invalid_names.append(spec.name)
                    emit(
                        f"{spec.name}: INVALID (a prior CONCURRENTLY build did "
                        f"not finish) -- run `DROP INDEX CONCURRENTLY {spec.name};` "
                        "then rerun --apply"
                    )
                    continue
                emit(f"{spec.name}: building concurrently")
                started = time.monotonic()
                connection.execute(spec.ddl(schema, concurrently=True))
                elapsed_ms = (time.monotonic() - started) * 1000
                state = _state(connection, schema, spec)
                if state["state"] != "存在":
                    raise HotpathIndexError(f"index_verification_failed:{spec.name}")
                emit(f"{spec.name}: ready ({elapsed_ms:.0f}ms)")
            if invalid_names:
                raise HotpathIndexError(
                    "invalid_indexes_need_manual_drop:" + ",".join(invalid_names)
                )
        except HotpathIndexError:
            raise
        except psycopg.errors.LockNotAvailable:
            raise HotpathIndexError("postgres_lock_timeout") from None
        except Exception as exc:
            # Credential-free diagnostics: which spec was in flight (already
            # a public, non-secret name from this module) plus the
            # PostgreSQL SQLSTATE code, never the exception's own message
            # (which can echo back SQL text or literal values).
            detail = f":{current_spec_name}" if current_spec_name else ""
            sqlstate = getattr(exc, "sqlstate", None)
            if sqlstate:
                detail += f":{sqlstate}"
            raise HotpathIndexError(f"hotpath_index_build_failed{detail}") from None
        finally:
            try:
                connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                    (HOTPATH_INDEX_LOCK_NAME,),
                )
            except Exception:
                pass
    return inspect_hotpath_indexes(database_url, schema=schema)
