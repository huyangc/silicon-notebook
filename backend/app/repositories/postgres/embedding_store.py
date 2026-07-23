from __future__ import annotations

from typing import Callable, ContextManager, Sequence

import numpy as np
from psycopg import sql

from app.repositories.postgres._store_utils import (
    execute_many,
    iso_timestamp,
    normalize_timestamp,
)
from app.services.vector_index import decode_vector, encode_vector


_EMBEDDING_ID_COLUMNS = {
    "element_embeddings": "element_id",
    "knowledge_embeddings": "object_id",
    "relation_embeddings": "relation_id",
    "chunk_embeddings": "chunk_id",
}


def _validated_vector(value: object, *, dimension: int | None) -> tuple[bytes, int]:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if not raw or len(raw) % np.dtype(np.float32).itemsize:
            raise ValueError("embedding byte length must be a non-zero multiple of float32")
        array = decode_vector(raw)
        if array is None:  # guarded above; keeps the canonical decoder contract explicit
            raise ValueError("embedding vector must not be empty")
    else:
        array = np.asarray(value, dtype=np.float32)
    if array.ndim != 1 or array.size == 0:
        raise ValueError("embedding vector must be a non-empty one-dimensional array")
    if not np.isfinite(array).all():
        raise ValueError("embedding vector must contain only finite values")
    current_dimension = int(array.size)
    if dimension is not None and current_dimension != dimension:
        raise ValueError("embedding batch contains inconsistent dimensions")
    encoded = encode_vector(array)
    if len(encoded) != current_dimension * np.dtype(np.float32).itemsize:
        raise ValueError("embedding byte length does not match float32 dimension")
    return encoded, current_dimension


def _compat_vector_rows(rows):
    output = []
    for row in rows:
        item = dict(row)
        raw = item.get("vector")
        if isinstance(raw, memoryview):
            item["vector"] = raw.tobytes()
        output.append(item)
    return output


