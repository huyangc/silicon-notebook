"""单张 knowhow 表的跨 notebook 传输 SQL：快照 + 单事务插入 + 提交前校验。

镜像 SharingStore 对整本拷贝所做的事，但收窄到一张表 + 它隐藏源的派生产物。
所有 SQL 收在这里（callers_static 约束：原始 SQL 只在 repositories/sqlite 下）。
"""
from __future__ import annotations

import sqlite3
from typing import Callable

from app.repositories.sqlite import knowhow_fingerprint
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.knowhow_history_store import record_change

# 插入 FK 顺序：表→列/行→资产→格/代码→隐藏源→元素→chunk→向量
_BUSINESS_ORDER = ("columns", "rows", "assets", "cells", "cell_code")
_DERIVED_ORDER = ("elements", "chunks", "chunk_embeddings")


def _insert_rows(db: sqlite3.Connection, table: str, rows: list) -> None:
    for row in rows:
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        db.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )


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
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def snapshot_table(self, table_id: str) -> dict:
        """Read every business/derived row for ``table_id`` as ONE
        internally-consistent point-in-time view.

        PR review round 4 P1 (copy correctness, not only move's): this used
        to run on a plain ``with self.database.connect() as db:`` block.
        ``connect()`` returns the THREAD-LOCAL, REUSED connection (see
        ``SqliteDatabase.connect``'s own docstring — 233+ call sites share
        it — and ``_Conn``'s class docstring, which explicitly warns
        "生产中复用连接实际只读,所有写经 write()") with no explicit
        ``BEGIN``. Under WAL, a bare SELECT with no open transaction
        observes the latest state committed AS OF THE MOMENT THAT STATEMENT
        runs, not a snapshot pinned at the start of the method — a writer
        committing BETWEEN, say, the ``rows`` SELECT and the ``cells``
        SELECT below makes the two see two different committed states (a
        cell ending up referencing a ``row_id`` absent from ``rows``, which
        then hard-crashes ``transfer.py``'s ``_remap`` with a KeyError on
        ``khrow_map[cell["row_id"]]`` — this can happen on a plain
        ``copy_table``, no ``move``/delete involved).

        The obvious-looking fix — wrap the reads in one explicit transaction
        on this SAME connection (``db.execute("BEGIN")`` ... commit at
        with-exit) — is NOT safe here specifically BECAUSE ``connect()`` is
        that shared, thread-local, REUSED connection: nested
        ``with connect() as db:`` blocks from other call sites on this
        thread are a real, already-documented pattern (``_Conn``'s own class
        docstring warns about exactly this), and a manual ``BEGIN`` issued
        while already nested inside some OTHER caller's still-open
        ``with connect()`` block would either raise "cannot start a
        transaction within a transaction" or — worse — silently pin that
        OUTER caller's later reads to this method's snapshot until ITS block
        finally exits (only the OUTERMOST ``with`` commits — ``_Conn.
        __exit__``'s depth counter). That is not a hypothetical risk to
        guard against defensively; it is the exact failure mode ``_Conn``'s
        docstring already exists to warn readers away from.

        ``database.write()`` sidesteps all of that: it always opens a BRAND
        NEW connection (never the shared one, so zero nesting hazard) and
        holds the process-wide ``write_lock`` for its entire duration —
        since EVERY write in this codebase goes through ``write()`` too
        (the same invariant ``insert_transfer``/``delete_table_if_unchanged``
        below already rely on), no concurrent writer can even be mid-commit
        while we hold it. That is strictly stronger than WAL snapshot
        isolation alone would give us (not just "a consistent snapshot", but
        "provably no concurrent write landed at all" for the duration). The
        cost — this method briefly blocks other writers process-wide — is
        the same trade its two siblings in this class already make, and a
        single table's worth of SELECTs (design doc's "百行内" scale
        ceiling) is cheap enough to hold the lock for.

        PR review round 5 P2: ``write_lock`` alone still leaves a gap —
        it is a ``threading.Lock`` scoped to THIS PROCESS's
        ``SqliteDatabase`` instance, so it excludes every OTHER writer in
        this process (every write in this codebase goes through the same
        ``write()``), but it cannot exclude a second OS process (or any
        other runtime) with its own handle on the same SQLite file. Nothing
        above actually stops such a writer from committing between, say,
        the ``rows`` SELECT and the ``cells`` SELECT — the exact torn-read
        shape this method exists to prevent, just from a source outside
        this process's lock instead of a same-process thread.
        ``self.database.write()``'s own connection (see ``_new_connection``
        in ``database.py``) is opened with no ``isolation_level`` override,
        so python's sqlite3 module runs each statement in implicit
        autocommit UNTIL a data-modifying statement opens a transaction —
        SELECTs never trigger that. The explicit ``db.execute("BEGIN")``
        below is what actually closes the cross-process gap: it opens a
        REAL SQLite transaction on this connection before any of the reads
        below run, so every one of them shares ONE snapshot for the
        transaction's whole duration (SQLite's WAL snapshot isolation) no
        matter what any other process commits in the meantime — the same
        guarantee ``write_lock`` gives against same-process writers, now
        also held against everything outside this process. This is safe
        to do here specifically because ``write()`` handed us a BRAND NEW,
        never-shared connection (``_new_connection()`` is called fresh
        inside every ``write()`` call) — it is NOT the thread-local reused
        connection ``connect()`` returns, so none of the nesting hazards
        this docstring's earlier paragraphs document for a manual ``BEGIN``
        on THAT connection apply here: there is no other call site that
        could already have this exact connection open in a ``with`` block,
        because nothing else ever sees this connection object at all. The
        transaction needs no manual ``COMMIT``: ``write()``'s own
        ``with conn:`` (``_Conn.__exit__``, depth-counted, see the class
        docstring above) already commits on clean exit — a plain read-only
        commit is cheap, it just releases the snapshot.
        """
        with self.database.write() as db:
            db.execute("BEGIN")
            table = db.execute(
                "SELECT * FROM knowhow_tables WHERE id = ?", (table_id,)
            ).fetchone()
            if table is None:
                raise KeyError(table_id)
            table = dict(table)

            def rows_for(sql: str, params: tuple) -> list:
                return [dict(r) for r in db.execute(sql, params).fetchall()]

            columns = rows_for(
                "SELECT * FROM knowhow_columns WHERE table_id = ? ORDER BY position, id",
                (table_id,),
            )
            rows = rows_for(
                "SELECT * FROM knowhow_rows WHERE table_id = ? ORDER BY position, id",
                (table_id,),
            )
            cells = rows_for(
                "SELECT c.* FROM knowhow_cells c JOIN knowhow_rows r ON r.id = c.row_id "
                "WHERE r.table_id = ?",
                (table_id,),
            )
            cell_code = rows_for(
                "SELECT cc.* FROM knowhow_cell_code cc JOIN knowhow_rows r ON r.id = cc.row_id "
                "WHERE r.table_id = ?",
                (table_id,),
            )

            hidden = table.get("hidden_source_id")
            elements: list = []
            chunks: list = []
            chunk_embeddings: list = []
            source = None
            if hidden:
                source_row = db.execute(
                    "SELECT * FROM sources WHERE id = ?", (hidden,)
                ).fetchone()
                source = dict(source_row) if source_row is not None else None
                if source is not None:
                    elements = rows_for(
                        "SELECT * FROM source_elements WHERE source_id = ?", (hidden,)
                    )
                    chunks = rows_for(
                        "SELECT * FROM chunks WHERE source_id = ?", (hidden,)
                    )
                    chunk_embeddings = rows_for(
                        "SELECT ce.* FROM chunk_embeddings ce "
                        "JOIN chunks c ON c.id = ce.chunk_id WHERE c.source_id = ?",
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
        """``new_id``/``now`` (knowhow 表版本管理 Task 13, spec §7.3): when
        BOTH are supplied, records ONE ``table_create`` genesis flow entry
        for the freshly-inserted target table as the LAST step of this same
        write transaction — ``record_change`` itself requires this (its
        fingerprint must reflect the state AFTER the change, and here that
        means after every business/derived row has landed AND the count
        check below has actually passed; a failed check raises and rolls
        back the whole transaction, so a rejected transfer records nothing).
        ``actor``/``note`` are the caller's (``copy_table``/``move_table``
        via ``transfer.py``) resolved values — this store has no ``seams``
        of its own (see ``RepositoryRuntime.__init__``'s own comment on
        ``knowhow_transfer_store``), so id/clock generation is always
        supplied at call time, never read from ``self``.

        Both default to ``None`` so existing plain calls (this store's own
        ``test_insert_transfer_rejects_count_mismatch``, which intentionally
        never reaches the success path) keep working unchanged — recording
        is simply skipped when either is omitted, not a silent partial
        write: with no ``new_id``, ``record_change`` could not even mint the
        change row's own id.

        History itself does NOT travel with the transfer (spec §7.3 — the
        source table's own ``knowhow_changes``/``knowhow_milestones`` are
        deliberately absent from ``_BUSINESS_ORDER``/``_DERIVED_ORDER``
        above); this is the target's OWN, brand-new first flow entry, not a
        copy of anything the source ever recorded."""
        table = payload["table"]
        new_table_id = table["id"]
        with self.database.write() as db:
            db.execute("BEGIN IMMEDIATE")
            _insert_rows(db, "knowhow_tables", [table])
            for key in _BUSINESS_ORDER:
                _insert_rows(db, _TABLE_NAMES[key], payload.get(key) or [])
            if payload.get("source"):
                _insert_rows(db, "sources", [payload["source"]])
            for key in _DERIVED_ORDER:
                _insert_rows(db, _TABLE_NAMES[key], payload.get(key) or [])

            # chunks_fts 必须在这里显式补，且必须在同一个事务里。
            #
            # chunks_fts 是「无触发器、手工维护」的 FTS5 虚表（migrations.py
            # 只给 memory_items_fts 建了触发器）——正常投影路径靠
            # ChunkStore.insert_rows 末尾那行显式写入（chunk_store.py:139）。
            # 而拷贝出来的 chunk 走不到那行：copy_table 之后调度的重投影，对
            # 每个副本 chunk 都满足 `old_specs == new_specs` → _write_chunks
            # 直接 `continue`（projection.py），insert_rows/delete_by_ids 这两
            # 个仅有的写 FTS 路径一个都不会被调用。所以不在这里补，副本就只剩
            # 向量召回、词法检索永久搜不到，而且没有任何自愈路径（向量那边有
            # self-heal probe，FTS 没有）。
            #
            # 语句与列序照抄 ChunkStore._insert_fts_rows（chunk_store.py:75-78,
            # 由 insert_rows:139 调用），不另发明 SQL；同事务保证「FTS 写失败
            # 连 chunk 行一起回滚」，与 replace_source_chunks 的既有不变量一致。
            # notebook_sharing.py:487-491 对整本拷贝做的是同一件事，原因相同。
            chunk_rows = payload.get("chunks") or []
            if chunk_rows:
                db.executemany(
                    "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                    [
                        (c["id"], c["notebook_id"], c.get("text") or "")
                        for c in chunk_rows
                    ],
                )

            # 提交前校验：落库计数须等于源表快照计数（不一致 → 抛错 → 回滚，不留半份副本）
            def count(sql: str) -> int:
                return int(db.execute(sql, (new_table_id,)).fetchone()[0])

            checks = {
                "columns": count("SELECT COUNT(*) FROM knowhow_columns WHERE table_id=?"),
                "rows": count("SELECT COUNT(*) FROM knowhow_rows WHERE table_id=?"),
                "cells": count(
                    "SELECT COUNT(*) FROM knowhow_cells c JOIN knowhow_rows r "
                    "ON r.id=c.row_id WHERE r.table_id=?"
                ),
                "cell_code": count(
                    "SELECT COUNT(*) FROM knowhow_cell_code cc JOIN knowhow_rows r "
                    "ON r.id=cc.row_id WHERE r.table_id=?"
                ),
            }
            expected = {k: int(expected_counts.get(k, 0)) for k in checks}
            if checks != expected:
                raise RuntimeError(f"knowhow transfer 校验失败：{checks} != {expected}")

            # 版本管理创世流水（spec §7.3）：必须是本事务的最后一步——见本方法
            # 签名处的文档字符串。cells/cell_code 按 row_id 分组重建成
            # table_create 的标准 payload 形状（"table"/"columns"/"rows"，
            # 每行的 "cells"/"code" 内嵌）——与 create_knowhow_table 自己产出的
            # 创世流水同一形状，只是这次 rows 非空（这张表落库时已经带着全部
            # 初始内容，不像普通建表要等后续逐行 add_knowhow_row）。
            if new_id is not None and now is not None:
                cells_by_row: "dict[str, dict[str, str]]" = {}
                for cell in payload.get("cells") or []:
                    cells_by_row.setdefault(cell["row_id"], {})[cell["column_id"]] = (
                        cell["content_md"]
                    )
                code_by_row: "dict[str, list[dict]]" = {}
                for code in payload.get("cell_code") or []:
                    code_by_row.setdefault(code["row_id"], []).append({
                        "column_id": code["column_id"],
                        "code_text": code["code_text"],
                        "language": code.get("language", ""),
                        "cell_content_hash": code["cell_content_hash"],
                        "updated_by": code.get("updated_by", ""),
                    })
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
                        "rows": [
                            {
                                "row_id": row["id"], "position": row["position"],
                                "created_at": row["created_at"],
                                "cells": cells_by_row.get(row["id"], {}),
                                "code": code_by_row.get(row["id"], []),
                            }
                            for row in payload.get("rows") or []
                        ],
                    },
                    actor=actor, origin="user", note=note,
                )

    #: 搬到 knowhow_fingerprint 后的同对象别名——既有引用（含测试）不受影响。
    #: 改 SQL 请去 knowhow_fingerprint.py，那里写明了它现在有两个身份。
    _FINGERPRINT_SQL = knowhow_fingerprint.FINGERPRINT_SQL
    _GROUP_SEP = knowhow_fingerprint.GROUP_SEP

    @classmethod
    def _fingerprint_on(cls, db: sqlite3.Connection, table_id: str) -> "str | None":
        return knowhow_fingerprint.fingerprint_on(db, table_id)

    def table_fingerprint(self, table_id: str) -> "str | None":
        """Cheap source-version probe for ``table_id``: a SHA-256 hash over
        every field the copy path actually reproduces, computed in ONE
        top-level SELECT (SQLite gives a single statement's scalar
        subqueries a consistent snapshot as of when it starts executing, so
        this can't observe a torn read the way separate queries could —
        same guarantee this method has always relied on, see
        ``_FINGERPRINT_SQL``'s own comment for exactly what it hashes and
        why). Returns ``None`` if the table no longer exists.

        Used by ``move_table``'s snapshot-vs-delete concurrent-edit guard:
        ``copy_table`` snapshots the source, and the source is deleted
        afterward — if ANYTHING the copy reproduces changes in between, the
        target holds a stale copy and the newly-edited source is destroyed
        forever unless this probe catches it. That requirement —
        "detect any change to anything the copy reproduces" — is the whole
        method's job description, and three review rounds in a row each
        found one more field it didn't actually cover:

        - Round 2 P1-2: a bare ``mutation_seq`` compare missed added/
          deleted rows and columns entirely — per ``KnowhowStore``'s own
          docstring, structural edits (add/delete row, add/delete column)
          deliberately do NOT bump it, only ``update_knowhow_cell``/
          ``update_knowhow_cells`` do. Fixed by adding four live counts
          (columns/rows/cells/cell_code).
        - Round 3 P1-3: the four counts still missed an in-place CODE edit
          — ``upsert_knowhow_cell_code`` is an ``INSERT ... ON
          CONFLICT(row_id, column_id) DO UPDATE``, so editing an
          already-attached cell's code keeps the same row/column pair and
          doesn't move any count (and, like every structural-vs-content
          distinction in this table, doesn't bump ``mutation_seq`` either).
          Fixed by adding a ``cell_code_signal`` content hash.
        - Round 5 P1-1 (this fix): the counts + content signal STILL missed
          a third class of edit — the table's own title/description and
          per-column name/role/position. ``role`` alone encodes BOTH the
          anchor designation (which column is the row-title / KG subject)
          AND the content kind (which columns participate in KG
          extraction) — a metadata-only edit here changes KG projection
          semantics with zero effect on any prior signal, so a concurrent
          edit could ride along silently deleted while the target keeps a
          stale label forever (previously written off in this docstring as
          an "accepted scope boundary" — round 5 disagrees: this is not
          cosmetic, it changes what the copy MEANS).

        Rather than enumerate a fourth signal (and leave a structurally
        identical opening for a fifth review round to find), this
        fingerprint is now built the other way around: it is a hash of
        EVERY field ``snapshot_table``/``insert_transfer`` reproduce as
        business data, full stop — see ``_FINGERPRINT_SQL``'s own comment
        for the exact field list. The invariant this buys: the fingerprint
        changes if and only if some field the copy reproduces changed.
        Extending what the copy reproduces (a new copied column on one of
        these tables) still requires updating ``_FINGERPRINT_SQL`` too —
        that coupling cannot be automated away — but it is now a single,
        obvious, mechanical edit to one SQL string in one place, not a
        fresh signal someone has to think up from scratch.
        """
        with self.database.connect() as db:
            return self._fingerprint_on(db, table_id)

    def delete_table_if_unchanged(
        self, table_id: str, expected_fingerprint: "str | None"
    ) -> bool:
        """Atomic conditional delete for ``move_table``'s cleanup step (PR
        review round 3 P1-1): recomputes ``table_fingerprint`` and deletes
        the ``knowhow_tables`` row (cascade — see migration 16/17's
        ``ON DELETE CASCADE`` FKs from columns/rows onto the table, and from
        cells/cell_code onto rows) in ONE ``database.write()``. The whole
        method body runs under ``database.write_lock`` continuously (see
        ``SqliteDatabase.write``'s docstring: writers are process-wide
        serialized), so no concurrent writer's edit can land between "the
        recheck says unchanged" and "the row is gone" the way it could
        across two separate calls (``table_fingerprint()`` then a separate
        unconditional delete) — that gap is exactly PR review round 2's
        P1-2 fix left open: it re-checked the fingerprint, but the check and
        the eventual delete were still two independent statements with an
        arbitrary amount of other work (projection teardown) running in
        between, so an edit landing in THAT window still sailed past the
        check and got deleted along with the row. Returns whether the row
        was actually deleted (``False`` means a concurrent edit changed the
        fingerprint since ``expected_fingerprint`` was captured — the table
        is left fully intact, caller should surface this as a recoverable
        cleanup failure, not silently retry the delete).

        PR review round 7 P1-B (cross-process hardening): the fingerprint
        SELECT below used to run before any DML on this connection —
        ``write()`` hands back a brand-new connection with no
        ``isolation_level`` override, so python's sqlite3 module runs a bare
        SELECT in implicit autocommit, opening no real SQLite transaction at
        all. ``database.write_lock`` (a ``threading.Lock``; nested ``write()``
        calls on one thread are handled by ``write_depth``, not by lock
        reentrancy) excludes every
        OTHER writer IN THIS PROCESS for the method's whole duration, but it
        is a process-local primitive — it cannot exclude a second OS process
        with its own handle on the same SQLite file (this codebase's offline
        CLI and the backend server share one product database in real
        deployments; see ``snapshot_table``'s own docstring, round 5 P2, for
        the identical gap and the identical reasoning). Without a real
        transaction spanning both statements, an external writer could still
        commit an edit between the fingerprint SELECT and the DELETE below,
        even though every same-process caller is correctly locked out.
        ``BEGIN IMMEDIATE`` (rather than the plain ``BEGIN`` ``snapshot_
        table`` uses — that method is read-only and never escalates to a
        write, so a deferred transaction is enough; this method also
        deletes) acquires SQLite's RESERVED lock as the very first statement
        on this connection, mirroring ``insert_transfer``'s own established
        convention for a method that reads then writes in one transaction.
        Safe to issue here for the same reason ``snapshot_table`` gives:
        ``write()`` always hands back a BRAND NEW, never-before-used
        connection (see ``_new_connection`` in ``database.py``), so there is
        no possibility of "cannot start a transaction within a transaction"
        — nothing else has ever touched this exact connection object."""
        with self.database.write() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._fingerprint_on(db, table_id) != expected_fingerprint:
                return False
            cursor = db.execute("DELETE FROM knowhow_tables WHERE id = ?", (table_id,))
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
                "SELECT id FROM sources WHERE notebook_id = ? AND source_type = 'knowhow' "
                "AND id NOT IN ("
                "  SELECT hidden_source_id FROM knowhow_tables "
                "  WHERE hidden_source_id IS NOT NULL"
                ")",
                (notebook_id,),
            ).fetchall()
        return [str(row["id"]) for row in rows]
