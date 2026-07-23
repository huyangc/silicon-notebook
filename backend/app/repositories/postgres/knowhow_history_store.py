"""PostgreSQL adapter for the backend-neutral Knowhow history contract.

The history/revert algorithm is shared with SQLite.  This module supplies a
small DB-API compatibility boundary (placeholder syntax, timestamp rows, and
the canonical fingerprint query) so the algorithm remains one implementation
instead of being copied into two large, independently drifting files.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator

from app.repositories.postgres.database import PostgresDatabase
from app.repositories.sqlite.knowhow_history_store import (
    KnowhowHistoryStore as SharedKnowhowHistoryStore,
    record_change as shared_record_change,
)


_PG_FINGERPRINT_SQL = """
SELECT
  encode(convert_to(t.title, 'UTF8'), 'hex') AS title,
  encode(convert_to(t.description, 'UTF8'), 'hex') AS description,
  (SELECT string_agg(sig, chr(30) ORDER BY id) FROM (
    SELECT id,
      encode(convert_to(id, 'UTF8'), 'hex') || chr(31) ||
      encode(convert_to(name, 'UTF8'), 'hex') || chr(31) ||
      encode(convert_to(role, 'UTF8'), 'hex') || chr(31) ||
      encode(convert_to(position::text, 'UTF8'), 'hex') AS sig
    FROM knowhow_columns WHERE table_id=t.id
  ) s) AS columns_signal,
  (SELECT string_agg(sig, chr(30) ORDER BY id) FROM (
    SELECT id,
      encode(convert_to(id, 'UTF8'), 'hex') || chr(31) ||
      encode(convert_to(position::text, 'UTF8'), 'hex') AS sig
    FROM knowhow_rows WHERE table_id=t.id
  ) s) AS rows_signal,
  (SELECT string_agg(sig, chr(30) ORDER BY row_id, column_id) FROM (
    SELECT c.row_id, c.column_id,
      encode(convert_to(c.row_id, 'UTF8'), 'hex') || chr(31) ||
      encode(convert_to(c.column_id, 'UTF8'), 'hex') || chr(31) ||
      encode(convert_to(c.content_md, 'UTF8'), 'hex') AS sig
    FROM knowhow_cells c JOIN knowhow_rows r ON r.id=c.row_id
    WHERE r.table_id=t.id
  ) s) AS cells_signal,
  (SELECT string_agg(sig, chr(30) ORDER BY row_id, column_id) FROM (
    SELECT cc.row_id, cc.column_id,
      encode(convert_to(cc.row_id, 'UTF8'), 'hex') || chr(31) ||
      encode(convert_to(cc.column_id, 'UTF8'), 'hex') || chr(31) ||
      encode(convert_to(cc.code_text, 'UTF8'), 'hex') || chr(31) ||
      encode(convert_to(cc.language, 'UTF8'), 'hex') || chr(31) ||
      encode(convert_to(cc.cell_content_hash, 'UTF8'), 'hex') || chr(31) ||
      encode(convert_to(cc.updated_by, 'UTF8'), 'hex') AS sig
    FROM knowhow_cell_code cc JOIN knowhow_rows r ON r.id=cc.row_id
    WHERE r.table_id=t.id
  ) s) AS cell_code_signal
FROM knowhow_tables t WHERE t.id=%s
"""


def _row(row):
    if row is None:
        return None
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in dict(row).items()
    }


class _Cursor:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self):
        return _row(self._cursor.fetchone())

    def fetchall(self):
        return [_row(row) for row in self._cursor.fetchall()]


class CompatConnection:
    def __init__(self, connection) -> None:
        self.connection = connection

    def execute(self, statement: str, params: Any = None):
        normalized = statement.strip().upper()
        if normalized == "BEGIN IMMEDIATE":
            # The surrounding PostgreSQL write transaction is SERIALIZABLE;
            # its conflict detection replaces SQLite's RESERVED lock.
            return _Cursor(self.connection.execute("SELECT 1"))
        if statement.lstrip().startswith("SELECT hex(t.title)"):
            statement = _PG_FINGERPRINT_SQL
        else:
            statement = statement.replace("?", "%s")
        return _Cursor(self.connection.execute(statement, params))


class _CompatDatabase:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    @contextmanager
    def connect(self) -> Iterator[CompatConnection]:
        with self.database.connect() as connection:
            yield CompatConnection(connection)

    @contextmanager
    def write(self) -> Iterator[CompatConnection]:
        with self.database.write(isolation_level="serializable") as connection:
            yield CompatConnection(connection)


def compat_connection(connection) -> CompatConnection:
    return CompatConnection(connection)


def record_change(connection, **kwargs) -> int:
    return shared_record_change(CompatConnection(connection), **kwargs)


class KnowhowHistoryStore(SharedKnowhowHistoryStore):
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], str],
    ) -> None:
        super().__init__(_CompatDatabase(database), new_id=new_id, now=now)
