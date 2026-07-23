from __future__ import annotations

import json
from typing import Sequence

from app.repositories.ports import ChunkWrite
from app.repositories.postgres._store_utils import (
    TimestampInput,
    execute_many,
    json_value,
    jsonb,
    normalize_timestamp,
    placeholders,
)
from app.repositories.postgres.database import PostgresDatabase


def _compat_element_ids(row: dict) -> dict:
    result = dict(row)
    if "element_ids" in result and not isinstance(result["element_ids"], str):
        result["element_ids"] = json.dumps(result["element_ids"] or [])
    return result


class ChunkStore:
    """PostgreSQL chunk persistence; search indexes derive from base rows."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def source_elements_for_chunking(self, source_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,element_type,text,metadata FROM source_elements "
                "WHERE source_id=%s ORDER BY id COLLATE \"C\"",
                (source_id,),
            ).fetchall()
        output = []
        for row in rows:
            metadata = json_value(row["metadata"], {})
            caption = str(metadata.get("caption") or "") if isinstance(metadata, dict) else ""
            output.append(
                {
                    "id": row["id"],
                    "element_type": row["element_type"],
                    "text": row["text"],
                    "caption": caption,
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
            "SELECT id,element_ids FROM chunks WHERE notebook_id=%s AND source_id IN "
            "(SELECT id FROM sources WHERE notebook_id=%s AND source_type='knowhow') "
            "ORDER BY ordinal",
            (notebook_id, notebook_id),
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

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
    def backfill_fts(connection, notebook_id: str) -> int:
        # Search indexes derive directly from chunks; report how many base rows
        # are already covered so the neutral maintenance API remains useful.
        return int(
            connection.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id=%s", (notebook_id,)
            ).fetchone()["c"]
        )
