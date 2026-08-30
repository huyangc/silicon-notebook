from __future__ import annotations

from typing import Callable, ContextManager, Sequence

import numpy as np
from psycopg import sql

from app.repositories.postgres._store_utils import (
    execute_many,
    iso_timestamp,
    normalize_timestamp,
)
from app.domain.vector_index import decode_vector, encode_vector


_EMBEDDING_ID_COLUMNS = {
    "element_embeddings": "element_id",
    "knowledge_embeddings": "object_id",
    "relation_embeddings": "relation_id",
    "chunk_embeddings": "chunk_id",
}

# Page size for vector_pages' whole-notebook keyset scan (consumed by
# index_projection_store.embedding_matrix, object_ids=None). Deliberate parity
# with the SQLite side's own `_MATRIX_FETCH_BATCH`
# (app/repositories/sqlite/index_projection_store.py) — same offline
# build/fold memory diet, same page-bounds-transient-BLOBs argument — but NOT
# imported from there: adapters do not cross-import each other, so the two
# constants are independently defined and must be kept numerically equal by
# hand if either changes. See vector_pages' docstring for the three ways the
# PostgreSQL pagination shape differs from SQLite's.
_MATRIX_FETCH_BATCH = 10_000


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
        mark_dirty_in_tx: "Callable[[object, str], int] | None" = None,
    ) -> None:
        """``mark_dirty_in_tx``, when given, is invoked with THIS transaction's
        connection right after the vector rows commit — see
        ``SourceEmbeddingService.embed_knowledge``'s docstring (codex #638 R6
        P1) for why a vector REPLACE needs its own atomic seq bump while a
        first-time embed does not. None for every caller but that one
        (default preserves the old no-bump behavior byte-for-byte)."""
        self._replace_simple(
            "knowledge_embeddings", notebook_id, rows, created_at=created_at,
            mark_dirty_in_tx=mark_dirty_in_tx,
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
        mark_dirty_in_tx: "Callable[[object, str], int] | None" = None,
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
            if mark_dirty_in_tx is not None:
                mark_dirty_in_tx(connection, notebook_id)

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
    def vector_pages(db, notebook_id: str, table: str, id_col: str,
                      batch: int = _MATRIX_FETCH_BATCH):
        """Keyset-paginated whole-notebook vector read, generator of (vid,
        vector) pairs — the bounded replacement for the offline build/fold
        pipeline's whole-notebook load (index_projection_store.embedding_matrix,
        object_ids=None); `vector_rows` above stays the unbounded read for its
        other consumer, the query-time vector cache in
        retrieval_candidates._vector_matrix — bounded by SCENARIO, not by its
        caller: that branch only runs where no ANN index exists, which the
        copy-stats / warm-cache gates confine to small notebooks.

        Mirrors the SQLite side's `_stream_rows`
        (app/repositories/sqlite/index_projection_store.py) shape — each page
        an independent, fully-exhausted statement, never one held cursor for
        the whole scan — with three deliberate PostgreSQL differences, every
        one load-bearing at 9M-row scale:
          • Pagination key is the id column itself (`table`'s PK: object_id /
            chunk_id / relation_id / element_id, `C` collation text) — NOT a
            PostgreSQL analogue of SQLite rowid. PostgreSQL exposes no stable
            physical-row-order handle to page over across statements (ctid
            can change under VACUUM); the id column already is a stable,
            indexed total order, so `ORDER BY {id} LIMIT batch` / next page
            `AND {id} > last` is a plain PK index range scan — `notebook_id`
            applies as a residual heap filter. For a notebook holding the
            dominant share of the table (production: 99.1%) that filter is
            near-free and each page stops at LIMIT. This is NOT equivalent to
            SQLite's walk — there, `idx_{table}_nb` implicitly ends in rowid,
            so `notebook_id=? AND rowid>?` is a zero-over-scan seek at every
            share. Here, once a notebook's share drops below roughly
            sqrt(total_rows × batch), the planner flips to `idx_{table}_nb`
            plus a per-page top-N sort that re-reads the WHOLE notebook every
            page; total work stays bounded by about one full PK traversal
            (the flip only happens where the notebook is small), but it is a
            real minority-share cost, accepted because scale builds target
            dominant-share notebooks. If it ever matters, a `(notebook_id,
            {id})` composite belongs in an offline CREATE INDEX CONCURRENTLY
            maintenance path (the build_postgres_retrieval_indexes.py
            precedent), not a transactional startup migration (schema is
            currently v21).
          • No de-dup / no `seen` set: SQLite's rowid pagination must
            de-duplicate because `INSERT OR REPLACE` (the SQLite embedding
            writer) deletes+reinserts a refreshed row at a LARGER rowid, so a
            row refreshed mid-scan can re-surface in a later page under its
            old AND new rowid. Here the page key IS the id column, and
            `ON CONFLICT (id) DO UPDATE` (the PostgreSQL writer) updates the
            row in place — the id never moves in keyset order, so id-keyset
            pagination visits each id at most once by construction. Adding a
            `seen` set anyway would cost ~1GB+ for a 9M-row text-id set —
            spending the OOM-avoidance memory budget defending against a
            hazard that cannot occur on this backend. Cross-page drift under
            concurrent writes is tolerated, same ledger as SQLite: an id the
            cursor already passed keeps its scan-time vector even if
            re-embedded later (the refreshed vector is picked up by the next
            fold/rebuild), and a row inserted below the cursor is invisible
            this round; build_matrix's n_hint tolerates the row-count drift.
          • Snapshot released between pages: each page's SELECT is issued and
            fully fetched (execute + fetchall) before the next page's SELECT
            is issued — no single statement or transaction holds a streaming
            server-side cursor across the whole (potentially hours-long)
            build. A long-held read keeps its MVCC snapshot (and the xmin
            horizon) pinned for that whole duration, which blocks VACUUM from
            reclaiming dead tuples under concurrent writes — the PostgreSQL
            analogue of the SQLite side's WAL-checkpoint-blocking argument.
        `batch` rows below the page size ends the scan (matches SQLite's
        `len(page) < _MATRIX_FETCH_BATCH: break`); an id row's `vector`
        column is unwrapped from bytea/memoryview to bytes by
        `_compat_vector_rows` before it is yielded, same as every other
        reader in this module."""
        table_id, column_id = EmbeddingStore._table_identifiers(table, id_col)
        last_vid = None
        while True:
            if last_vid is None:
                statement = sql.SQL(
                    "SELECT {} AS vid,vector FROM {} WHERE notebook_id=%s "
                    "ORDER BY {} LIMIT %s"
                ).format(column_id, table_id, column_id)
                params = (notebook_id, batch)
            else:
                statement = sql.SQL(
                    "SELECT {} AS vid,vector FROM {} WHERE notebook_id=%s AND {} > %s "
                    "ORDER BY {} LIMIT %s"
                ).format(column_id, table_id, column_id, column_id)
                params = (notebook_id, last_vid, batch)
            page = _compat_vector_rows(db.execute(statement, params).fetchall())
            if not page:
                break
            for row in page:
                yield row["vid"], row["vector"]
            last_vid = page[-1]["vid"]
            if len(page) < batch:
                break

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
