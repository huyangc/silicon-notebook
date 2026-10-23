"""Full characterization snapshots with fewer PostgreSQL round trips."""
from __future__ import annotations

from contextlib import ExitStack
from typing import Any, Generator

from psycopg import sql

from app.repositories.read_budget import current_read_budget


def read_table_rows(
    db: Any, tables: list[str],
) -> Generator[tuple[str, list[Any]], None, None]:
    """Read every table on the caller's connection, without changing its transaction.

    Bounded reads keep their per-statement deadline checks and execute proxies.
    Ordinary snapshots queue all reads before waiting, then release each cursor's
    libpq result as soon as its rows are materialised. The caller consumes one
    table at a time into its row Counter and closes this generator on failure.
    """
    if current_read_budget() is not None:
        for table in tables:
            with db.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))) as cursor:
                yield table, cursor.fetchall()
        return

    with ExitStack() as stack:
        cursors = []
        with db.pipeline():
            for table in tables:
                cursor = stack.enter_context(db.execute(
                    sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
                ))
                cursors.append((table, cursor))
        for table, cursor in cursors:
            rows = cursor.fetchall()
            cursor.close()
            yield table, rows
