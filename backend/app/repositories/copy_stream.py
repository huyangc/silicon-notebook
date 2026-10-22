"""The page hand-off of the streaming copy read (``iter_copy_rows``, both backends).

Each table's pages are a generator that owns a live cursor on the store's
connection: a PostgreSQL named (server-side) cursor, or a SQLite cursor. The
generator must be finished -- and closed -- while that connection is still held,
never later by the garbage collector: by then the connection is back in the pool,
and closing a server-side cursor would send ``CLOSE`` inside another borrower's
transaction. ``feed_table`` is the one place that enforces it: the consumer must
read every page inside its call, a consumer that returns early fails loudly, and
the generator is closed on every path before control leaves the store.
"""
from __future__ import annotations

from typing import Iterator

from app.repositories.ports import CopyRowsNotConsumedError, CopyTableConsumer

_END = object()


def feed_table(on_table: CopyTableConsumer, table: str, pages: Iterator[list[dict]]) -> None:
    """Hand ``pages`` to ``on_table`` and close them before returning (module
    docstring). Raises ``CopyRowsNotConsumedError`` when the consumer returned
    with pages left unread."""
    try:
        on_table(table, pages)
        if next(pages, _END) is not _END:
            raise CopyRowsNotConsumedError(
                f"iter_copy_rows: the consumer of {table} returned without reading every page"
            )
    finally:
        close = getattr(pages, "close", None)
        if close is not None:
            close()


def single_page(rows: list[dict]) -> Iterator[list[dict]]:
    """One already-read page (the root row), as a closable page generator."""
    yield rows


def no_pages() -> Iterator[list[dict]]:
    """The page generator of a skipped table."""
    yield from ()
