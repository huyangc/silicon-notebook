from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Mapping, Sequence

from app.repositories.chunk_elements import reverse_rows_for_writes
from app.repositories.like_pattern import escape_like_pattern
from app.repositories.ports import ChunkWrite
from app.repositories.sqlite.source_ceiling import ceiling_param, normalise_ceiling
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.id_binding import (
    bind_ids, drive_by, member_of, not_member_of,
)
from app.repositories.sqlite.memory_sql import (
    MEMORY_SOURCE_NOT_CHUNKED,
    memory_source_type_predicate,
)
from app.repositories.sqlite.source_store import VISIBLE_SOURCE_TYPES_PREDICATE
from app.domain.vector_index import encode_vector
from app.domain.indexing_pipeline import IndexingPipelineStalePlanError


# Bounded IN(...) fan-out for the element -> chunk point lookup. SQLite's
# default SQLITE_MAX_VARIABLE_NUMBER is far higher, but a fixed batch keeps the
# statement shape stable no matter how many evidence elements one query hit.
CHUNK_ELEMENT_LOOKUP_BATCH = 500

# ``ChunkStore.replace_source_chunks`` and ``ChunkStore.insert_rows`` raise
# ``ValueError(MEMORY_SOURCE_NOT_CHUNKED)`` when the target source is a private
# Memory projection. Passages are a SHARED index (every notebook member
# retrieves from them) while Memory is private per user, so a Memory source
# must never own a chunk row. The message and the type predicate come from
# ``memory_sql`` (the one definition of "Memory source").
#
# Which chunk write paths refuse, and which do not (the static guard
# ``tests/test_memory_chunk_write_guard.py`` enumerates every write of the
# ``chunks`` table and fails on an unlisted one):
#   * refuse with ``_refuse_memory_source``: this file's two methods, the KG
#     build publish (``kg_build_job_store``), the Knowhow transfer insert
#     (``knowhow_transfer_store``) and the sync import (``migration/sync``);
#   * changes existing rows only / moves existing rows only: ``maintenance``
#     and the ``migration`` mirrors, listed with that reason in the guard;
#   * the notebook copy path (``NotebookCopyService.copy_notebook`` ->
#     ``sharing_store.insert_copy_rows("chunks")``) does not call the refusal.
#     Which chunk rows it copies is decided by the ``chunks`` query of the copy
#     statement set ``sharing_store`` uses for a notebook that holds a Memory
#     source: its only set ``_COPY_SNAPSHOT_QUERIES`` when there is one set, or,
#     where ``SharingStore._copy_queries`` chooses per copy (task E5-1), the set
#     it returns when its probe ``_COPY_DIRTY_SQL`` finds a Memory source,
#     ``_MEMORY_COPY_SNAPSHOT_QUERIES``. When that query carries
#     ``NOT memory_sql.memory_derived_*(<alias>)`` (``memory_derived_object``,
#     ``memory_derived_in_notebook``) a Memory source's rows are not copied.
#     The guard checks on every run, on both backends, whether it does and lists
#     the path as guarded or unguarded accordingly (``copy_path_reason``).


