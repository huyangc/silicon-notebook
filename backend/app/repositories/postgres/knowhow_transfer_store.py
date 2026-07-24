"""单张 knowhow 表的 PostgreSQL 快照、传输与条件删除。"""
from __future__ import annotations

import hashlib
from typing import Callable

from psycopg import sql

from app.repositories.postgres._store_utils import (
    json_value,
    jsonb,
    normalize_timestamp_row,
    sqlite_compatible_row,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.knowhow_history_store import record_change

# 插入 FK 顺序：表→列/行→资产→格/代码→隐藏源→元素→chunk→向量
_BUSINESS_ORDER = ("columns", "rows", "assets", "cells", "cell_code")
_DERIVED_ORDER = ("elements", "chunks", "chunk_embeddings")

_TRANSFER_TABLES = frozenset({
    "knowhow_tables",
    "knowhow_columns",
    "knowhow_rows",
    "notebook_assets",
    "knowhow_cells",
    "knowhow_cell_code",
    "sources",
    "source_elements",
    "chunks",
    "chunk_embeddings",
})
_JSON_COLUMNS = {
    "source_elements": {"metadata": {}},
    "chunks": {"element_ids": []},
}


def _insert_rows(db: object, table: str, rows: list) -> None:
    if table not in _TRANSFER_TABLES:
        raise ValueError("unsupported knowhow transfer table")
    for row in rows:
        prepared = normalize_timestamp_row(table, row)
        prepared.pop("ordinal", None)
        for column, default in _JSON_COLUMNS.get(table, {}).items():
            if column in prepared:
                prepared[column] = jsonb(json_value(prepared[column], default))
        cols = list(prepared.keys())
        db.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table),
                sql.SQL(",").join(map(sql.Identifier, cols)),
                sql.SQL(",").join(sql.Placeholder() for _ in cols),
            ),
            [prepared[c] for c in cols],
        )


def _snapshot_row(table: str, row: dict) -> dict:
    result = sqlite_compatible_row(
        row,
        json_columns=tuple(_JSON_COLUMNS.get(table, {})),
        timestamp_columns=tuple(
            column
            for column in row
            if column.endswith("_at")
        ),
    )
    assert result is not None
    result.pop("ordinal", None)
    if "vector" in result:
        result["vector"] = bytes(result["vector"])
    return result


# 逻辑表名 → 真实表名（键在 payload/校验里用逻辑名）
_TABLE_NAMES = {
    "columns": "knowhow_columns",
    "rows": "knowhow_rows",
    "assets": "notebook_assets",
    "cells": "knowhow_cells",
    "cell_code": "knowhow_cell_code",
    "elements": "source_elements",
    "chunks": "chunks",
    "chunk_embeddings": "chunk_embeddings",
}


