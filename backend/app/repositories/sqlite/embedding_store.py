from __future__ import annotations

from typing import Callable, ContextManager, Sequence

import sqlite3

from app.services.vector_index import encode_vector


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