#: 两个写方法的 Memory 探针:对来源做一次主键点查(计划钉在
#: ``tests/test_memory_chunk_write_sqlite_plans.py``)。
MEMORY_PROBE_SQL = "SELECT 1 FROM sources WHERE id = ? AND " + memory_source_type_predicate()


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
            ceiling = bind_ids(source_ids, sort=True)
            # ``member_of``: walk ``q.id`` order and stop at ``LIMIT``; a
            # ceiling-driven plan fetched and sorted every question of every
            # listed source (49k ids: 78 ms vs 16 ms; one id pays a bounded
            # notebook scan, 8 ms on 98k questions).
            source_clause = f"AND {member_of('q.source_id', ceiling)} "
            params.append(ceiling.param)
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
        sources = bind_ids(values)
        # ``+c.notebook_id``: without planner statistics (production never
        # runs ANALYZE) the notebook index would win and every requested id
        # would rescan the notebook's chunks (2,000 ids: 2.6 s); the unary
        # plus leaves ``idx_chunks_source`` as the only candidate.
        if presence_only:
            # The ordinal of each requested id is the output order, so the
            # JSON array is walked directly rather than through a predicate.
            return db.execute(
                "SELECT requested.value AS source_id "
                "FROM json_each(?) AS requested "
                "WHERE EXISTS (SELECT 1 FROM chunks c "
                "WHERE +c.notebook_id=? AND c.source_id=requested.value) "
                "ORDER BY CAST(requested.key AS INTEGER)",
                (sources.param, notebook_id),
            ).fetchall()
        # ``drive_by``: every chunk of the listed sources is the answer, so one
        # ``idx_chunks_source`` seek per id is proportional to the output.
        return db.execute(
            "SELECT id FROM chunks WHERE +notebook_id=? "
            f"AND {drive_by('source_id', sources)}",
            (notebook_id, sources.param),
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
            # 探测与写入同一个写事务:write() 的连接是 sqlite3 旧式事务控制,
            # SELECT 不开事务、要到 DELETE 才开——先 BEGIN IMMEDIATE,否则另一进程
            # 可在「探测通过」与「首条写入」之间改掉来源。在任何 DELETE 之前拒绝,
            # 被拒的写入不动该来源已有的行。write() 每次递出全新连接,此处无已开事务。
            self.database.begin_immediate(db)
            self._refuse_memory_source(db, source_id)
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

    @staticmethod
    def _refuse_memory_source(connection: sqlite3.Connection, source_id: str) -> None:
        """``replace_source_chunks`` / ``insert_rows`` 两个写方法的守卫:每次写调用、
        每个来源只探一次主键(绝不逐 chunk 行);Memory 来源是用户私有的,不许在
        共享段落索引里拥有行。在**调用方连接**上执行,事务归调用方
        (``replace_source_chunks`` 自己先 BEGIN IMMEDIATE)。未知 source_id 这里不拒,
        chunks 外键照旧在插入时拒。

        它**不**做的事:探针只保证「写入那一刻来源已经是 Memory」时被拒,防不住类型
        **事后**被改成 Memory。PostgreSQL(READ COMMITTED)上实测:探针是普通 SELECT,
        并发的 ``UPDATE sources SET source_type='memory'`` 照样提交,探针加 ``FOR KEY
        SHARE`` / ``FOR SHARE`` 也一样。所以「Memory 来源名下没有 chunk」这条不变量
        依赖 ``sources.source_type`` 插入后永不改变:静态守卫
        (``test_memory_chunk_write_guard.py``)断言除 INSERT 外没有语句写这一列,表可能是
        ``sources`` 的通用写者都登记在那里;同步导入的 ``ON CONFLICT (id) DO UPDATE`` 在运行时
        把关:包里某来源 id 的类型与目标端不同、且其中一方是 Memory 时,整轮导入在应用任何
        表之前被拒绝。(SQLite 的 BEGIN IMMEDIATE 关掉了 ``replace_source_chunks``
        里探针与写入之间的窗口,但提交之后类型再被改,仍只有不可变性防得住。)"""
        row = connection.execute(MEMORY_PROBE_SQL, (source_id,)).fetchone()
        if row is not None:
            raise ValueError(MEMORY_SOURCE_NOT_CHUNKED)

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
        # Every chunk of a re-projected knowhow table can be listed: one JSON
        # parameter, each id a primary-key seek (``drive_by``).
        chunks = bind_ids(ids)
        connection.execute(
            f"DELETE FROM chunks_fts WHERE {drive_by('chunk_id', chunks)}",
            (chunks.param,),
        )
        connection.execute(
            f"DELETE FROM chunks WHERE {drive_by('id', chunks)}",
            (chunks.param,),
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
        # 事务归调用方:探测跑在调用方这条连接上(Knowhow 投影器在同一个写事务里已先写过
        # 元素行),与下面的 INSERT 同一事务。
        self._refuse_memory_source(connection, source_id)
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
    def graph_hydrate_rows(
        db: sqlite3.Connection,
        chunk_ids: Sequence[str],
        *,
        allowed_source_ids: Mapping[str, Iterable[str] | None] | None = None,
    ):
        """Hydrate one window of PPR-ranked chunk ids; ``allowed_source_ids``
        is the per-library ceiling map of the PostgreSQL twin, with the same
        semantics (a listed library keeps only its listed sources, an empty
        list denies it, nothing listed = the historical statement, byte for
        byte).  The candidate primary keys drive the read with and without
        planner statistics; each list is ONE ``json_each`` parameter
        (``id_binding.member_of``, the non-driving ``+col IN`` form), so a
        49k-source ceiling never approaches the variable limit."""
        ids = list(chunk_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        sql = (
            f"SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids, "
            f"c.notebook_id AS chunk_notebook_id, s.title AS source_title "
            f"FROM chunks c JOIN sources s ON s.id=c.source_id "
            f"WHERE c.id IN ({ph})"
        )
        clause, params = _library_source_ceiling_clause(allowed_source_ids or {})
        if clause is None:
            return db.execute(sql, ids).fetchall()
        return db.execute(f"{sql} AND {clause}", [*ids, *params]).fetchall()

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
        # ``chunk_ids`` is one ``_in_batches`` window (<= 900 candidate primary
        # keys): they drive the lookup; the ceiling only filters.  The notebook
        # predicate is ``+c.notebook_id`` so that, without planner statistics
        # (production), the notebook index cannot win over the primary key
        # (49k ceiling: 19.4 ms notebook scan vs 4.5 ms by primary key).
        id_placeholders = ",".join("?" for _ in ids)
        source_clause = ""
        params: list[object] = [notebook_id, *ids]
        if source_mode in {"include", "exclude"} and sources:
            ceiling = bind_ids(sources, sort=True)
            predicate = member_of if source_mode == "include" else not_member_of
            source_clause = f" AND {predicate('c.source_id', ceiling)}"
            params.append(ceiling.param)
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
            f"WHERE +c.notebook_id=? AND c.id IN ({id_placeholders})"
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


def _library_source_ceiling_clause(
    ceilings: Mapping[str, Iterable[str] | None],
) -> tuple[str | None, list]:
    """The SQLite twin of ``postgres/chunk_store._library_source_ceiling_clause``
    (the rule is stated there).  The list tests carry the unary ``+``
    (``id_binding.member_of`` / ``not_member_of``) so neither the notebook nor
    the source index can outbid the candidate primary keys when
    ``sqlite_stat1`` is empty.  The per-library notebook equality needs none:
    it sits inside an OR whose first arm (``+c.notebook_id NOT IN ...``) no
    index can serve, so SQLite can never answer the OR from an index and the
    equality is only ever evaluated on rows the primary keys fetched (pinned
    by ``test_store_plan_is_driven_by_candidate_keys`` with and without
    ``ANALYZE``)."""
    listed = {
        str(notebook_id): ids for notebook_id, ids in ceilings.items()
        if ids is not None
    }
    if not listed:
        return None, []
    notebooks = bind_ids(sorted(listed))
    arms = [not_member_of("c.notebook_id", notebooks)]
    params: list = [notebooks.param]
    for notebook_id in sorted(listed):
        ceiling = normalise_ceiling(listed[notebook_id])
        if not ceiling:
            continue
        bound = ceiling_param(ceiling)
        arms.append(f"(c.notebook_id = ? AND {member_of('c.source_id', bound)})")
        params.extend((notebook_id, bound.param))
    return f"({' OR '.join(arms)})", params
