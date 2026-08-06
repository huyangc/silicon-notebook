"""Online-safe PostgreSQL lexical-index installation for large live tables."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


RETRIEVAL_INDEX_LOCK_NAME = "silicon-notebook:postgres-retrieval-indexes:v1"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RetrievalIndexError(RuntimeError):
    """Credential-free, operator-actionable retrieval-index failure."""


@dataclass(frozen=True)
class IndexShape:
    table: str
    keys: tuple[str, ...]
    predicate: str
    opclasses: tuple[str, ...]
    collations: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalIndexSpec:
    name: str
    legacy_name: str
    ddl_template: str
    shape: IndexShape
    legacy_shape: IndexShape

    def ddl(self, schema: str) -> sql.Composed:
        return sql.SQL(self.ddl_template).format(
            index=sql.Identifier(self.name),
            schema=sql.Identifier(schema),
            table=sql.Identifier(self.shape.table),
        )


RETRIEVAL_INDEX_SPECS = (
    RetrievalIndexSpec(
        name="idx_knowledge_objects_nb_name_trgm",
        legacy_name="idx_knowledge_objects_name_trgm",
        ddl_template=(
            "CREATE INDEX CONCURRENTLY {index} ON {schema}.{table} USING gin ("
            "notebook_id public.text_ops, "
            "(((payload ->> 'name') COLLATE \"C\")) public.gin_trgm_ops"
            ") WHERE status != 'deprecated'"
        ),
        shape=IndexShape(
            table="knowledge_objects",
            keys=("notebook_id", "(payload ->> 'name')"),
            predicate="status <> 'deprecated'",
            opclasses=("text_ops", "gin_trgm_ops"),
            collations=("column:notebook_id", "pg_catalog:C"),
        ),
        legacy_shape=IndexShape(
            table="knowledge_objects",
            keys=("(payload ->> 'name')",),
            predicate="",
            opclasses=("gin_trgm_ops",),
            collations=("pg_catalog:C",),
        ),
    ),
    RetrievalIndexSpec(
        name="idx_chunks_nb_text_trgm",
        legacy_name="idx_chunks_text_trgm",
        ddl_template=(
            "CREATE INDEX CONCURRENTLY {index} ON {schema}.{table} USING gin ("
            "notebook_id public.text_ops, text public.gin_trgm_ops)"
        ),
        shape=IndexShape(
            table="chunks",
            keys=("notebook_id", "text"),
            predicate="",
            opclasses=("text_ops", "gin_trgm_ops"),
            collations=("column:notebook_id", "column:text"),
        ),
        legacy_shape=IndexShape(
            table="chunks",
            keys=("text",),
            predicate="",
            opclasses=("gin_trgm_ops",),
            collations=("column:text",),
        ),
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
            application_name="silicon-notebook-retrieval-index-builder",
            connect_timeout=10,
        )
    except Exception:
        raise RetrievalIndexError("postgres_connection_failed") from None


def _index_row(connection, schema: str, name: str):
    return connection.execute(
        "SELECT i.indisvalid,i.indisready,i.indisunique,i.indnkeyatts,i.indnatts,"
        "tbl.relname AS table_name,tbl_ns.nspname AS table_schema,am.amname,"
        "pg_get_indexdef(i.indexrelid) AS indexdef,"
        "pg_get_expr(i.indpred,i.indrelid,true) AS predicate,"
        "ARRAY(SELECT pg_get_indexdef(i.indexrelid,n,true) "
        "FROM generate_series(1,i.indnkeyatts) AS n ORDER BY n) AS keys,"
        "ARRAY(SELECT opc.opcname FROM unnest(i.indclass::oid[]) WITH ORDINALITY op(oid,ord) "
        "JOIN pg_opclass opc ON opc.oid=op.oid ORDER BY op.ord) AS opclasses,"
        "ARRAY(SELECT ons.nspname FROM unnest(i.indclass::oid[]) WITH ORDINALITY op(oid,ord) "
        "JOIN pg_opclass opc ON opc.oid=op.oid "
        "JOIN pg_namespace ons ON ons.oid=opc.opcnamespace ORDER BY op.ord) AS opclass_schemas,"
        "i.indcollation::oid[] AS collation_oids "
        "FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid "
        "JOIN pg_namespace ns ON ns.oid=idx.relnamespace "
        "JOIN pg_class tbl ON tbl.oid=i.indrelid "
        "JOIN pg_namespace tbl_ns ON tbl_ns.oid=tbl.relnamespace "
        "JOIN pg_am am ON am.oid=idx.relam "
        "WHERE ns.nspname=%s AND idx.relname=%s",
        (schema, name),
    ).fetchone()


def _normalized_definition(value: str) -> str:
    return " ".join(
        (value or "").lower().replace("::text", "").replace("public.", "").split()
    )


def _expected_collation_oids(
    connection, schema: str, shape: IndexShape
) -> tuple[int, ...]:
    values: list[int] = []
    for expectation in shape.collations:
        kind, value = expectation.split(":", 1)
        if kind == "column":
            row = connection.execute(
                "SELECT a.attcollation AS oid FROM pg_attribute a "
                "JOIN pg_class t ON t.oid=a.attrelid "
                "JOIN pg_namespace n ON n.oid=t.relnamespace "
                "WHERE n.nspname=%s AND t.relname=%s AND a.attname=%s "
                "AND a.attnum>0 AND NOT a.attisdropped",
                (schema, shape.table, value),
            ).fetchone()
        elif kind == "pg_catalog":
            row = connection.execute(
                "SELECT c.oid FROM pg_collation c JOIN pg_namespace n "
                "ON n.oid=c.collnamespace WHERE n.nspname='pg_catalog' "
                "AND c.collname=%s",
                (value,),
            ).fetchone()
        else:
            raise ValueError("unsupported index collation expectation")
        if row is None:
            return ()
        values.append(int(row["oid"]))
    return tuple(values)


def _matches_shape(connection, schema: str, row, shape: IndexShape) -> bool:
    return bool(
        str(row["table_schema"]) == schema
        and str(row["table_name"]) == shape.table
        and str(row["amname"]) == "gin"
        and not bool(row["indisunique"])
        and int(row["indnkeyatts"]) == len(shape.keys)
        and int(row["indnatts"]) == len(shape.keys)
        and tuple(_normalized_definition(str(value)) for value in row["keys"] or ())
        == tuple(_normalized_definition(value) for value in shape.keys)
        and _normalized_definition(str(row["predicate"] or ""))
        == _normalized_definition(shape.predicate)
        and tuple(row["opclasses"] or ()) == shape.opclasses
        and tuple(row["opclass_schemas"] or ())
        == tuple("public" for _ in shape.opclasses)
        and tuple(int(value) for value in row["collation_oids"] or ())
        == _expected_collation_oids(connection, schema, shape)
    )


def _state(connection, schema: str, spec: RetrievalIndexSpec) -> dict[str, object]:
    row = _index_row(connection, schema, spec.name)
    if row is None:
        return {"name": spec.name, "state": "missing", "definition": ""}
    definition = _normalized_definition(str(row["indexdef"] or ""))
    if not _matches_shape(connection, schema, row, spec.shape):
        state = "unexpected"
    elif not bool(row["indisvalid"]) or not bool(row["indisready"]):
        state = "invalid"
    else:
        state = "ready"
    return {"name": spec.name, "state": state, "definition": definition}


def inspect_retrieval_indexes(database_url: str, *, schema: str = "public") -> dict:
    """Read the extension/index state without taking the build advisory lock."""
    schema = _schema(schema)
    with _connect(database_url) as connection:
        extensions = {
            row["extname"]: row["schema_name"]
            for row in connection.execute(
                "SELECT e.extname,n.nspname AS schema_name FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid=e.extnamespace "
                "WHERE e.extname=ANY(%s)",
                (["pg_trgm", "btree_gin"],),
            ).fetchall()
        }
        states = [_state(connection, schema, spec) for spec in RETRIEVAL_INDEX_SPECS]
    return {"schema": schema, "extensions": extensions, "indexes": states}


def _require_extensions(connection) -> None:
    row = connection.execute(
        "SELECT e.extname,n.nspname AS schema_name FROM pg_extension e "
        "JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pg_trgm'"
    ).fetchone()
    if row is None or row["schema_name"] != "public":
        raise RetrievalIndexError("public_pg_trgm_required")
    connection.execute("CREATE EXTENSION IF NOT EXISTS btree_gin WITH SCHEMA public")
    row = connection.execute(
        "SELECT n.nspname AS schema_name FROM pg_extension e "
        "JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='btree_gin'"
    ).fetchone()
    if row is None or row["schema_name"] != "public":
        raise RetrievalIndexError("public_btree_gin_required")


def install_retrieval_indexes(
    database_url: str,
    *,
    schema: str = "public",
    lock_timeout_seconds: int = 5,
    drop_legacy: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Build notebook-aware GIN indexes concurrently and recover invalid builds.

    The SQL predicates and ranking code are untouched; PostgreSQL merely gains an
    access path that intersects the mandatory notebook equality with trigram keys.
    A failed concurrent build can leave an invalid catalog row, so a rerun drops
    only that invalid owned name before retrying. A valid unexpected definition is
    never replaced automatically.
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
                    (RETRIEVAL_INDEX_LOCK_NAME,),
                ).fetchone()["locked"]
            )
            if not locked:
                raise RetrievalIndexError("retrieval_index_build_already_running")
            connection.execute(
                "SELECT set_config('statement_timeout','0',false),"
                "set_config('lock_timeout',%s,false)",
                (f"{lock_timeout_seconds}s",),
            )
            _require_extensions(connection)
            for spec in RETRIEVAL_INDEX_SPECS:
                state = _state(connection, schema, spec)
                if state["state"] == "ready":
                    emit(f"{spec.name}: already ready")
                    continue
                if state["state"] == "unexpected":
                    raise RetrievalIndexError(f"unexpected_index_definition:{spec.name}")
                if state["state"] == "invalid":
                    emit(f"{spec.name}: dropping invalid prior build")
                    connection.execute(
                        sql.SQL("DROP INDEX CONCURRENTLY {}.{}").format(
                            sql.Identifier(schema), sql.Identifier(spec.name)
                        )
                    )
                emit(f"{spec.name}: building concurrently")
                connection.execute(spec.ddl(schema))
                state = _state(connection, schema, spec)
                if state["state"] != "ready":
                    raise RetrievalIndexError(f"index_verification_failed:{spec.name}")
                emit(f"{spec.name}: ready")
            if drop_legacy:
                # Remove the write-amplifying global indexes only after both new
                # indexes passed catalog verification in this same session. Scan
                # every legacy definition before dropping the first one so one
                # DBA-customized name cannot cause a partially destructive run.
                for spec in RETRIEVAL_INDEX_SPECS:
                    if _state(connection, schema, spec)["state"] != "ready":
                        raise RetrievalIndexError("new_indexes_not_ready")
                legacy_to_drop: list[str] = []
                for legacy_spec in RETRIEVAL_INDEX_SPECS:
                    name = legacy_spec.legacy_name
                    legacy_row = _index_row(connection, schema, name)
                    if legacy_row is None:
                        emit(f"{name}: legacy global index already absent")
                        continue
                    if not _matches_shape(
                        connection, schema, legacy_row, legacy_spec.legacy_shape
                    ):
                        raise RetrievalIndexError(
                            f"unexpected_legacy_index_definition:{name}"
                        )
                    legacy_to_drop.append(name)
                for name in legacy_to_drop:
                    emit(f"{name}: dropping legacy global index concurrently")
                    connection.execute(
                        sql.SQL("DROP INDEX CONCURRENTLY {}.{}").format(
                            sql.Identifier(schema), sql.Identifier(name)
                        )
                    )
        except RetrievalIndexError:
            raise
        except psycopg.errors.LockNotAvailable:
            raise RetrievalIndexError("postgres_lock_timeout") from None
        except Exception:
            raise RetrievalIndexError("retrieval_index_build_failed") from None
        finally:
            try:
                connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                    (RETRIEVAL_INDEX_LOCK_NAME,),
                )
            except Exception:
                pass
    return inspect_retrieval_indexes(database_url, schema=schema)


def ready_index_names(state: dict) -> set[str]:
    return {
        str(row["name"])
        for row in state.get("indexes", ())
        if row.get("state") == "ready"
    }
