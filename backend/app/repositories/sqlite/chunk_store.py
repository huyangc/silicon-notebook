from __future__ import annotations

import json
import sqlite3
from typing import Sequence

from app.repositories.chunk_elements import reverse_rows_for_writes
from app.repositories.like_pattern import escape_like_pattern
from app.repositories.ports import ChunkWrite
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.source_store import VISIBLE_SOURCE_TYPES_PREDICATE
from app.domain.vector_index import encode_vector
from app.domain.indexing_pipeline import IndexingPipelineStalePlanError


# Bounded IN(...) fan-out for the element -> chunk point lookup. SQLite's
# default SQLITE_MAX_VARIABLE_NUMBER is far higher, but a fixed batch keeps the
# statement shape stable no matter how many evidence elements one query hit.
CHUNK_ELEMENT_LOOKUP_BATCH = 500


class ChunkStore:
    """SQLite chunks/chunks_fts row persistence for the chunk-native retrieval
    layer. Row-level only — chunk boundary computation (build_chunks), id
    minting and the kg_mutation_seq dirty bump stay in the facade."""

    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def question_index_chunk_page(
        self,
        notebook_id: str,
        *,
        after_id: str,
        limit: int,
        include_existing: bool,
    ) -> list[dict]:
        existing = "" if include_existing else "AND c.question_indexed_at IS NULL "
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT c.id AS chunk_id,c.source_id,c.text,c.section_path "
                "FROM chunks c WHERE c.notebook_id=? AND c.id>? "
                f"{existing}ORDER BY c.id LIMIT ?",
                (notebook_id, after_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_chunk_questions(
        self,
        chunk_id: str,
        notebook_id: str,
        source_id: str,
        rows: Sequence[tuple[str, str, object]],
        *,
        created_at: str,
    ) -> None:
        encoded = [
            (question_id, chunk_id, notebook_id, source_id, question,
             encode_vector(vector), created_at)
            for question_id, question, vector in rows
        ]
        with self.database.write() as db:
            db.execute("DELETE FROM chunk_questions WHERE chunk_id=?", (chunk_id,))
            db.executemany(
                "INSERT INTO chunk_questions "
                "(id,chunk_id,notebook_id,source_id,question,vector,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                encoded,
            )
            db.execute(
                "UPDATE chunks SET question_indexed_at=? WHERE id=?",
                (created_at, chunk_id),
            )

    def question_index_rows(
        self,
        notebook_id: str,
        *,
        actor_id: str,
        allowed_source_ids: Sequence[str] | None,
        limit: int,
    ) -> list[dict]:
        params: list[object] = [notebook_id, actor_id]
        source_clause = ""
        if allowed_source_ids is not None:
            source_ids = list(dict.fromkeys(allowed_source_ids))
            if not source_ids:
                return []
            source_clause = (
                "AND q.source_id IN ("
                "SELECT CAST(value AS TEXT) FROM json_each(?)"
                ") "
            )
            params.append(json.dumps(source_ids))
        params.append(int(limit))
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT q.id,q.chunk_id,q.source_id,q.vector "
                "FROM chunk_questions q JOIN chunks c "
                "ON c.id=q.chunk_id AND c.notebook_id=q.notebook_id "
                "AND c.source_id=q.source_id JOIN sources s "
                "ON s.id=c.source_id AND s.notebook_id=c.notebook_id "
                "WHERE q.notebook_id=? AND (s.source_type!='memory' OR EXISTS ("
                "SELECT 1 FROM memory_items m WHERE m.id=s.memory_id "
                "AND m.notebook_id=q.notebook_id AND m.created_by=?)) "
                + source_clause + "ORDER BY q.id LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def question_index_stats(self, notebook_id: str) -> dict[str, int]:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM chunk_questions WHERE notebook_id=?) "
                "AS questions,"
                "(SELECT COUNT(*) FROM chunks WHERE notebook_id=? "
                "AND question_indexed_at IS NOT NULL) AS chunks,"
                "(SELECT COUNT(DISTINCT chunk_id) FROM chunk_questions "
                "WHERE notebook_id=?) AS question_chunks",
                (notebook_id, notebook_id, notebook_id),
            ).fetchone()
        return {
            "questions": int(row["questions"]),
            "chunks": int(row["chunks"]),
            "question_chunks": int(row["question_chunks"]),
        }

    @staticmethod
    def ids_for_sources(
        db,
        notebook_id: str,
        source_ids: Sequence[str],
        *,
        presence_only: bool = False,
    ):
        values = list(dict.fromkeys(source_ids))
        if not values:
            return []
        if presence_only:
            return db.execute(
                "SELECT CAST(requested.value AS TEXT) AS source_id "
                "FROM json_each(?) AS requested "
                "WHERE EXISTS (SELECT 1 FROM chunks c "
                "WHERE c.notebook_id=? "
                "AND c.source_id=CAST(requested.value AS TEXT)) "
                "ORDER BY CAST(requested.key AS INTEGER)",
                (json.dumps(values), notebook_id),
            ).fetchall()
        return db.execute(
            "SELECT id FROM chunks WHERE notebook_id=? "
            "AND source_id IN (SELECT CAST(value AS TEXT) FROM json_each(?))",
            (notebook_id, json.dumps(values)),
        ).fetchall()

    def source_elements_for_chunking(self, source_id: str) -> list:
        """元素 id 形如 el-<sid>-0001 零补位, 故 ORDER BY id == 插入顺序。
        额外带出 metadata 里的 caption 与 description：MinerU 带图注的 image 元素
        需凭前者进检索 chunk，markdown 的 `> **图片描述**` 引用块凭后者（没有 alt
        的图只有描述这一个入口；build_chunks 对 image/figure 仅在两者皆空时跳过）。
        同时带出
        section_path（markdown 解析路径存的完整标题面包屑，含自身、" > " 分隔）：
        build_chunks 的 heading 分支用它代替标题自身文本作 section 标签，避免子标题
        （如 Arguments/Examples）覆盖掉上级标题（命令名）；缺省时 build_chunks 自行
        回退到标题自身文本，字节不变。"""
        with self.database.connect() as db:
            erows = db.execute(
                "SELECT id, element_type, text, metadata FROM source_elements "
                "WHERE source_id=? ORDER BY id", (source_id,)).fetchall()
        out = []
        for r in erows:
            caption = ""
            description = ""
            section_path = ""
            raw = r["metadata"]
            if raw:
                try:
                    parsed = json.loads(raw)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    caption = str(parsed.get("caption") or "")
                    description = str(parsed.get("description") or "")
                    section_path = str(parsed.get("section_path") or "")
            out.append({"id": r["id"], "element_type": r["element_type"],
                        "text": r["text"], "caption": caption,
                        "description": description,
                        "section_path": section_path})
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
            # element -> chunk reverse rows, same transaction as the chunk rows
            # they describe. The old rows are already gone: chunk_elements has
            # ``REFERENCES chunks(id) ON DELETE CASCADE`` and every connection
            # runs with ``PRAGMA foreign_keys = ON`` (SqliteDatabase), so the
            # DELETE above took them with it — exactly like chunk_embeddings.
            self._insert_chunk_element_rows(db, notebook_id, chunks)
            if mark_chunked_at is not None:
                db.execute(
                    "UPDATE sources SET chunked_at = ? WHERE id = ?",
                    (mark_chunked_at, source_id))

    def _insert_fts_rows(self, connection: sqlite3.Connection, rows: list) -> None:
        connection.executemany(
            "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
            rows)

    @staticmethod
    def chunk_element_rows(
        notebook_id: str, chunks: Sequence[ChunkWrite]
    ) -> list[tuple[str, str, str]]:
        """``(notebook_id, element_id, chunk_id)`` reverse rows for these writes.

        Shaping (including de-duplication within the batch) is the shared,
        backend-neutral helper the offline backfill also uses, so a chunk
        written online and the same chunk projected offline produce byte-for-byte
        identical rows."""
        return reverse_rows_for_writes(
            notebook_id, [(chunk.id, list(chunk.element_ids)) for chunk in chunks]
        )

    def _insert_chunk_element_rows(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        chunks: Sequence[ChunkWrite],
    ) -> None:
        rows = self.chunk_element_rows(notebook_id, chunks)
        if rows:
            connection.executemany(
                "INSERT OR IGNORE INTO chunk_elements "
                "(notebook_id,element_id,chunk_id) VALUES (?,?,?)", rows)

    @staticmethod
    def chunks_for_element_ids(
        db: sqlite3.Connection, notebook_id: str, element_ids: Sequence[str]
    ):
        """``(element_id, chunk_id)`` rows for these elements, in chunk order.

        The fast half of the element -> chunk reverse lookup: an indexed seek
        on the ``(notebook_id, element_id, chunk_id)`` primary key instead of a
        whole-notebook chunk scan with per-row ``json.loads``.

        ``ORDER BY c.rowid`` is insertion order, which is what the legacy
        whole-table scan happened to produce. That order was never a contract
        (see ``_kg_source_chunks``), but the consumer's truncation is
        order-sensitive, so the replacement must at least be deterministic —
        chunk ids are random surrogates, so ordering by id would shuffle.
        Batching is by element id, so every row for one element stays inside a
        single ordered statement."""
        ids = list(dict.fromkeys(e for e in element_ids if e))
        rows: list = []
        for offset in range(0, len(ids), CHUNK_ELEMENT_LOOKUP_BATCH):
            batch = ids[offset : offset + CHUNK_ELEMENT_LOOKUP_BATCH]
            placeholders = ",".join("?" for _ in batch)
            rows.extend(
                db.execute(
                    f"SELECT ce.element_id AS element_id, ce.chunk_id AS chunk_id "
                    f"FROM chunk_elements ce JOIN chunks c ON c.id = ce.chunk_id "
                    f"WHERE ce.notebook_id = ? AND ce.element_id IN ({placeholders}) "
                    f"ORDER BY c.rowid",
                    (notebook_id, *batch),
                ).fetchall()
            )
        return rows

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
        # The projector's precise ``delete_by_ids`` already dropped the prior
        # rows for these chunks via the chunks cascade; add the new ones in the
        # same transaction so the reverse index never lags its chunk rows.
        self._insert_chunk_element_rows(connection, notebook_id, rows)

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
    def retrieval_contribution_rows(
        db: sqlite3.Connection,
        notebook_id: str,
        chunk_ids: Sequence[str],
        *,
        actor_id: str,
        source_mode: str | None,
        source_ids: Sequence[str],
    ):
        ids = list(dict.fromkeys(chunk_ids))
        sources = list(dict.fromkeys(source_ids))
        if not ids or (source_mode == "include" and not sources):
            return []
        id_placeholders = ",".join("?" for _ in ids)
        source_clause = ""
        params: list[object] = [notebook_id, *ids]
        if source_mode in {"include", "exclude"} and sources:
            operator = "IN" if source_mode == "include" else "NOT IN"
            source_clause = (
                f" AND c.source_id {operator} ("
                "SELECT CAST(value AS TEXT) FROM json_each(?)"
                ")"
            )
            params.append(json.dumps(sources))
        memory_clause = (
            " AND (s.source_type <> 'memory' OR EXISTS ("
            "SELECT 1 FROM memory_items m "
            "WHERE m.id=s.memory_id AND m.created_by=?))"
        )
        params.append(actor_id)
        return db.execute(
            "SELECT c.id,c.source_id,c.text,c.section_path,c.element_ids,"
            "c.notebook_id AS chunk_notebook_id,s.title AS source_title "
            "FROM chunks c JOIN sources s "
            "ON s.id=c.source_id AND s.notebook_id=c.notebook_id "
            f"WHERE c.notebook_id=? AND c.id IN ({id_placeholders})"
            f"{source_clause}{memory_clause}",
            params,
        ).fetchall()

    @staticmethod
    def id_element_rows(
        db: sqlite3.Connection, notebook_id: str, page_rows: int | None = None
    ):
        """``page_rows`` (batch-3 W4, codex #676) mirrors the PostgreSQL
        keyset-paged sibling's parameter so callers can pass
        ``settings.graph_fetch_page_rows`` uniformly across backends; SQLite
        accepts and ignores it — this read has never been paged here (see
        the port docstring)."""
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
    def chunks_by_section(
        db: sqlite3.Connection,
        notebook_id: str,
        source_id: str,
        section_path: str,
        limit: int,
    ):
        """One section's chunks — that node PLUS its descendants — in document
        order, for the exact-identifier fast path's "fetch the whole section".

        `section_path` holds the full breadcrumb (`Commands > set_db >
        Arguments`), so the subtree predicate is equality OR the
        ``<path> > %`` prefix. Legacy rows hold a single heading instead: the
        prefix branch then simply matches nothing and equality still works,
        which is exactly the degraded-but-correct behaviour those libraries
        should get.

        The pattern is escaped (`escape_like_pattern` + the ESCAPE clause
        SQLite requires, since it has no default escape character) because
        command names contain `_`, LIKE's single-character wildcard.

        `ORDER BY rowid` is document order: chunk ids are random 128-bit
        surrogates, so ordering by id would shuffle a section's parts. `LIMIT`
        is a hard bound — a pathological section can never dump a source.
        """
        path = section_path or ""
        if not path or limit <= 0:
            return []
        return db.execute(
            "SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids, "
            "s.title AS source_title "
            "FROM chunks c JOIN sources s ON s.id=c.source_id "
            "WHERE c.notebook_id=? AND c.source_id=? "
            "AND (c.section_path=? OR c.section_path LIKE ? ESCAPE '\\') "
            "ORDER BY c.rowid LIMIT ?",
            (notebook_id, source_id, path,
             escape_like_pattern(path) + " > %", int(limit)),
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