class EmbeddingStore:
    """PostgreSQL persistence for raw float32 bytea embeddings."""

    def __init__(self, *, write: Callable[[], ContextManager]) -> None:
        self.write = write

    def bind_write(self, write: Callable) -> None:
        self.write = write

    @staticmethod
    def _encoded_rows(rows: Sequence[tuple]) -> list[tuple[object, bytes]]:
        output: list[tuple[object, bytes]] = []
        dimension: int | None = None
        for row_id, vector in rows:
            encoded, current_dimension = _validated_vector(
                vector, dimension=dimension
            )
            dimension = current_dimension
            output.append((row_id, encoded))
        return output

    def replace_element_vectors(
        self,
        source_id: str,
        notebook_id: str,
        rows: Sequence[tuple],
        *,
        created_at: str,
    ) -> None:
        encoded = self._encoded_rows(rows)
        if not encoded:
            return
        created = normalize_timestamp(created_at)
        with self.write() as connection:
            execute_many(
                connection,
                "INSERT INTO element_embeddings "
                "(element_id,source_id,notebook_id,vector,created_at) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (element_id) DO UPDATE SET "
                "source_id=EXCLUDED.source_id,notebook_id=EXCLUDED.notebook_id,"
                "vector=EXCLUDED.vector,created_at=EXCLUDED.created_at",
                [(row_id, source_id, notebook_id, vector, created) for row_id, vector in encoded],
            )

    def replace_knowledge_vectors(
        self,
        notebook_id: str,
        rows: Sequence[tuple],
        *,
        created_at: str,
    ) -> None:
        self._replace_simple(
            "knowledge_embeddings", notebook_id, rows, created_at=created_at
        )

    def replace_relation_vectors(
        self,
        notebook_id: str,
        rows: Sequence[tuple],
        *,
        created_at: str,
    ) -> None:
        self._replace_simple(
            "relation_embeddings", notebook_id, rows, created_at=created_at
        )

    def replace_chunk_vectors(
        self,
        notebook_id: str,
        rows: Sequence[tuple],
        *,
        created_at: str,
    ) -> None:
        self._replace_simple(
            "chunk_embeddings", notebook_id, rows, created_at=created_at
        )

    def _replace_simple(
        self,
        table: str,
        notebook_id: str,
        rows: Sequence[tuple],
        *,
        created_at: str,
    ) -> None:
        encoded = self._encoded_rows(rows)
        if not encoded:
            return
        id_column = _EMBEDDING_ID_COLUMNS[table]
        statement = sql.SQL(
            "INSERT INTO {} ({},notebook_id,vector,created_at) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT ({}) DO UPDATE SET "
            "notebook_id=EXCLUDED.notebook_id,vector=EXCLUDED.vector,"
            "created_at=EXCLUDED.created_at"
        ).format(
            sql.Identifier(table),
            sql.Identifier(id_column),
            sql.Identifier(id_column),
        )
        created = normalize_timestamp(created_at)
        with self.write() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(
                    statement,
                    [(row_id, notebook_id, vector, created) for row_id, vector in encoded],
                )

    @staticmethod
    def _table_identifiers(table: str, id_col: str):
        if _EMBEDDING_ID_COLUMNS.get(table) != id_col:
            raise ValueError("unsupported PostgreSQL embedding table identifier")
        return sql.Identifier(table), sql.Identifier(id_col)

    @staticmethod
    def version_row(db, notebook_id: str, table: str):
        id_col = _EMBEDDING_ID_COLUMNS.get(table)
        if id_col is None:
            raise ValueError("unsupported PostgreSQL embedding table identifier")
        statement = sql.SQL(
            "SELECT COUNT(*) AS c, MAX(created_at) AS ts FROM {} WHERE notebook_id=%s"
        ).format(sql.Identifier(table))
        row = dict(db.execute(statement, (notebook_id,)).fetchone())
        row["ts"] = iso_timestamp(row["ts"])
        return row

    @staticmethod
    def vector_rows(db, notebook_id: str, table: str, id_col: str):
        table_id, column_id = EmbeddingStore._table_identifiers(table, id_col)
        rows = db.execute(
            sql.SQL("SELECT {} AS vid,vector FROM {} WHERE notebook_id=%s").format(
                column_id, table_id
            ),
            (notebook_id,),
        ).fetchall()
        return _compat_vector_rows(rows)

    @staticmethod
    def vector_rows_for_ids(db, notebook_id: str, table: str, id_col: str, ids):
        values = list(ids)
        if not values:
            return []
        table_id, column_id = EmbeddingStore._table_identifiers(table, id_col)
        rows = db.execute(
            sql.SQL(
                "SELECT {} AS vid,vector FROM {} WHERE notebook_id=%s AND {}=ANY(%s)"
            ).format(column_id, table_id, column_id),
            (notebook_id, values),
        ).fetchall()
        return _compat_vector_rows(rows)

    @staticmethod
    def relation_delta_rows(db, notebook_id: str, source_ids):
        values = list(source_ids)
        if not values:
            return []
        rows = db.execute(
            "SELECT relation_id AS vid,vector FROM relation_embeddings "
            "WHERE notebook_id=%s AND relation_id IN "
            "(SELECT id FROM knowledge_relations WHERE notebook_id=%s AND source_id=ANY(%s))",
            (notebook_id, notebook_id, values),
        ).fetchall()
        return _compat_vector_rows(rows)

    @staticmethod
    def knowledge_delta_rows(db, notebook_id: str, source_ids):
        values = list(source_ids)
        if not values:
            return []
        rows = db.execute(
            "SELECT object_id AS vid,vector FROM knowledge_embeddings "
            "WHERE notebook_id=%s AND object_id IN "
            "(SELECT id FROM knowledge_objects WHERE notebook_id=%s AND source_id=ANY(%s))",
            (notebook_id, notebook_id, values),
        ).fetchall()
        return _compat_vector_rows(rows)

    @staticmethod
    def element_delta_rows(db, notebook_id: str, source_ids):
        values = list(source_ids)
        if not values:
            return []
        rows = db.execute(
            "SELECT element_id AS vid,vector FROM element_embeddings "
            "WHERE notebook_id=%s AND source_id=ANY(%s)",
            (notebook_id, values),
        ).fetchall()
        return _compat_vector_rows(rows)

    @staticmethod
    def chunk_delta_rows(db, notebook_id: str, source_ids):
        values = list(source_ids)
        if not values:
            return []
        rows = db.execute(
            "SELECT chunk_id AS vid,vector FROM chunk_embeddings "
            "WHERE notebook_id=%s AND chunk_id IN "
            "(SELECT id FROM chunks WHERE notebook_id=%s AND source_id=ANY(%s))",
            (notebook_id, notebook_id, values),
        ).fetchall()
        return _compat_vector_rows(rows)

    @staticmethod
    def rows_by_ids(db, table: str, id_col: str, ids):
        values = list(ids)
        if not values:
            return []
        table_id, column_id = EmbeddingStore._table_identifiers(table, id_col)
        rows = db.execute(
            sql.SQL("SELECT {} AS vid,vector FROM {} WHERE {}=ANY(%s)").format(
                column_id, table_id, column_id
            ),
            (values,),
        ).fetchall()
        return _compat_vector_rows(rows)

    @staticmethod
    def embedded_object_ids(db, notebook_id: str) -> set:
        return {
            row["object_id"]
            for row in db.execute(
                "SELECT object_id FROM knowledge_embeddings "
                "WHERE notebook_id=%s AND vector IS NOT NULL",
                (notebook_id,),
            ).fetchall()
        }

    @staticmethod
    def embedded_relation_ids(db, notebook_id: str) -> set:
        return {
            row["relation_id"]
            for row in db.execute(
                "SELECT relation_id FROM relation_embeddings WHERE notebook_id=%s",
                (notebook_id,),
            ).fetchall()
        }
