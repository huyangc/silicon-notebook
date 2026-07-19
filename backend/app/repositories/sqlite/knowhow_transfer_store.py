"""单张 knowhow 表的跨 notebook 传输 SQL：快照 + 单事务插入 + 提交前校验。

镜像 SharingStore 对整本拷贝所做的事，但收窄到一张表 + 它隐藏源的派生产物。
所有 SQL 收在这里（callers_static 约束：原始 SQL 只在 repositories/sqlite 下）。
"""
from __future__ import annotations

import hashlib
import sqlite3

from app.repositories.sqlite.database import SqliteDatabase

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
        it is a ``threading.RLock`` scoped to THIS PROCESS's
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

    def insert_transfer(self, payload: dict, expected_counts: dict) -> None:
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

    #: Shared by ``table_fingerprint`` (own ``connect()``) and
    #: ``delete_table_if_unchanged`` (caller-supplied connection inside its
    #: own ``write()``) so the two never drift apart — see both methods'
    #: docstrings for why they must run the byte-identical query.
    #:
    #: PR review round 5 P1-1 (ROOT FIX — see ``table_fingerprint``'s
    #: docstring for the full history/rationale): rather than a growing list
    #: of individually-enumerated signals (``mutation_seq``, then four
    #: counts, then a cell_code content signal — rounds 2/3), this is now a
    #: structurally-complete projection of every field ``snapshot_table``/
    #: ``insert_transfer`` actually copy as business data: knowhow_tables.
    #: {title,description} (scalar — one row), then one canonically-ordered
    #: ``group_concat`` per child table covering EVERY column that table
    #: reproduces: knowhow_columns.{id,name,role,position}, knowhow_rows.
    #: {id,position}, knowhow_cells.{row_id,column_id,content_md},
    #: knowhow_cell_code.{row_id,column_id,code_text,language,
    #: cell_content_hash}. Deliberately excluded: mutation_seq/created_at/
    #: updated_at/hidden_source_id/created_by on knowhow_tables (rewritten
    #: or dropped by ``_remap`` — never "the copied content", see
    #: transfer.py) and the surrogate ``id`` columns on knowhow_cells/
    #: knowhow_cell_code (their real identity is the (row_id, column_id)
    #: pair the UNIQUE constraint already keys on; the id is regenerated by
    #: ``_remap`` on every copy regardless of content).
    #:
    #: Each per-row tuple is joined with ``char(31)`` (ASCII unit
    #: separator) and rows within a group with ``char(30)`` (ASCII record
    #: separator) — control characters chosen (not e.g. ``:``/``|``, round
    #: 3's choice) specifically because real column names/cell content are
    #: free text that could plausibly contain punctuation but essentially
    #: never contains non-printable separator characters designed for
    #: exactly this purpose. Ordering is by each group's own natural stable
    #: key (``id`` for columns/rows — unique, immutable once assigned; the
    #: UNIQUE(row_id, column_id) pair for cells/cell_code) via a subquery's
    #: ``ORDER BY`` — same idiom round 3 already established (and round 3's
    #: own test_fingerprint_stable_when_nothing_changes already proves it
    #: reproducible across calls) rather than the newer, version-sensitive
    #: ``group_concat(x ORDER BY y)`` syntax. Ordering by ``id`` (a key that
    #: never changes value under any of these edits) rather than a
    #: mutable field like ``position`` is what makes a pure VALUE swap
    #: between two rows (e.g. two columns trading positions) show up as a
    #: hash change: the tuple at a given id's slot in the sequence differs,
    #: even though the multiset of column ids present is unchanged.
    _FINGERPRINT_SQL = (
        "SELECT t.title AS title, t.description AS description, "
        "(SELECT group_concat(sig, char(30)) FROM ("
        "  SELECT id || char(31) || name || char(31) || role || char(31) || position AS sig"
        "  FROM knowhow_columns WHERE table_id = t.id ORDER BY id"
        ")) AS columns_signal, "
        "(SELECT group_concat(sig, char(30)) FROM ("
        "  SELECT id || char(31) || position AS sig"
        "  FROM knowhow_rows WHERE table_id = t.id ORDER BY id"
        ")) AS rows_signal, "
        "(SELECT group_concat(sig, char(30)) FROM ("
        "  SELECT c.row_id || char(31) || c.column_id || char(31) || c.content_md AS sig"
        "  FROM knowhow_cells c JOIN knowhow_rows r ON r.id = c.row_id"
        "  WHERE r.table_id = t.id ORDER BY c.row_id, c.column_id"
        ")) AS cells_signal, "
        "(SELECT group_concat(sig, char(30)) FROM ("
        "  SELECT cc.row_id || char(31) || cc.column_id || char(31) || cc.code_text"
        "    || char(31) || cc.language || char(31) || cc.cell_content_hash AS sig"
        "  FROM knowhow_cell_code cc JOIN knowhow_rows r ON r.id = cc.row_id"
        "  WHERE r.table_id = t.id ORDER BY cc.row_id, cc.column_id"
        ")) AS cell_code_signal "
        "FROM knowhow_tables t WHERE t.id = ?"
    )

    #: ASCII group separator, joining the five top-level signals (table
    #: meta / columns / rows / cells / cell_code) before hashing — one more
    #: level up from the ``char(30)``/``char(31)`` levels inside
    #: ``_FINGERPRINT_SQL`` itself (the standard US < RS < GS ASCII
    #: separator hierarchy, used here exactly as designed).
    _GROUP_SEP = "\x1d"

    @classmethod
    def _fingerprint_on(cls, db: sqlite3.Connection, table_id: str) -> "str | None":
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
        cleanup failure, not silently retry the delete)."""
        with self.database.write() as db:
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
