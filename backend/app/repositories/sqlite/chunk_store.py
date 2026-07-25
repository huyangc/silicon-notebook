from __future__ import annotations

import json
import sqlite3
from typing import Sequence

from app.repositories.ports import ChunkWrite
from app.repositories.sqlite.database import SqliteDatabase


class ChunkStore:
    """SQLite chunks/chunks_fts row persistence for the chunk-native retrieval
    layer. Row-level only — chunk boundary computation (build_chunks), id
    minting and the kg_mutation_seq dirty bump stay in the facade."""

    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def source_elements_for_chunking(self, source_id: str) -> list:
        """元素 id 形如 el-<sid>-0001 零补位, 故 ORDER BY id == 插入顺序。
        额外带出 metadata 里的 caption：MinerU 带图注的 image 元素需凭它进检索
        chunk（build_chunks 对 image/figure 仅在无 caption 时跳过）。"""
        with self.database.connect() as db:
            erows = db.execute(
                "SELECT id, element_type, text, metadata FROM source_elements "
                "WHERE source_id=? ORDER BY id", (source_id,)).fetchall()
        out = []
        for r in erows:
            caption = ""
            raw = r["metadata"]
            if raw:
                try:
                    parsed = json.loads(raw)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    caption = str(parsed.get("caption") or "")
            out.append({"id": r["id"], "element_type": r["element_type"],
                        "text": r["text"], "caption": caption})
        return out

    def replace_source_chunks(
        self,
        source_id: str,
        notebook_id: str,
        chunks: Sequence[ChunkWrite],
        *,
        created_at: str,
        mark_chunked_at: str | None = None,
    ) -> None:
        """幂等:先删该 source 旧 chunk(级联删 chunk_embeddings)。chunk 行与其
        chunks_fts 行在同一个写事务里换血——FTS 插入失败连 chunk 行一起回滚。

        ``mark_chunked_at`` 非 None 时,在**同一事务**内把 sources.chunked_at 置成它
        (完成标记与它所认证的 chunk 数据原子提交——否则 0-chunk 成功的源崩在
        「chunks 已提交、marker 未提交」之间会留下 chunks=0+chunked_at=NULL,正好被
        H3 误判为损坏)。``build_chunks_for_source`` 传时间戳;knowhow 投影器**不传**
        (它按格子复用本方法、传空 chunks,那些隐藏源不该被打完成标记)。"""
        rows = [(c.id, notebook_id, source_id, c.text,
                 c.section_path, json.dumps(list(c.element_ids)), created_at)
                for c in chunks]
        with self.database.write() as db:
            # chunks_fts 是词法派生索引(无 source_id 列,不随 chunks 的 FK 级联),须同事务
            # 手动同步:先删本 source 旧 chunk 的 FTS 行(chunks DELETE 前 join 取 id),再重插。
            db.execute(
                "DELETE FROM chunks_fts WHERE chunk_id IN "
                "(SELECT id FROM chunks WHERE source_id=?)", (source_id,))
            db.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))  # 级联删 embeddings
            db.executemany(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?)", rows)
            self._insert_fts_rows(db, [(r[0], r[1], r[3]) for r in rows])
            if mark_chunked_at is not None:
                db.execute(
                    "UPDATE sources SET chunked_at = ? WHERE id = ?",
                    (mark_chunked_at, source_id))

    def _insert_fts_rows(self, connection: sqlite3.Connection, rows: list) -> None:
        connection.executemany(
            "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
            rows)

    # ------------------------------------------------- knowhow projection
    # (Task 5, knowhow-tables PR-1): the deterministic projector diffs and
    # rewrites chunks PER KNOWHOW ROW (and, within a row, per cell) — never
    # the whole source at once like replace_source_chunks above, since many
    # rows share one hidden source and a single-cell edit must not touch its
    # siblings' chunks (idempotency + "only the changed chunk gets
    # re-embedded" both depend on this).
    def rows_by_id_prefix(
        self, connection: sqlite3.Connection, source_id: str, id_prefix: str
    ) -> list:
        """This row's PRIOR chunks (any cell/part), by id LIKE prefix — chunk
        ids are ``chunk-kh-{hash(row_id)}-{part}``, so every part for one row
        shares this literal prefix (unlike element/KO ids, which hash
        row_id+column_id and so carry no shared per-row substring)."""
        return connection.execute(
            "SELECT id, text, section_path FROM chunks "
            "WHERE source_id = ? AND id LIKE ? ORDER BY id",
            (source_id, f"{id_prefix}%"),
        ).fetchall()

    def delete_by_ids(
        self, connection: sqlite3.Connection, chunk_ids: Sequence[str]
    ) -> None:
        """Delete an EXPLICIT chunk id list (+ their chunks_fts rows first,
        same FTS-before-base ordering as replace_source_chunks — chunks_fts
        has no FK cascade of its own). Precise per-cell deletion, as opposed
        to replace_source_chunks' whole-source wipe."""
        ids = list(chunk_ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        connection.execute(
            f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", ids
        )
        connection.execute(
            f"DELETE FROM chunks WHERE id IN ({placeholders})", ids
        )

    def insert_rows(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        source_id: str,
        rows: Sequence[ChunkWrite],
        *,
        created_at: str,
    ) -> None:
        """Insert-only half of replace_source_chunks (no delete-all first) —
        the projector does its own precise ``delete_by_ids`` beforehand."""
        values = [
            (c.id, notebook_id, source_id, c.text,
             c.section_path, json.dumps(list(c.element_ids)), created_at)
            for c in rows
        ]
        if not values:
            return
        connection.executemany(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)", values)
        self._insert_fts_rows(connection, [(v[0], v[1], v[3]) for v in values])

    def source_chunks(self, source_id: str) -> list:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT id, text FROM chunks WHERE source_id=?", (source_id,)).fetchall()
        return [{"id": r["id"], "text": r["text"]} for r in rows]

    @staticmethod
    def language_probe_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT text FROM ("
            "  SELECT rowid AS rid, text FROM chunks WHERE notebook_id=? "
            "  ORDER BY rowid LIMIT 30) "
            "UNION "
            "SELECT text FROM ("
            "  SELECT rowid AS rid, text FROM chunks WHERE notebook_id=? "
            "  ORDER BY rowid DESC LIMIT 30)",
            (notebook_id, notebook_id),
        ).fetchall()

    @staticmethod
    def retrieval_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            """
            SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids,
                   s.title AS source_title
            FROM chunks c JOIN sources s ON s.id = c.source_id
            WHERE c.notebook_id = ?
            """,
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def count_row(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()

    @staticmethod
    def hydrate_rows(db: sqlite3.Connection, chunk_ids: Sequence[str]):
        ids = list(chunk_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids, "
            f"s.title AS source_title FROM chunks c JOIN sources s ON s.id=c.source_id "
            f"WHERE c.id IN ({ph})", ids,
        ).fetchall()

    @staticmethod
    def graph_hydrate_rows(db: sqlite3.Connection, chunk_ids: Sequence[str]):
        ids = list(chunk_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids, "
            f"c.notebook_id AS chunk_notebook_id, s.title AS source_title "
            f"FROM chunks c JOIN sources s ON s.id=c.source_id "
            f"WHERE c.id IN ({ph})", ids,
        ).fetchall()

    @staticmethod
    def id_element_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT id, element_ids FROM chunks WHERE notebook_id=?",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def knowhow_chunk_rows(db: sqlite3.Connection, notebook_id: str):
        """``(id, element_ids)`` for chunks owned by the notebook's hidden
        knowhow source(s) — the tiny, bounded set gate-0 knowhow KG-node
        retrieval reverse-looks-up (default-on, env-reversible feature). Scoped to
        live ``knowhow_tables.hidden_source_id`` values so it is a table-local
        probe, never a notebook-wide or orphan-source scan."""
        return db.execute(
            "SELECT c.id,c.element_ids FROM knowhow_tables kt "
            "JOIN chunks c ON c.source_id=kt.hidden_source_id "
            "WHERE kt.notebook_id=? AND c.notebook_id=?",
            (notebook_id, notebook_id),
        ).fetchall()

    @staticmethod
    def knowhow_bridge_version_row(db: sqlite3.Connection, notebook_id: str):
        """Cheap generation row for the scoped Knowhow chunk-vector corpus.

        ``kg_mutation_seq`` covers projection structure, while this count/time
        pair also catches vector-only repair jobs, which deliberately do not
        mutate KG state.
        """
        return db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(ce.created_at), '') AS ts "
            "FROM knowhow_tables kt "
            "JOIN chunks c ON c.source_id=kt.hidden_source_id "
            "JOIN chunk_embeddings ce ON ce.chunk_id=c.id "
            "WHERE kt.notebook_id=? AND c.notebook_id=? AND ce.notebook_id=?",
            (notebook_id, notebook_id, notebook_id),
        ).fetchone()

    @staticmethod
    def rows_by_ids(db: sqlite3.Connection, chunk_ids: Sequence[str]):
        ids = list(chunk_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT id, source_id, text, section_path, element_ids "
            f"FROM chunks WHERE id IN ({ph})", ids,
        ).fetchall()

    @staticmethod
    def id_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT id FROM chunks WHERE notebook_id=?", (notebook_id,),
        ).fetchall()

    @staticmethod
    def backfill_fts(db: sqlite3.Connection, notebook_id: str) -> int:
        """从 chunks 重建 chunks_fts(DELETE+re-INSERT),返回写入行数(Task 26:
        SQL 正文自 facade 迁入;调用方持有唯一写事务边界)。"""
        db.execute("DELETE FROM chunks_fts WHERE notebook_id=?", (notebook_id,))
        rows = db.execute(
            "SELECT id, text FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchall()
        if rows:
            db.executemany(
                "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                [(r["id"], notebook_id, r["text"] or "") for r in rows])
        return len(rows)
