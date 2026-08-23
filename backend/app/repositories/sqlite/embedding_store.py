from __future__ import annotations

from typing import Callable, ContextManager, Sequence

import sqlite3

from app.domain.vector_index import encode_vector


class EmbeddingStore:
    """Persistence for the four vector tables (element/knowledge/relation/
    chunk embeddings): one idempotent INSERT OR REPLACE batch per flush, one
    write transaction per flush.

    ``write`` is the facade's ``_write`` compatibility seat injected late
    (RepositoryRuntime.wire_persistence) and resolved at CALL time, so
    per-instance monkeypatches keep observing every vector flush — the same
    seat test_sqlite_write_optimization pins for _embed_objects_batch's
    one-transaction batch semantics. The seat itself delegates to the shared
    database write lock, so vector writes stay serialized with every other
    writer. Vector COMPUTE (embedder batching/concurrency) stays in the
    facade; rows arrive here as (id, vector) pairs and are encoded with
    encode_vector (float32 BLOB)."""

    def __init__(
        self, *, write: Callable[[], ContextManager[sqlite3.Connection]]
    ) -> None:
        self.write = write

    def bind_write(self, write: Callable) -> None:
        self.write = write

    def replace_element_vectors(
        self,
        source_id: str,
        notebook_id: str,
        rows: Sequence[tuple],
        *,
        created_at: str,
    ) -> None:
        if not rows:
            return
        with self.write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO element_embeddings "
                "(element_id, source_id, notebook_id, vector, created_at) VALUES (?,?,?,?,?)",
                [(eid, source_id, notebook_id, encode_vector(vec), created_at)
                 for eid, vec in rows],
            )

    def replace_knowledge_vectors(
        self,
        notebook_id: str,
        rows: Sequence[tuple],
        *,
        created_at: str,
    ) -> None:
        if not rows:
            return
        with self.write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO knowledge_embeddings "
                "(object_id, notebook_id, vector, created_at) VALUES (?,?,?,?)",
                [(oid, notebook_id, encode_vector(vec), created_at)
                 for oid, vec in rows],
            )

    def replace_relation_vectors(
        self,
        notebook_id: str,
        rows: Sequence[tuple],
        *,
        created_at: str,
    ) -> None:
        if not rows:
            return
        with self.write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO relation_embeddings "
                "(relation_id, notebook_id, vector, created_at) VALUES (?,?,?,?)",
                [(rid, notebook_id, encode_vector(vec), created_at)
                 for rid, vec in rows],
            )

    def replace_chunk_vectors(
        self,
        notebook_id: str,
        rows: Sequence[tuple],
        *,
        created_at: str,
    ) -> None:
        if not rows:
            return
        with self.write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO chunk_embeddings "
                "(chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                [(cid, notebook_id, encode_vector(vec), created_at)
                 for cid, vec in rows],
            )

    @staticmethod
    def version_row(db, notebook_id: str, table: str):
        return db.execute(
            f"SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts "
            f"FROM {table} WHERE notebook_id = ?", (notebook_id,),
        ).fetchone()

    @staticmethod
    def vector_rows(db, notebook_id: str, table: str, id_col: str):
        return db.execute(
            f"SELECT {id_col} AS vid, vector FROM {table} WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def vector_rows_for_ids(db, notebook_id: str, table: str, id_col: str, ids):
        values = list(ids)
        if not values:
            return []
        ph = ",".join("?" for _ in values)
        return db.execute(
            f"SELECT {id_col} AS vid, vector FROM {table} "
            f"WHERE notebook_id = ? AND {id_col} IN ({ph})",
            (notebook_id, *values),
        ).fetchall()

    @staticmethod
    def relation_delta_rows(db, notebook_id: str, source_ids):
        values = list(source_ids)
        if not values:
            return []
        ph = ",".join("?" for _ in values)
        return db.execute(
            f"SELECT relation_id AS vid, vector FROM relation_embeddings "
            f"WHERE notebook_id=? AND relation_id IN "
            f"(SELECT id FROM knowledge_relations WHERE notebook_id=? AND source_id IN ({ph}))",
            (notebook_id, notebook_id, *values),
        ).fetchall()

    @staticmethod
    def knowledge_delta_rows(db, notebook_id: str, source_ids):
        values = list(source_ids)
        if not values:
            return []
        ph = ",".join("?" for _ in values)
        return db.execute(
            f"SELECT object_id AS vid, vector FROM knowledge_embeddings "
            f"WHERE notebook_id=? AND object_id IN "
            f"(SELECT id FROM knowledge_objects WHERE notebook_id=? AND source_id IN ({ph}))",
            (notebook_id, notebook_id, *values),
        ).fetchall()

    @staticmethod
    def element_delta_rows(db, notebook_id: str, source_ids):
        values = list(source_ids)
        if not values:
            return []
        ph = ",".join("?" for _ in values)
        return db.execute(
            f"SELECT element_id AS vid, vector FROM element_embeddings "
            f"WHERE notebook_id=? AND source_id IN ({ph})",
            (notebook_id, *values),
        ).fetchall()

    @staticmethod
    def chunk_delta_rows(db, notebook_id: str, source_ids):
        values = list(source_ids)
        if not values:
            return []
        ph = ",".join("?" for _ in values)
        return db.execute(
            f"SELECT chunk_id AS vid, vector FROM chunk_embeddings "
            f"WHERE notebook_id=? AND chunk_id IN "
            f"(SELECT id FROM chunks WHERE notebook_id=? AND source_id IN ({ph}))",
            (notebook_id, notebook_id, *values),
        ).fetchall()

    @staticmethod
    def rows_by_ids(db, table: str, id_col: str, ids):
        values = list(ids)
        if not values:
            return []
        ph = ",".join("?" for _ in values)
        return db.execute(
            f"SELECT {id_col} AS vid, vector FROM {table} WHERE {id_col} IN ({ph})",
            values,
        ).fetchall()

    @staticmethod
    def embedded_object_ids(db, notebook_id: str) -> set:
        """object_ids that already hold a knowledge-payload vector (Task 26:
        the backfill "have" probe, moved verbatim from the facade)."""
        return {
            row["object_id"]
            for row in db.execute(
                "SELECT object_id FROM knowledge_embeddings "
                "WHERE notebook_id = ? AND vector IS NOT NULL",
                (notebook_id,),
            ).fetchall()
        }

    @staticmethod
    def embedded_relation_ids(db, notebook_id: str) -> set:
        """relation_ids that already hold a relation vector (Task 26)."""
        return {
            row["relation_id"]
            for row in db.execute(
                "SELECT relation_id FROM relation_embeddings WHERE notebook_id=?",
                (notebook_id,),
            ).fetchall()
        }
