from __future__ import annotations

import json
from datetime import timedelta
from typing import Callable, Sequence

from psycopg import sql
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.repositories.postgres._store_utils import (
    TimestampInput,
    iso_timestamp,
    normalized_clock,
    normalize_timestamp_row,
    sqlite_compatible_notebook_row,
    sqlite_compatible_row,
    utc_now,
)
from app.repositories.postgres.database import PostgresDatabase


_KNOWHOW_SOURCE_IDS = "SELECT id FROM sources WHERE source_type='knowhow'"
_COPY_SNAPSHOT_QUERIES: tuple[tuple[str, str], ...] = (
    ("notebooks", "SELECT * FROM notebooks WHERE id=%s"),
    ("sources", "SELECT * FROM sources WHERE notebook_id=%s"),
    (
        "source_paper_meta",
        "SELECT m.* FROM source_paper_meta m JOIN sources s ON s.id=m.source_id "
        "WHERE s.notebook_id=%s AND s.source_type<>'knowhow'",
    ),
    (
        "source_authors",
        "SELECT a.* FROM source_authors a JOIN sources s ON s.id=a.source_id "
        "WHERE s.notebook_id=%s AND s.source_type<>'knowhow'",
    ),
    (
        "source_elements",
        "SELECT e.* FROM source_elements e JOIN sources s ON s.id=e.source_id "
        "WHERE s.notebook_id=%s ORDER BY e.ordinal",
    ),
    ("chunks", "SELECT * FROM chunks WHERE notebook_id=%s ORDER BY ordinal"),
    (
        "knowledge_objects",
        "SELECT * FROM knowledge_objects WHERE notebook_id=%s "
        f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS}) ORDER BY ordinal",
    ),
    (
        "knowledge_relations",
        "SELECT * FROM knowledge_relations WHERE notebook_id=%s "
        f"AND (source_id IS NULL OR source_id NOT IN ({_KNOWHOW_SOURCE_IDS}))",
    ),
    ("chunk_embeddings", "SELECT * FROM chunk_embeddings WHERE notebook_id=%s"),
    (
        "element_embeddings",
        "SELECT e.* FROM element_embeddings e JOIN sources s ON s.id=e.source_id "
        "WHERE s.notebook_id=%s AND s.source_type<>'knowhow'",
    ),
    ("knowledge_embeddings", "SELECT * FROM knowledge_embeddings WHERE notebook_id=%s"),
    ("relation_embeddings", "SELECT * FROM relation_embeddings WHERE notebook_id=%s"),
    ("concept_clusters", "SELECT * FROM concept_clusters WHERE notebook_id=%s"),
    ("knowhow_tables", "SELECT * FROM knowhow_tables WHERE notebook_id=%s"),
    (
        "knowhow_columns",
        "SELECT c.* FROM knowhow_columns c JOIN knowhow_tables t ON t.id=c.table_id "
        "WHERE t.notebook_id=%s",
    ),
    (
        "knowhow_rows",
        "SELECT r.* FROM knowhow_rows r JOIN knowhow_tables t ON t.id=r.table_id "
        "WHERE t.notebook_id=%s",
    ),
    (
        "knowhow_cells",
        "SELECT c.* FROM knowhow_cells c JOIN knowhow_rows r ON r.id=c.row_id "
        "JOIN knowhow_tables t ON t.id=r.table_id WHERE t.notebook_id=%s",
    ),
    (
        "knowhow_cell_code",
        "SELECT c.* FROM knowhow_cell_code c JOIN knowhow_rows r ON r.id=c.row_id "
        "JOIN knowhow_tables t ON t.id=r.table_id WHERE t.notebook_id=%s",
    ),
    ("notebook_assets", "SELECT * FROM notebook_assets WHERE notebook_id=%s"),
)
_COPY_VALIDATED_TABLES = (
    ("sources", ""),
    ("source_paper_meta", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("source_authors", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    ("chunks", ""),
    ("knowledge_objects", f"AND source_id NOT IN ({_KNOWHOW_SOURCE_IDS})"),
    (
        "knowledge_relations",
        f"AND (source_id IS NULL OR source_id NOT IN ({_KNOWHOW_SOURCE_IDS}))",
    ),
    ("concept_clusters", ""),
    ("knowhow_tables", ""),
    ("notebook_assets", ""),
)
_COPY_VALIDATED_JOIN_TABLES = (
    (
        "knowhow_columns",
        "SELECT COUNT(*) AS c FROM knowhow_columns x "
        "JOIN knowhow_tables t ON t.id=x.table_id WHERE t.notebook_id=%s",
    ),
    (
        "knowhow_rows",
        "SELECT COUNT(*) AS c FROM knowhow_rows x "
        "JOIN knowhow_tables t ON t.id=x.table_id WHERE t.notebook_id=%s",
    ),
    (
        "knowhow_cells",
        "SELECT COUNT(*) AS c FROM knowhow_cells x JOIN knowhow_rows r ON r.id=x.row_id "
        "JOIN knowhow_tables t ON t.id=r.table_id WHERE t.notebook_id=%s",
    ),
    (
        "knowhow_cell_code",
        "SELECT COUNT(*) AS c FROM knowhow_cell_code x JOIN knowhow_rows r ON r.id=x.row_id "
        "JOIN knowhow_tables t ON t.id=r.table_id WHERE t.notebook_id=%s",
    ),
)
_COPY_TABLES = frozenset(table for table, _query in _COPY_SNAPSHOT_QUERIES)
_JSON_COLUMNS = {
    "source_paper_meta": {"keywords", "raw_json"},
    "source_elements": {"metadata"},
    "chunks": {"element_ids"},
    "knowledge_objects": {"payload", "evidence"},
    "knowledge_relations": {"evidence"},
}


def _snapshot_compat_row(table: str, row: dict) -> dict:
    if table == "notebooks":
        result = sqlite_compatible_notebook_row(row)
    else:
        result = sqlite_compatible_row(row, json_columns=_JSON_COLUMNS.get(table, set()))
    assert result is not None
    # SQLite SELECT * never exposed its implicit rowid. PostgreSQL's explicit
    # compatibility ordinal must likewise stay adapter-private: a deep copy is
    # a new insertion and must allocate a fresh ordinal instead of colliding
    # with the source row's globally unique value.
    result.pop("ordinal", None)
    return result


class SharingStore:
    """PostgreSQL sharing/member rows and backend-neutral deep-copy primitives."""

    def __init__(
        self,
        database: PostgresDatabase,
        settings: Settings,
        *,
        now: Callable[[], TimestampInput],
        insert_row: Callable,
    ) -> None:
        self.database = database
        self.settings = settings
        self.now = normalized_clock(now)
        self.insert_row = insert_row

    def bind_insert_row(self, insert_row: Callable) -> None:
        self.insert_row = insert_row

    def set_share_token(self, notebook_id: str, token: str) -> str:
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT is_shared,share_token FROM notebooks WHERE id=%s FOR UPDATE",
                (notebook_id,),
            ).fetchone()
            chosen = row["share_token"] if row["is_shared"] and row["share_token"] else token
            connection.execute(
                "UPDATE notebooks SET is_shared=1,share_token=%s,updated_at=%s WHERE id=%s",
                (chosen, self.now(), notebook_id),
            )
        return str(chosen)

    def clear_share(self, notebook_id: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "UPDATE notebooks SET is_shared=0,share_token=NULL,updated_at=%s WHERE id=%s",
                (self.now(), notebook_id),
            )
            connection.execute(
                "DELETE FROM notebook_members WHERE notebook_id=%s", (notebook_id,)
            )

    def find_by_token(self, token: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM notebooks WHERE share_token=%s AND is_shared=1", (token,)
            ).fetchone()
        return row["id"] if row else None

    def list_shared_by_owner(self, user_id: str) -> list[dict]:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT id,name,share_token FROM notebooks "
                "WHERE created_by=%s AND is_shared=1 ORDER BY updated_at DESC,id COLLATE \"C\"",
                (user_id,),
            ).fetchall()

    def notebook_row(self, notebook_id: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM notebooks WHERE id=%s", (notebook_id,)
            ).fetchone()
        return sqlite_compatible_notebook_row(row)

    @staticmethod
    def notebook_row_on(connection, notebook_id: str) -> dict | None:
        row = connection.execute(
            "SELECT * FROM notebooks WHERE id=%s", (notebook_id,)
        ).fetchone()
        return sqlite_compatible_notebook_row(row)

    def shared_preview_rows(self, notebook_id: str) -> tuple[str, list[str]]:
        with self.database.connect() as connection:
            owner = connection.execute(
                "SELECT u.username FROM notebooks n LEFT JOIN users u ON u.id=n.created_by "
                "WHERE n.id=%s",
                (notebook_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT title FROM sources WHERE notebook_id=%s "
                "AND source_type NOT IN ('memory','knowhow') "
                "ORDER BY created_at,id COLLATE \"C\" LIMIT 50",
                (notebook_id,),
            ).fetchall()
        return (owner["username"] if owner and owner["username"] else "", [r["title"] for r in rows])

    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT created_by FROM notebooks WHERE id=%s", (notebook_id,)
            ).fetchone()
        return bool(row) and row["created_by"] == user_id

    def is_member(self, notebook_id: str, user_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM notebook_members WHERE notebook_id=%s AND user_id=%s",
                (notebook_id, user_id),
            ).fetchone()
        return row is not None

    def add_member(self, notebook_id: str, user_id: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) "
                "VALUES (%s,%s,'reader',%s) ON CONFLICT(notebook_id,user_id) DO NOTHING",
                (notebook_id, user_id, self.now()),
            )

    def remove_member(self, notebook_id: str, user_id: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM notebook_members WHERE notebook_id=%s AND user_id=%s",
                (notebook_id, user_id),
            )

    def kick_all_members(self, notebook_id: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM notebook_members WHERE notebook_id=%s", (notebook_id,)
            )

    def list_members(self, notebook_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT u.username AS username,m.added_at AS added_at "
                "FROM notebook_members m JOIN users u ON u.id=m.user_id "
                "WHERE m.notebook_id=%s ORDER BY m.added_at,u.username COLLATE \"C\"",
                (notebook_id,),
            ).fetchall()
        return [
            {
                "username": row["username"],
                "added_at": iso_timestamp(row["added_at"]),
            }
            for row in rows
        ]

    def source_owner(self, source_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT n.created_by AS owner FROM sources s "
                "JOIN notebooks n ON n.id=s.notebook_id WHERE s.id=%s",
                (source_id,),
            ).fetchone()
        return row["owner"] if row else None

    def conversation_owner(self, conversation_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT created_by AS owner FROM conversations WHERE id=%s",
                (conversation_id,),
            ).fetchone()
        return row["owner"] if row else None

    def answer_owner(self, answer_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT n.created_by AS owner FROM answers a "
                "JOIN notebooks n ON n.id=a.notebook_id WHERE a.id=%s",
                (answer_id,),
            ).fetchone()
        return row["owner"] if row else None

    def source_notebook_id(self, source_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT notebook_id FROM sources WHERE id=%s", (source_id,)
            ).fetchone()
        return row["notebook_id"] if row else None

    def answer_notebook_id(self, answer_id: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT notebook_id FROM answers WHERE id=%s", (answer_id,)
            ).fetchone()
        return row["notebook_id"] if row else None

    def snapshot_copy_rows(self, notebook_id: str) -> dict[str, list[dict]]:
        snapshot: dict[str, list[dict]] = {}
        with self.database.connect() as connection:
            for table, query in _COPY_SNAPSHOT_QUERIES:
                snapshot[table] = [
                    _snapshot_compat_row(table, row)
                    for row in connection.execute(query, (notebook_id,)).fetchall()
                ]
        return snapshot

    def insert_copy_rows(self, table: str, rows: Sequence[dict], *, chunk_size: int) -> None:
        if table not in _COPY_TABLES:
            raise ValueError("unsupported copy table")
        for index in range(0, len(rows), chunk_size):
            with self.database.write() as connection:
                for data in rows[index : index + chunk_size]:
                    self.insert_row(
                        connection,
                        table,
                        normalize_timestamp_row(table, data),
                    )

    def insert_fts_rows(self, sql_text: str, rows: Sequence[tuple], *, chunk_size: int) -> None:
        # PostgreSQL GIN/trigram indexes are maintained from base rows; there is
        # no copied FTS mirror table. Keep the neutral copy-service hook a no-op.
        del sql_text, rows, chunk_size

    def validate_copy(self, source_notebook_id: str, new_id: str) -> None:
        with self.database.connect() as connection:
            for table, extra in _COPY_VALIDATED_TABLES:
                copied = connection.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=%s {extra}",
                    (new_id,),
                ).fetchone()["c"]
                source = connection.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=%s {extra}",
                    (source_notebook_id,),
                ).fetchone()["c"]
                if copied != source:
                    raise RuntimeError(f"copy_notebook: {table} 行数不一致 {copied}!={source}")
            for table, query in _COPY_VALIDATED_JOIN_TABLES:
                copied = connection.execute(query, (new_id,)).fetchone()["c"]
                source = connection.execute(query, (source_notebook_id,)).fetchone()["c"]
                if copied != source:
                    raise RuntimeError(f"copy_notebook: {table} 行数不一致 {copied}!={source}")
            dangling = connection.execute(
                "SELECT COUNT(*) AS c FROM knowledge_relations r WHERE r.notebook_id=%s AND ("
                "r.source_object_id NOT IN (SELECT id FROM knowledge_objects WHERE notebook_id=%s) OR "
                "r.target_object_id NOT IN (SELECT id FROM knowledge_objects WHERE notebook_id=%s))",
                (new_id, new_id, new_id),
            ).fetchone()["c"]
            if dangling:
                raise RuntimeError("copy_notebook: 关系存在悬空引用")

    def publish_copy(self, notebook_id: str, status: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "UPDATE notebooks SET status=%s,updated_at=%s WHERE id=%s",
                (status, self.now(), notebook_id),
            )

    def compensate_copy(self, notebook_id: str) -> None:
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id=%s", (notebook_id,)
            )
            connection.execute(
                "DELETE FROM notebooks WHERE id=%s AND status='copying'", (notebook_id,)
            )

    def sweep_stale_copies(self, *, created_by: str | None = None) -> int:
        cutoff = utc_now() - timedelta(
            seconds=max(1, self.settings.notebook_copy_stale_seconds)
        )
        with self.database.write() as connection:
            if created_by is None:
                rows = connection.execute(
                    "SELECT id FROM notebooks WHERE status='copying' AND created_at<%s FOR UPDATE",
                    (cutoff,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id FROM notebooks WHERE status='copying' AND created_by=%s "
                    "AND created_at<%s FOR UPDATE",
                    (created_by, cutoff),
                ).fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                return 0
            connection.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id=ANY(%s)", (ids,)
            )
            connection.execute("DELETE FROM notebooks WHERE id=ANY(%s)", (ids,))
        return len(ids)

    @staticmethod
    def insert_row_values(connection, table: str, data: dict) -> None:
        if table not in _COPY_TABLES:
            raise ValueError("unsupported copy table")
        data = normalize_timestamp_row(table, data)
        columns = list(data)
        values = []
        json_columns = _JSON_COLUMNS.get(table, set())
        for column in columns:
            value = data[column]
            if column in json_columns:
                if isinstance(value, str):
                    value = json.loads(value or "null")
                value = Jsonb(value)
            values.append(value)
        statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join(sql.Placeholder() for _ in columns),
        )
        connection.execute(statement, values)
