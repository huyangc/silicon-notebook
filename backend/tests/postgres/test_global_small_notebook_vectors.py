from __future__ import annotations

import pytest

from app.domain.vector_index import encode_vector, decode_vector
from app.repositories.postgres.embedding_store import EmbeddingStore


pytestmark = pytest.mark.postgres_integration


def test_small_library_vector_pages_apply_size_and_source_gates(postgres_database):
    database = postgres_database
    with database.write() as db:
        db.execute("CREATE TABLE chunks(id text PRIMARY KEY,notebook_id text,source_id text)")
        db.execute("CREATE INDEX chunks_notebook ON chunks(notebook_id,id)")
        db.execute("CREATE TABLE chunk_embeddings(chunk_id text PRIMARY KEY,notebook_id text,vector bytea)")
        for identifier, notebook, source in (
            ("a", "personal", "visible"), ("b", "personal", "private"),
            ("c", "personal", "visible"), ("d", "different", "visible"),
        ):
            db.execute("INSERT INTO chunks VALUES (%s,%s,%s)", (identifier, notebook, source))
            db.execute("INSERT INTO chunk_embeddings VALUES (%s,%s,%s)",
                       (identifier, notebook, encode_vector([1., 0.])))
    with database.connect() as db:
        admitted, first = EmbeddingStore.global_small_chunk_vector_page(
            db, "personal", allowed_source_ids=["visible"], max_chunks=3, after="", page_size=1,
        )
        assert admitted and [row["vid"] for row in first] == ["a"]
        assert decode_vector(first[0]["vector"]).tolist() == [1., 0.]
        admitted, second = EmbeddingStore.global_small_chunk_vector_page(
            db, "personal", allowed_source_ids=["visible"], max_chunks=3, after="a", page_size=1,
        )
        assert admitted and [row["vid"] for row in second] == ["c"]
        admitted, empty = EmbeddingStore.global_small_chunk_vector_page(
            db, "personal", allowed_source_ids=["visible"], max_chunks=3, after="c", page_size=1,
        )
        assert admitted and empty == []
        # Count all notebook chunks, including sources outside this request.
        admitted, denied = EmbeddingStore.global_small_chunk_vector_page(
            db, "personal", allowed_source_ids=["visible"], max_chunks=2, after="", page_size=10,
        )
        assert not admitted and denied == []
