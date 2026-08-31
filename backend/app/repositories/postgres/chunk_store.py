from __future__ import annotations

import json
from typing import Sequence

from app.repositories.chunk_elements import reverse_rows_for_writes
from app.repositories.ports import ChunkWrite
from app.repositories.postgres._store_utils import (
    TimestampInput,
    execute_many,
    iso_timestamp,
    json_value,
    jsonb,
    normalize_timestamp,
    placeholders,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.source_store import VISIBLE_SOURCE_TYPES_PREDICATE
from app.repositories.postgres.search import chunk_section_rows
from app.domain.vector_index import encode_vector
from app.domain.indexing_pipeline import IndexingPipelineStalePlanError


# Bounded fan-out for the element -> chunk point lookup. Deliberately a local
# constant with the same value as the SQLite adapter's: adapters never import
# one another, and a fixed batch keeps the statement shape stable no matter how
# many evidence elements one query hit.
CHUNK_ELEMENT_LOOKUP_BATCH = 500


def _compat_element_ids(row: dict) -> dict:
    result = dict(row)
    if "element_ids" in result and not isinstance(result["element_ids"], str):
        result["element_ids"] = json.dumps(result["element_ids"] or [])
    return result


class ChunkStore:
    """PostgreSQL chunk persistence; search indexes derive from base rows."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def question_index_chunk_page(
        self,
        notebook_id: str,
        *,
        after_id: str,
        limit: int,
        include_existing: bool,
    ) -> list[dict]:
        existing = "" if include_existing else "AND c.question_indexed_at IS NULL "
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT c.id AS chunk_id,c.source_id,c.text,c.section_path "
                "FROM chunks c WHERE c.notebook_id=%s AND c.id>%s "
                f"{existing}ORDER BY c.id COLLATE \"C\" LIMIT %s",
                (notebook_id, after_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_chunk_questions(
        self,
        chunk_id: str,
        notebook_id: str,
        source_id: str,
        rows: Sequence[tuple[str, str, object]],
        *,
        created_at: str,
    ) -> None:
        created = normalize_timestamp(created_at)
        encoded = [
            (question_id, chunk_id, notebook_id, source_id, question,
             encode_vector(vector), created)
            for question_id, question, vector in rows
        ]
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM chunk_questions WHERE chunk_id=%s", (chunk_id,)
            )
            execute_many(
                connection,
                "INSERT INTO chunk_questions "
                "(id,chunk_id,notebook_id,source_id,question,vector,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                encoded,
            )
            connection.execute(
                "UPDATE chunks SET question_indexed_at=%s WHERE id=%s",
                (created, chunk_id),
            )

    def question_index_rows(
        self,
        notebook_id: str,
        *,
        actor_id: str,
        allowed_source_ids: Sequence[str] | None,
        limit: int,
    ) -> list[dict]:
        params: list[object] = [notebook_id, actor_id]
        source_clause = ""
        if allowed_source_ids is not None:
            source_ids = list(dict.fromkeys(allowed_source_ids))
            if not source_ids:
                return []
            source_clause = "AND q.source_id=ANY(%s) "
            params.append(source_ids)
        params.append(int(limit))
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT q.id,q.chunk_id,q.source_id,q.vector "
                "FROM chunk_questions q JOIN chunks c "
                "ON c.id=q.chunk_id AND c.notebook_id=q.notebook_id "
                "AND c.source_id=q.source_id JOIN sources s "
                "ON s.id=c.source_id AND s.notebook_id=c.notebook_id "
                "WHERE q.notebook_id=%s AND (s.source_type!='memory' OR EXISTS ("
                "SELECT 1 FROM memory_items m WHERE m.id=s.memory_id "
                "AND m.notebook_id=q.notebook_id AND m.created_by=%s)) "
                + source_clause + "ORDER BY q.id COLLATE \"C\" LIMIT %s",
                params,
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            if isinstance(item.get("vector"), memoryview):
                item["vector"] = item["vector"].tobytes()
            output.append(item)
        return output

    def question_index_stats(self, notebook_id: str) -> dict[str, int]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM chunk_questions WHERE notebook_id=%s) "
                "AS questions,"
                "(SELECT COUNT(*) FROM chunks WHERE notebook_id=%s "
                "AND question_indexed_at IS NOT NULL) AS chunks,"
                "(SELECT COUNT(DISTINCT chunk_id) FROM chunk_questions "
                "WHERE notebook_id=%s) AS question_chunks",
                (notebook_id, notebook_id, notebook_id),
            ).fetchone()
        return {
            "questions": int(row["questions"]),
            "chunks": int(row["chunks"]),
            "question_chunks": int(row["question_chunks"]),
        }

    @staticmethod
    def ids_for_sources(
        connection,
        notebook_id: str,
        source_ids: Sequence[str],
        *,
        presence_only: bool = False,
    ):
        values = list(dict.fromkeys(source_ids))
        if not values:
            return []
        if presence_only:
            return connection.execute(
                "SELECT requested.source_id FROM "
                "unnest(%s::text[]) WITH ORDINALITY "
                "AS requested(source_id, ordinal) "
                "WHERE EXISTS (SELECT 1 FROM chunks c "
                "WHERE c.notebook_id=%s "
                "AND c.source_id=requested.source_id) "
                "ORDER BY requested.ordinal",
                (values, notebook_id),
            ).fetchall()
        return connection.execute(
            "SELECT id FROM chunks WHERE notebook_id=%s AND source_id=ANY(%s)",
            (notebook_id, values),
        ).fetchall()

    def source_elements_for_chunking(self, source_id: str) -> list[dict]:
        """额外带出 metadata 里的 caption、description 与 section_path，语义与 SQLite 侧
        ChunkStore.source_elements_for_chunking 逐字对等：section_path 是
        markdown 解析路径存的完整标题面包屑（含自身、" > " 分隔），供
        build_chunks 的 heading 分支代替标题自身文本作 section 标签；缺省时
        build_chunks 自行回退到标题自身文本，字节不变。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,element_type,text,metadata FROM source_elements "
                "WHERE source_id=%s ORDER BY id COLLATE \"C\"",
                (source_id,),
            ).fetchall()
        output = []
        for row in rows:
            metadata = json_value(row["metadata"], {})
            is_dict = isinstance(metadata, dict)
            caption = str(metadata.get("caption") or "") if is_dict else ""
            description = str(metadata.get("description") or "") if is_dict else ""
            section_path = str(metadata.get("section_path") or "") if is_dict else ""
            output.append(
                {
                    "id": row["id"],
                    "element_type": row["element_type"],
                    "text": row["text"],
                    "caption": caption,
                    "description": description,
                    "section_path": section_path,
                }
            )
        return output

    def replace_source_chunks(
        self,
        source_id: str,
        notebook_id: str,
        chunks: Sequence[ChunkWrite],
        *,
        created_at: TimestampInput,
        mark_chunked_at: TimestampInput | None = None,
    ) -> None:
        created_at = normalize_timestamp(created_at)
        rows = [
            (
                chunk.id,
                notebook_id,
                source_id,
                chunk.text,
                chunk.section_path,
                jsonb(list(chunk.element_ids)),
                created_at,
            )
            for chunk in chunks
        ]
        with self.database.write() as connection:
            connection.execute("DELETE FROM chunks WHERE source_id=%s", (source_id,))
            execute_many(
                connection,
                "INSERT INTO chunks"
                "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )
            self._insert_fts_rows(
                connection, [(row[0], row[1], row[3]) for row in rows]
            )
            # element -> chunk reverse rows, same transaction as the chunk rows
            # they describe. The old rows are already gone: chunk_elements has
            # ``REFERENCES chunks(id) ON DELETE CASCADE``, so the DELETE above
            # took them with it.
            self._insert_chunk_element_rows(connection, notebook_id, chunks)
            if mark_chunked_at is not None:
                connection.execute(
                    "UPDATE sources SET chunked_at=%s WHERE id=%s",
                    (normalize_timestamp(mark_chunked_at), source_id),
                )

    def _insert_fts_rows(self, connection, rows: list) -> None:
        # PostgreSQL's GIN/trigram indexes update with chunks themselves.
        del connection, rows

    @staticmethod
    def chunk_element_rows(
        notebook_id: str, chunks: Sequence[ChunkWrite]
    ) -> list[tuple[str, str, str]]:
        """``(notebook_id, element_id, chunk_id)`` reverse rows for these writes.

        Shaping (including de-duplication within the batch) is the shared,
        backend-neutral helper the offline backfill also uses, so a chunk
        written online and the same chunk projected offline produce byte-for-byte
        identical rows."""
        return reverse_rows_for_writes(
            notebook_id, [(chunk.id, list(chunk.element_ids)) for chunk in chunks]
        )

    def _insert_chunk_element_rows(
        self, connection, notebook_id: str, chunks: Sequence[ChunkWrite]
    ) -> None:
        rows = self.chunk_element_rows(notebook_id, chunks)
        if rows:
            execute_many(
                connection,
                "INSERT INTO chunk_elements (notebook_id,element_id,chunk_id) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                rows,
            )

    @staticmethod
    def chunks_for_element_ids(
        connection, notebook_id: str, element_ids: Sequence[str]
    ):
        """``(element_id, chunk_id)`` rows for these elements, in chunk order.

        The fast half of the element -> chunk reverse lookup: an indexed seek
        on the ``(notebook_id, element_id, chunk_id)`` primary key instead of a
        whole-notebook chunk scan with per-row JSON decoding.

        ``ORDER BY c.ordinal`` is the PostgreSQL counterpart of SQLite's
        ``rowid`` insertion order (see POSTGRES_ROWID_ORDINAL_TABLES)."""
        ids = list(dict.fromkeys(e for e in element_ids if e))
        rows: list = []
        for offset in range(0, len(ids), CHUNK_ELEMENT_LOOKUP_BATCH):
            batch = ids[offset : offset + CHUNK_ELEMENT_LOOKUP_BATCH]
            rows.extend(
                connection.execute(
                    "SELECT ce.element_id AS element_id, ce.chunk_id AS chunk_id "
                    "FROM chunk_elements ce JOIN chunks c ON c.id = ce.chunk_id "
                    "WHERE ce.notebook_id=%s AND ce.element_id=ANY(%s) "
                    "ORDER BY c.ordinal",
                    (notebook_id, batch),
                ).fetchall()
            )
        return rows

    def rows_by_id_prefix(self, connection, source_id: str, id_prefix: str) -> list:
        return connection.execute(
            "SELECT id,text,section_path FROM chunks "
            "WHERE source_id=%s AND id LIKE %s ORDER BY id COLLATE \"C\"",
            (source_id, f"{id_prefix}%"),
        ).fetchall()

    def delete_by_ids(self, connection, chunk_ids: Sequence[str]) -> None:
        ids = list(chunk_ids)
        if ids:
            connection.execute("DELETE FROM chunks WHERE id=ANY(%s)", (ids,))

    def insert_rows(
        self,
        connection,
        notebook_id: str,
        source_id: str,
        rows: Sequence[ChunkWrite],
        *,
        created_at: TimestampInput,
    ) -> None:
        created_at = normalize_timestamp(created_at)
        values = [
            (
                row.id,
                notebook_id,
                source_id,
                row.text,
                row.section_path,
                jsonb(list(row.element_ids)),
                created_at,
            )
            for row in rows
        ]
        if not values:
            return
        execute_many(
            connection,
            "INSERT INTO chunks"
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            values,
        )
        self._insert_fts_rows(
            connection, [(row[0], row[1], row[3]) for row in values]
        )
        # The projector's precise ``delete_by_ids`` already dropped the prior
        # rows for these chunks via the chunks cascade; add the new ones in the
        # same transaction so the reverse index never lags its chunk rows.
        self._insert_chunk_element_rows(connection, notebook_id, rows)

    def source_chunks(self, source_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,text FROM chunks WHERE source_id=%s ORDER BY ordinal",
                (source_id,),
            ).fetchall()
        return [{"id": row["id"], "text": row["text"]} for row in rows]

    @staticmethod
    def language_probe_rows(connection, notebook_id: str):
        return connection.execute(
            "(SELECT text FROM chunks WHERE notebook_id=%s ORDER BY ordinal LIMIT 30) "
            "UNION "
            "(SELECT text FROM chunks WHERE notebook_id=%s ORDER BY ordinal DESC LIMIT 30)",
            (notebook_id, notebook_id),
        ).fetchall()

    @staticmethod
    def retrieval_rows(connection, notebook_id: str):
        rows = connection.execute(
            "SELECT c.id,c.source_id,c.text,c.section_path,c.element_ids,"
            "s.title AS source_title FROM chunks c JOIN sources s ON s.id=c.source_id "
            "WHERE c.notebook_id=%s ORDER BY c.ordinal",
            (notebook_id,),
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def count_row(connection, notebook_id: str):
        return connection.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()

    @staticmethod
    def hydrate_rows(connection, chunk_ids: Sequence[str]):
        ids = list(chunk_ids)
        if not ids:
            return []
        rows = connection.execute(
            "SELECT c.id,c.source_id,c.text,c.section_path,c.element_ids,"
            "s.title AS source_title FROM chunks c JOIN sources s ON s.id=c.source_id "
            f"WHERE c.id IN ({placeholders(ids)}) ORDER BY c.ordinal",
            ids,
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def graph_hydrate_rows(connection, chunk_ids: Sequence[str]):
        ids = list(chunk_ids)
        if not ids:
            return []
        rows = connection.execute(
            "SELECT c.id,c.source_id,c.text,c.section_path,c.element_ids,"
            "c.notebook_id AS chunk_notebook_id,s.title AS source_title "
            "FROM chunks c JOIN sources s ON s.id=c.source_id "
            f"WHERE c.id IN ({placeholders(ids)}) ORDER BY c.ordinal",
            ids,
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def retrieval_contribution_rows(
        connection,
        notebook_id: str,
        chunk_ids: Sequence[str],
        *,
        actor_id: str,
        source_mode: str | None,
        source_ids: Sequence[str],
    ):
        ids = list(dict.fromkeys(chunk_ids))
        sources = list(dict.fromkeys(source_ids))
        if not ids or (source_mode == "include" and not sources):
            return []
        source_clause = ""
        params: list[object] = [notebook_id, *ids]
        if source_mode in {"include", "exclude"} and sources:
            source_clause = (
                " AND c.source_id=ANY(%s)"
                if source_mode == "include"
                else " AND c.source_id<>ALL(%s)"
            )
            params.append(sources)
        memory_clause = (
            " AND (s.source_type <> 'memory' OR EXISTS ("
            "SELECT 1 FROM memory_items m "
            "WHERE m.id=s.memory_id AND m.created_by=%s))"
        )
        params.append(actor_id)
        rows = connection.execute(
            "SELECT c.id,c.source_id,c.text,c.section_path,c.element_ids,"
            "c.notebook_id AS chunk_notebook_id,s.title AS source_title "
            "FROM chunks c JOIN sources s "
            "ON s.id=c.source_id AND s.notebook_id=c.notebook_id "
            f"WHERE c.notebook_id=%s AND c.id IN ({placeholders(ids)})"
            f"{source_clause}{memory_clause} ORDER BY c.ordinal",
            params,
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def id_element_rows(connection, notebook_id: str):
        rows = connection.execute(
            "SELECT id,element_ids FROM chunks WHERE notebook_id=%s ORDER BY ordinal",
            (notebook_id,),
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def knowhow_chunk_rows(connection, notebook_id: str):
        rows = connection.execute(
            "SELECT c.id,c.element_ids FROM knowhow_tables kt "
            "JOIN chunks c ON c.source_id=kt.hidden_source_id "
            "WHERE kt.notebook_id=%s AND c.notebook_id=%s ORDER BY c.ordinal",
            (notebook_id, notebook_id),
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def knowhow_bridge_version_row(connection, notebook_id: str):
        row = dict(connection.execute(
            "SELECT COUNT(*) AS c,MAX(ce.created_at) AS ts "
            "FROM knowhow_tables kt "
            "JOIN chunks c ON c.source_id=kt.hidden_source_id "
            "JOIN chunk_embeddings ce ON ce.chunk_id=c.id "
            "WHERE kt.notebook_id=%s AND c.notebook_id=%s AND ce.notebook_id=%s",
            (notebook_id, notebook_id, notebook_id),
        ).fetchone())
        row["ts"] = iso_timestamp(row["ts"])
        return row

    @staticmethod
    def rows_by_ids(connection, chunk_ids: Sequence[str]):
        ids = list(chunk_ids)
        if not ids:
            return []
        rows = connection.execute(
            "SELECT id,source_id,text,section_path,element_ids FROM chunks "
            f"WHERE id IN ({placeholders(ids)}) ORDER BY ordinal",
            ids,
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def id_rows(connection, notebook_id: str):
        return connection.execute(
            "SELECT id FROM chunks WHERE notebook_id=%s ORDER BY ordinal",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def chunks_by_section(
        connection,
        notebook_id: str,
        source_id: str,
        section_path: str,
        limit: int,
    ):
        """One section's chunks (that node plus its descendants), document
        order, hard-bounded — semantically equal to the SQLite adapter's
        `chunks_by_section`. SQL lives in `postgres/search.py` so the LIKE
        predicate stays next to the expression indexes it shares a table with.
        """
        rows = chunk_section_rows(
            connection, notebook_id, source_id, section_path, limit
        )
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def backfill_fts(connection, notebook_id: str) -> int:
        # Search indexes derive directly from chunks; report how many base rows
        # are already covered so the neutral maintenance API remains useful.
        return int(
            connection.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id=%s", (notebook_id,)
            ).fetchone()["c"]
        )
