from __future__ import annotations

from typing import Callable, ContextManager, Sequence

import sqlite3

from app.domain.vector_index import encode_vector
from app.repositories.sqlite.id_binding import bind_ids, drive_by


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
        mark_dirty_in_tx: "Callable[[object, str], int] | None" = None,
    ) -> None:
        """``mark_dirty_in_tx``, when given, is invoked with THIS transaction's
        connection right after the vector rows commit — see
        ``SourceEmbeddingService.embed_knowledge``'s docstring (codex #638 R6
        P1) for why a vector REPLACE needs its own atomic seq bump while a
        first-time embed does not. None for every caller but that one
        (default preserves the old no-bump behavior byte-for-byte)."""
        if not rows:
            return
        with self.write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO knowledge_embeddings "
                "(object_id, notebook_id, vector, created_at) VALUES (?,?,?,?)",
                [(oid, notebook_id, encode_vector(vec), created_at)
                 for oid, vec in rows],
            )
            if mark_dirty_in_tx is not None:
                mark_dirty_in_tx(db, notebook_id)

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

    def insert_missing_knowledge_vectors(
        self,
        notebook_id: str,
        rows: Sequence[tuple],
        *,
        created_at: str,
    ) -> int:
        """Insert ``(object_id, payload, vector)`` rows, each only while the object
        still has exactly that stored ``payload`` (the text the vector was computed
        from) and still has no vector; returns how many were written. Check and write
        are one statement in one write transaction: a backfill never overwrites a
        vector some other writer (an edit's re-embed, a build) stored meanwhile, and
        never attaches a vector computed from a payload that has since changed."""
        if not rows:
            return 0
        written = 0
        with self.write() as db:
            for object_id, payload, vector in rows:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO knowledge_embeddings "
                    "(object_id,notebook_id,vector,created_at) "
                    "SELECT ?,?,?,? WHERE EXISTS "
                    "(SELECT 1 FROM knowledge_objects WHERE id=? AND payload=?) "
                    "AND NOT EXISTS (SELECT 1 FROM knowledge_embeddings WHERE object_id=?)",
                    (object_id, notebook_id, encode_vector(vector), created_at,
                     object_id, payload, object_id),
                )
                written += max(0, cursor.rowcount)
        return written

    def insert_missing_relation_vectors(
        self,
        notebook_id: str,
        rows: Sequence[tuple],
        *,
        created_at: str,
    ) -> int:
        """Insert ``(relation_id, (source_object_id, target_object_id, edge_type,
        evidence), vector)`` rows, each only while the relation row still holds exactly
        those columns and still has no vector; returns how many were written. One
        statement per row in one write transaction, so a relation rebuilt or
        re-embedded meanwhile keeps its own vector, and a deleted relation is skipped
        instead of failing the foreign key. (The relation text also carries the two
        endpoint names; no writer re-embeds a relation when an endpoint is renamed, so
        that part of the text is not a freshness invariant any writer keeps.)"""
        if not rows:
            return 0
        written = 0
        with self.write() as db:
            for relation_id, (source, target, edge_type, evidence), vector in rows:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO relation_embeddings "
                    "(relation_id,notebook_id,vector,created_at) "
                    "SELECT ?,?,?,? WHERE EXISTS (SELECT 1 FROM knowledge_relations "
                    "WHERE id=? AND source_object_id=? AND target_object_id=? "
                    "AND edge_type=? AND evidence=?) "
                    "AND NOT EXISTS (SELECT 1 FROM relation_embeddings WHERE relation_id=?)",
                    (relation_id, notebook_id, encode_vector(vector), created_at,
                     relation_id, source, target, edge_type, evidence, relation_id),
                )
                written += max(0, cursor.rowcount)
        return written

    def replace_chunk_vectors_if_text(
        self,
        notebook_id: str,
        rows: Sequence[tuple],
        *,
        created_at: str,
    ) -> int:
        """Upsert ``(chunk_id, text, vector)`` rows, each only if the chunk still exists
        with exactly ``text``; returns how many were written. The check and the write are
        one statement in one write transaction, so a chunk rewritten (or deleted with its
        table) after its text was embedded gets no vector and raises no FK error."""
        if not rows:
            return 0
        written = 0
        with self.write() as db:
            for chunk_id, text, vector in rows:
                cursor = db.execute(
                    "INSERT OR REPLACE INTO chunk_embeddings "
                    "(chunk_id,notebook_id,vector,created_at) "
                    "SELECT ?,?,?,? WHERE EXISTS "
                    "(SELECT 1 FROM chunks WHERE id=? AND text=?)",
                    (chunk_id, notebook_id, encode_vector(vector), created_at,
                     chunk_id, text),
                )
                written += max(0, cursor.rowcount)
        return written

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
    def relation_delta_rows(db, notebook_id: str, source_ids, *, with_source_id: bool = False):
        """Relation vectors of the relations extracted from ``source_ids``.

        ``with_source_id`` (PR-E2): ``False`` sends the historical statement
        byte for byte; ``True`` also returns each relation's ``source_id``
        (a join instead of the ``IN`` subquery) so the Memory relation cache
        maps one batch of sources in one statement.  The source ids drive
        ``idx_knowledge_relations_source`` (both notebook predicates carry a
        unary ``+``: without statistics a notebook-prefixed index would
        otherwise win and walk the whole notebook) and the vectors are read by primary
        key, in every statistics state
        (``tests/test_store_evidence_ceiling_plans.py``)."""
        values = list(source_ids)
        if not values:
            return []
        ph = ",".join("?" for _ in values)
        if with_source_id:
            return db.execute(
                f"SELECT re.relation_id AS vid, re.vector, kr.source_id "
                f"FROM knowledge_relations kr "
                f"JOIN relation_embeddings re ON re.relation_id=kr.id "
                f"WHERE +kr.notebook_id=? AND kr.source_id IN ({ph}) AND +re.notebook_id=?",
                (notebook_id, *values, notebook_id),
            ).fetchall()
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
        # A knowhow re-projection passes every unchanged chunk of the table:
        # one JSON parameter, each id a primary-key seek (``drive_by``).
        bound = bind_ids(values)
        return db.execute(
            f"SELECT {id_col} AS vid, vector FROM {table} "
            f"WHERE {drive_by(id_col, bound)}",
            (bound.param,),
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
