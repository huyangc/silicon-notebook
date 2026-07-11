from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Sequence

from app.repositories.sqlite.database import SqliteDatabase


@dataclass(frozen=True)
class ChunkWrite:
    id: str
    text: str
    section_path: str
    element_ids: tuple


class ChunkStore:
    """SQLite chunks/chunks_fts row persistence for the chunk-native retrieval
    layer. Row-level only — chunk boundary computation (build_chunks), id
    minting and the kg_mutation_seq dirty bump stay in the facade."""

    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def source_elements_for_chunking(self, source_id: str) -> list:
        """元素 id 形如 el-<sid>-0001 零补位, 故 ORDER BY id == 插入顺序。"""
        with self.database.connect() as db:
            erows = db.execute(
                "SELECT id, element_type, text FROM source_elements "
                "WHERE source_id=? ORDER BY id", (source_id,)).fetchall()
        return [{"id": r["id"], "element_type": r["element_type"], "text": r["text"]}
                for r in erows]

    def replace_source_chunks(
        self,
        source_id: str,
        notebook_id: str,
        chunks: Sequence[ChunkWrite],
        *,
        created_at: str,
    ) -> None:
        """幂等:先删该 source 旧 chunk(级联删 chunk_embeddings)。chunk 行与其
        chunks_fts 行在同一个写事务里换血——FTS 插入失败连 chunk 行一起回滚。"""
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

    def _insert_fts_rows(self, connection: sqlite3.Connection, rows: list) -> None:
        connection.executemany(
            "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
            rows)

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
