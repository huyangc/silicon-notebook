from __future__ import annotations

import json
from typing import Sequence

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
from app.repositories.postgres.search import chunk_section_rows


def _compat_element_ids(row: dict) -> dict:
    result = dict(row)
    if "element_ids" in result and not isinstance(result["element_ids"], str):
        result["element_ids"] = json.dumps(result["element_ids"] or [])
    return result


class ChunkStore:
    """PostgreSQL chunk persistence; search indexes derive from base rows."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    @staticmethod
    def ids_for_sources(connection, notebook_id: str, source_ids: Sequence[str]):
        values = list(dict.fromkeys(source_ids))
        if not values:
            return []
        return connection.execute(
            "SELECT id FROM chunks WHERE notebook_id=%s AND source_id=ANY(%s)",
            (notebook_id, values),
        ).fetchall()

    def source_elements_for_chunking(self, source_id: str) -> list[dict]:
        """额外带出 metadata 里的 caption 与 section_path，语义与 SQLite 侧
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
            section_path = str(metadata.get("section_path") or "") if is_dict else ""
            output.append(
                {
                    "id": row["id"],
                    "element_type": row["element_type"],
                    "text": row["text"],
                    "caption": caption,
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
            if mark_chunked_at is not None:
                connection.execute(
                    "UPDATE sources SET chunked_at=%s WHERE id=%s",
                    (normalize_timestamp(mark_chunked_at), source_id),
                )

    def _insert_fts_rows(self, connection, rows: list) -> None:
        # PostgreSQL's GIN/trigram indexes update with chunks themselves.
        del connection, rows

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