class KnowhowTransferStore:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def snapshot_table(self, table_id: str) -> dict:
        """Return one stable, deterministic table snapshot.

        REPEATABLE READ prevents a transfer from mixing rows from different
        committed versions while another process edits the source table.
        """
        with self.database.write(isolation_level="repeatable read") as db:
            table = db.execute(
                "SELECT * FROM knowhow_tables WHERE id = %s FOR SHARE", (table_id,)
            ).fetchone()
            if table is None:
                raise KeyError(table_id)
            table = _snapshot_row("knowhow_tables", table)

            def rows_for(table_name: str, statement: str, params: tuple) -> list:
                return [
                    _snapshot_row(table_name, dict(r))
                    for r in db.execute(statement, params).fetchall()
                ]

            columns = rows_for(
                "knowhow_columns",
                "SELECT * FROM knowhow_columns WHERE table_id = %s "
                "ORDER BY position, id COLLATE \"C\"",
                (table_id,),
            )
            rows = rows_for(
                "knowhow_rows",
                "SELECT * FROM knowhow_rows WHERE table_id = %s "
                "ORDER BY position, id COLLATE \"C\"",
                (table_id,),
            )
            cells = rows_for(
                "knowhow_cells",
                "SELECT c.* FROM knowhow_cells c JOIN knowhow_rows r ON r.id = c.row_id "
                "WHERE r.table_id = %s ORDER BY c.row_id COLLATE \"C\", c.column_id COLLATE \"C\"",
                (table_id,),
            )
            cell_code = rows_for(
                "knowhow_cell_code",
                "SELECT cc.* FROM knowhow_cell_code cc JOIN knowhow_rows r ON r.id = cc.row_id "
                "WHERE r.table_id = %s ORDER BY cc.row_id COLLATE \"C\", cc.column_id COLLATE \"C\"",
                (table_id,),
            )

            hidden = table.get("hidden_source_id")
            elements: list = []
            chunks: list = []
            chunk_embeddings: list = []
            source = None
            if hidden:
                source_row = db.execute(
                    "SELECT * FROM sources WHERE id = %s", (hidden,)
                ).fetchone()
                source = (
                    _snapshot_row("sources", dict(source_row))
                    if source_row is not None
                    else None
                )
                if source is not None:
                    elements = rows_for(
                        "source_elements",
                        "SELECT * FROM source_elements WHERE source_id = %s ORDER BY ordinal, id COLLATE \"C\"",
                        (hidden,),
                    )
                    chunks = rows_for(
                        "chunks",
                        "SELECT * FROM chunks WHERE source_id = %s ORDER BY ordinal, id COLLATE \"C\"",
                        (hidden,),
                    )
                    chunk_embeddings = rows_for(
                        "chunk_embeddings",
                        "SELECT ce.* FROM chunk_embeddings ce "
                        "JOIN chunks c ON c.id = ce.chunk_id WHERE c.source_id = %s "
                        "ORDER BY ce.chunk_id COLLATE \"C\"",
                        (hidden,),
                    )
        return {
            "table": table,
            "columns": columns,
            "rows": rows,
            "cells": cells,
            "cell_code": cell_code,
            "source": source,
            "elements": elements,
            "chunks": chunks,
            "chunk_embeddings": chunk_embeddings,
        }

    def insert_transfer(
        self,
        payload: dict,
        expected_counts: dict,
        *,
        new_id: "Callable[[str], str] | None" = None,
        now: "Callable[[], str] | None" = None,
        actor: str = "",
        note: str = "",
    ) -> None:
        """PostgreSQL mirror of the SQLite ``insert_transfer`` (knowhow 表版本
        管理 Task 13, spec §7.3): when BOTH ``new_id`` and ``now`` are supplied,
        records ONE ``table_create`` genesis flow entry for the freshly-inserted
        target table as the LAST step of this same write transaction —
        ``record_change`` requires this ordering (its fingerprint must reflect
        the state AFTER every business/derived row has landed AND the count
        check below has passed; a failed check raises and rolls the whole
        transaction back, so a rejected transfer records nothing).

        ``actor``/``note`` are the caller's (``copy_table``/``move_table``)
        resolved values — this store carries no seams of its own, so id/clock
        generation is always supplied at call time, never read from ``self``.
        Both ``new_id``/``now`` default to ``None`` so plain calls that never
        reach the success path keep working unchanged — recording is simply
        skipped when either is omitted (with no ``new_id``, ``record_change``
        could not even mint the change row's own id). History itself does NOT
        travel with the transfer (spec §7.3); this is the target's OWN,
        brand-new first flow entry."""
        table = payload["table"]
        new_table_id = table["id"]
        with self.database.write() as db:
            _insert_rows(db, "knowhow_tables", [table])
            for key in _BUSINESS_ORDER:
                _insert_rows(db, _TABLE_NAMES[key], payload.get(key) or [])
            if payload.get("source"):
                _insert_rows(db, "sources", [payload["source"]])
            for key in _DERIVED_ORDER:
                _insert_rows(db, _TABLE_NAMES[key], payload.get(key) or [])

            # PostgreSQL lexical indexes derive directly from ``chunks.text``;
            # no side-table maintenance is required during transfer.

            # 提交前校验：落库计数须等于源表快照计数（不一致 → 抛错 → 回滚，不留半份副本）
            def count(sql: str) -> int:
                return int(db.execute(sql, (new_table_id,)).fetchone()["count"])

            checks = {
                "columns": count("SELECT COUNT(*) FROM knowhow_columns WHERE table_id=%s"),
                "rows": count("SELECT COUNT(*) FROM knowhow_rows WHERE table_id=%s"),
                "cells": count(
                    "SELECT COUNT(*) FROM knowhow_cells c JOIN knowhow_rows r "
                    "ON r.id=c.row_id WHERE r.table_id=%s"
                ),
                "cell_code": count(
                    "SELECT COUNT(*) FROM knowhow_cell_code cc JOIN knowhow_rows r "
                    "ON r.id=cc.row_id WHERE r.table_id=%s"
                ),
            }
            expected = {k: int(expected_counts.get(k, 0)) for k in checks}
            if checks != expected:
                raise RuntimeError(f"knowhow transfer 校验失败：{checks} != {expected}")

            # 版本管理创世流水（spec §7.3）：本事务的最后一步。payload 形状与
            # ``create_knowhow_table``/SQLite ``insert_transfer`` 产出的创世流水
            # 完全一致（"table"/"columns"/"rows"，``rows`` 恒为 ``[]``）；
            # ``origin='import'`` 同 ``copy_table``——复制/移动语义由 ``note``
            # 如实区分。``record_change`` 自行把 ``db`` 包成兼容连接（见
            # postgres/knowhow_history_store.py），与本 adapter 其它写路径一致。
            if new_id is not None and now is not None:
                record_change(
                    db, new_id=new_id, now=now, table_id=new_table_id,
                    kind="table_create",
                    payload={
                        "table": {
                            "title": table["title"], "description": table["description"],
                        },
                        "columns": [
                            {
                                "id": column["id"], "name": column["name"],
                                "role": column["role"], "position": column["position"],
                            }
                            for column in payload.get("columns") or []
                        ],
                        "rows": [],
                    },
                    actor=actor, origin="import", note=note,
                )

    # Shared by the version probe and conditional delete. The canonical
    # string covers every business field copied by the transfer service and
    # orders each child collection by immutable identifiers.
    _FINGERPRINT_SQL = (
        "SELECT t.title AS title, t.description AS description, "
        "(SELECT string_agg(sig, chr(30) ORDER BY id COLLATE \"C\") FROM ("
        "  SELECT id, id || chr(31) || name || chr(31) || role || chr(31) || position::text AS sig"
        "  FROM knowhow_columns WHERE table_id = t.id ORDER BY id COLLATE \"C\""
        ") fingerprint_columns) AS columns_signal, "
        "(SELECT string_agg(sig, chr(30) ORDER BY id COLLATE \"C\") FROM ("
        "  SELECT id, id || chr(31) || position::text AS sig"
        "  FROM knowhow_rows WHERE table_id = t.id ORDER BY id COLLATE \"C\""
        ") fingerprint_rows) AS rows_signal, "
        "(SELECT string_agg(sig, chr(30) ORDER BY row_id COLLATE \"C\", column_id COLLATE \"C\") FROM ("
        "  SELECT c.row_id, c.column_id, c.row_id || chr(31) || c.column_id || chr(31) || c.content_md AS sig"
        "  FROM knowhow_cells c JOIN knowhow_rows r ON r.id = c.row_id"
        "  WHERE r.table_id = t.id ORDER BY c.row_id COLLATE \"C\", c.column_id COLLATE \"C\""
        ") fingerprint_cells) AS cells_signal, "
        "(SELECT string_agg(sig, chr(30) ORDER BY row_id COLLATE \"C\", column_id COLLATE \"C\") FROM ("
        "  SELECT cc.row_id, cc.column_id, cc.row_id || chr(31) || cc.column_id || chr(31) || cc.code_text"
        "    || chr(31) || cc.language || chr(31) || cc.cell_content_hash"
        "    || chr(31) || cc.updated_by AS sig"
        "  FROM knowhow_cell_code cc JOIN knowhow_rows r ON r.id = cc.row_id"
        "  WHERE r.table_id = t.id ORDER BY cc.row_id COLLATE \"C\", cc.column_id COLLATE \"C\""
        ") fingerprint_code) AS cell_code_signal "
        "FROM knowhow_tables t WHERE t.id = %s"
    )

    #: ASCII group separator, joining the five top-level signals (table
    #: meta / columns / rows / cells / cell_code) before hashing — one more
    #: level up from the ``char(30)``/``char(31)`` levels inside
    #: ``_FINGERPRINT_SQL`` itself (the standard US < RS < GS ASCII
    #: separator hierarchy, used here exactly as designed).
    _GROUP_SEP = "\x1d"

    @classmethod
    def _fingerprint_on(cls, db: object, table_id: str) -> "str | None":
        row = db.execute(cls._FINGERPRINT_SQL, (table_id,)).fetchone()
        if row is None:
            return None
        parts = [
            row["title"],
            row["description"],
            row["columns_signal"] or "",
            row["rows_signal"] or "",
            row["cells_signal"] or "",
            row["cell_code_signal"] or "",
        ]
        canonical = cls._GROUP_SEP.join(parts)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def table_fingerprint(self, table_id: str) -> "str | None":
        """Hash all business fields reproduced by a table transfer."""
        with self.database.connect() as db:
            return self._fingerprint_on(db, table_id)

    def delete_table_if_unchanged(
        self, table_id: str, expected_fingerprint: "str | None"
    ) -> bool:
        """Delete only if the locked table still matches its source snapshot."""
        with self.database.write() as db:
            locked = db.execute(
                "SELECT id FROM knowhow_tables WHERE id=%s FOR UPDATE", (table_id,)
            ).fetchone()
            if locked is None:
                return False
            if self._fingerprint_on(db, table_id) != expected_fingerprint:
                return False
            cursor = db.execute("DELETE FROM knowhow_tables WHERE id = %s", (table_id,))
            return cursor.rowcount == 1

    def orphaned_knowhow_source_ids(self, notebook_id: str) -> list[str]:
        """PR review round 6 P1-B: every ``source_type='knowhow'`` row in
        ``notebook_id`` that no ``knowhow_tables.hidden_source_id`` currently
        references.

        These can only exist via one narrow, real window in ``move_table``'s
        own cleanup: ``delete_table_projection(hidden)`` deletes the SOURCE
        row but deliberately never touches the TABLE row's
        ``hidden_source_id`` column (only ``set_knowhow_hidden_source``
        writes it) — so between that call returning and the atomic
        ``delete_table_if_unchanged`` a moment later, the table row still
        exists with ``hidden_source_id`` pointing at the now-gone source. A
        concurrent ``KnowhowProjector.ensure_hidden_source`` for that exact
        table (a stale-debounce reprojection racing the move) reads that
        stale id, fails to resolve it, and mints a REPLACEMENT — updating the
        table row's ``hidden_source_id`` to the new id. ``table_fingerprint``
        never sees this (it is deliberately structural-business-data-only —
        ``hidden_source_id`` is derived, not business content, see that
        method's own docstring), so the atomic delete still succeeds
        normally a moment later: the table row (carrying the replacement id)
        is gone, and the replacement source is now referenced by NOTHING —
        a permanent orphan, still searchable, sitting in the source
        notebook.

        Every OTHER ``knowhow`` source is always either live (a table's
        current ``hidden_source_id``) or already torn down in the very same
        ``database.write()`` call that deletes its owning table row
        (ordinary ``delete_table_projection`` + ``delete_knowhow_table``
        pair, or this store's own ``delete_table_if_unchanged``) — nothing
        else in this codebase ever leaves one dangling, so this is a cheap,
        safe, idempotent sweep to run after every successful move, not a
        general-purpose repair scan. Read-only; the caller tears each
        returned id down via the existing ``KnowhowProjector.
        delete_table_projection`` (idempotent no-op if it somehow raced
        itself gone again by the time the sweep gets to it)."""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT id FROM sources WHERE notebook_id = %s AND source_type = 'knowhow' "
                "AND id NOT IN ("
                "  SELECT hidden_source_id FROM knowhow_tables "
                "  WHERE hidden_source_id IS NOT NULL"
                ")",
                (notebook_id,),
            ).fetchall()
        return [str(row["id"]) for row in rows]
