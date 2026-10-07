from __future__ import annotations

import json
from typing import Iterable, Mapping, Sequence

from app.repositories.chunk_elements import reverse_rows_for_writes
from app.repositories.ports import ChunkWrite
from app.repositories.postgres._store_utils import (
    GRAPH_FETCH_BATCH,
    TimestampInput,
    execute_many,
    iso_timestamp,
    json_value,
    jsonb,
    keyset_pages,
    normalize_timestamp,
    placeholders,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.source_ceiling import ceiling_param, normalise_ceiling
from app.repositories.postgres.id_binding import (
    bind_ids,
    execute_bound,
    execute_ids,
    member_of,
    not_member_of,
)
from app.repositories.postgres.memory_sql import (
    MEMORY_SOURCE_NOT_CHUNKED,
    memory_source_type_predicate,
)
from app.repositories.postgres.source_store import VISIBLE_SOURCE_TYPES_PREDICATE
from app.repositories.postgres.search import chunk_section_rows
from app.domain.vector_index import encode_vector
from app.domain.indexing_pipeline import IndexingPipelineStalePlanError


# Bounded fan-out for the element -> chunk point lookup. Deliberately a local
# constant with the same value as the SQLite adapter's: adapters never import
# one another, and a fixed batch keeps the statement shape stable no matter how
# many evidence elements one query hit.
CHUNK_ELEMENT_LOOKUP_BATCH = 500

#: The refusal probe of the two write methods: a primary-key point lookup.
MEMORY_PROBE_SQL = "SELECT 1 FROM sources WHERE id=%s AND " + memory_source_type_predicate()

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


def _compat_element_ids(row: dict) -> dict:
    result = dict(row)
    if "element_ids" in result and not isinstance(result["element_ids"], str):
        result["element_ids"] = json.dumps(result["element_ids"] or [])
    return result


class ChunkStore:
    """PostgreSQL chunk persistence; search indexes derive from base rows."""

    def __init__(self, database: PostgresDatabase) -> None:
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
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT c.id AS chunk_id,c.source_id,c.text,c.section_path "
                "FROM chunks c WHERE c.notebook_id=%s AND c.id>%s "
                f"{existing}ORDER BY c.id COLLATE \"C\" LIMIT %s",
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
        created = normalize_timestamp(created_at)
        encoded = [
            (question_id, chunk_id, notebook_id, source_id, question,
             encode_vector(vector), created)
            for question_id, question, vector in rows
        ]
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM chunk_questions WHERE chunk_id=%s", (chunk_id,)
            )
            execute_many(
                connection,
                "INSERT INTO chunk_questions "
                "(id,chunk_id,notebook_id,source_id,question,vector,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                encoded,
            )
            connection.execute(
                "UPDATE chunks SET question_indexed_at=%s WHERE id=%s",
                (created, chunk_id),
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
        ceiling = None
        if allowed_source_ids is not None:
            source_ids = list(dict.fromkeys(allowed_source_ids))
            if not source_ids:
                return []
            ceiling = bind_ids(source_ids)
            # ``member_of``: the list filters the ``q.id`` walk under LIMIT; the
            # semi-join form was slower (49k: 273 -> 633 ms uniform).
            source_clause = f"AND {member_of('q.source_id', ceiling)} "
            params.append(ceiling.param)
        params.append(int(limit))
        with self.database.connect() as connection:
            rows = execute_bound(
                connection,
                "SELECT q.id,q.chunk_id,q.source_id,q.vector "
                "FROM chunk_questions q JOIN chunks c "
                "ON c.id=q.chunk_id AND c.notebook_id=q.notebook_id "
                "AND c.source_id=q.source_id JOIN sources s "
                "ON s.id=c.source_id AND s.notebook_id=c.notebook_id "
                "WHERE q.notebook_id=%s AND (s.source_type!='memory' OR EXISTS ("
                "SELECT 1 FROM memory_items m WHERE m.id=s.memory_id "
                "AND m.notebook_id=q.notebook_id AND m.created_by=%s)) "
                + source_clause + "ORDER BY q.id COLLATE \"C\" LIMIT %s",
                params,
                ceiling,
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            if isinstance(item.get("vector"), memoryview):
                item["vector"] = item["vector"].tobytes()
            output.append(item)
        return output

    def question_index_stats(self, notebook_id: str) -> dict[str, int]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM chunk_questions WHERE notebook_id=%s) "
                "AS questions,"
                "(SELECT COUNT(*) FROM chunks WHERE notebook_id=%s "
                "AND question_indexed_at IS NOT NULL) AS chunks,"
                "(SELECT COUNT(DISTINCT chunk_id) FROM chunk_questions "
                "WHERE notebook_id=%s) AS question_chunks",
                (notebook_id, notebook_id, notebook_id),
            ).fetchone()
        return {
            "questions": int(row["questions"]),
            "chunks": int(row["chunks"]),
            "question_chunks": int(row["question_chunks"]),
        }

    @staticmethod
    def ids_for_sources(
        connection,
        notebook_id: str,
        source_ids: Sequence[str],
        *,
        presence_only: bool = False,
    ):
        values = list(dict.fromkeys(source_ids))
        if not values:
            return []
        sources = bind_ids(values)
        if presence_only:
            # The ordinal of each requested id is the output order, so the
            # list is unnested directly (WITH ORDINALITY) rather than through
            # a membership predicate; a custom plan costs its true length.
            return execute_ids(
                connection,
                "SELECT requested.source_id FROM "
                f"unnest({sources.sql}) WITH ORDINALITY "
                "AS requested(source_id, ordinal) "
                "WHERE EXISTS (SELECT 1 FROM chunks c "
                "WHERE c.notebook_id=%s "
                "AND c.source_id=requested.source_id) "
                "ORDER BY requested.ordinal",
                (sources.param, notebook_id),
            ).fetchall()
        # ``member_of``: every chunk of the listed sources; the semi-join form
        # was slower (49k: 27 -> 110 ms uniform).
        return execute_ids(
            connection,
            "SELECT id FROM chunks WHERE notebook_id=%s "
            f"AND {member_of('source_id', sources)}",
            (notebook_id, sources.param),
        ).fetchall()

    def source_elements_for_chunking(self, source_id: str) -> list[dict]:
        """额外带出 metadata 里的 caption、description 与 section_path，语义与 SQLite 侧
        ChunkStore.source_elements_for_chunking 逐字对等：section_path 是
        markdown 解析路径存的完整标题面包屑（含自身、" > " 分隔），供
        build_chunks 的 heading 分支代替标题自身文本作 section 标签；缺省时
        build_chunks 自行回退到标题自身文本，字节不变。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,element_type,text,metadata FROM source_elements "
                "WHERE source_id=%s ORDER BY id COLLATE \"C\"",
                (source_id,),
            ).fetchall()
        output = []
        for row in rows:
            metadata = json_value(row["metadata"], {})
            is_dict = isinstance(metadata, dict)
            caption = str(metadata.get("caption") or "") if is_dict else ""
            description = str(metadata.get("description") or "") if is_dict else ""
            section_path = str(metadata.get("section_path") or "") if is_dict else ""
            output.append(
                {
                    "id": row["id"],
                    "element_type": row["element_type"],
                    "text": row["text"],
                    "caption": caption,
                    "description": description,
                    "section_path": section_path,
                }
            )
        return output

    def replace_source_chunks(
        self,
        source_id: str,
        notebook_id: str,
        chunks: Sequence[ChunkWrite],
        *,
        created_at: TimestampInput,
        mark_chunked_at: TimestampInput | None = None,
    ) -> None:
        created_at = normalize_timestamp(created_at)
        rows = [
            (
                chunk.id,
                notebook_id,
                source_id,
                chunk.text,
                chunk.section_path,
                jsonb(list(chunk.element_ids)),
                created_at,
            )
            for chunk in chunks
        ]
        with self.database.write() as connection:
            # Probe on the write transaction's own connection, before the DELETE,
            # so a refused write leaves whatever the source already owns untouched.
            self._refuse_memory_source(connection, source_id)
            connection.execute("DELETE FROM chunks WHERE source_id=%s", (source_id,))
            execute_many(
                connection,
                "INSERT INTO chunks"
                "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )
            self._insert_fts_rows(
                connection, [(row[0], row[1], row[3]) for row in rows]
            )
            # element -> chunk reverse rows, same transaction as the chunk rows
            # they describe. The old rows are already gone: chunk_elements has
            # ``REFERENCES chunks(id) ON DELETE CASCADE``, so the DELETE above
            # took them with it.
            self._insert_chunk_element_rows(connection, notebook_id, chunks)
            if mark_chunked_at is not None:
                connection.execute(
                    "UPDATE sources SET chunked_at=%s WHERE id=%s",
                    (normalize_timestamp(mark_chunked_at), source_id),
                )

    @staticmethod
    def _refuse_memory_source(connection, source_id: str) -> None:
        """Guard of the two ``ChunkStore`` write methods (``replace_source_chunks``,
        ``insert_rows``): one primary-key probe per source per write call (never
        per chunk row), on the CALLER's connection so it shares the write's
        transaction. A Memory source is private per user and must not own rows
        in the shared passage index. An unknown source id is not refused here;
        the chunks foreign key rejects it on insert as before.

        What this probe does NOT do: it protects a write at the moment the
        source IS a Memory source; it cannot protect against the type changing
        LATER. Under READ COMMITTED the probe is a plain SELECT and a concurrent
        ``UPDATE sources SET source_type='memory'`` commits regardless (measured,
        with no lock, ``FOR KEY SHARE`` and ``FOR SHARE`` on the probe alike), so
        the invariant "no chunk under a Memory source" rests on
        ``sources.source_type`` never changing after insert. That immutability is
        enforced statically (``test_memory_chunk_write_guard.py``: no statement
        other than an INSERT writes the column; the generic writers whose table can
        be ``sources`` are listed there) and, for the sync import's
        ``ON CONFLICT (id) DO UPDATE``, at run time: a package in which a source id
        has a different type than at the target, one of the two being Memory, is
        refused for the whole run before any table is applied."""
        row = connection.execute(MEMORY_PROBE_SQL, (source_id,)).fetchone()
        if row is not None:
            raise ValueError(MEMORY_SOURCE_NOT_CHUNKED)

    def _insert_fts_rows(self, connection, rows: list) -> None:
        # PostgreSQL's GIN/trigram indexes update with chunks themselves.
        del connection, rows

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
        self, connection, notebook_id: str, chunks: Sequence[ChunkWrite]
    ) -> None:
        rows = self.chunk_element_rows(notebook_id, chunks)
        if rows:
            execute_many(
                connection,
                "INSERT INTO chunk_elements (notebook_id,element_id,chunk_id) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                rows,
            )

    @staticmethod
    def chunks_for_element_ids(
        connection, notebook_id: str, element_ids: Sequence[str]
    ):
        """``(element_id, chunk_id)`` rows for these elements, in chunk order.

        The fast half of the element -> chunk reverse lookup: an indexed seek
        on the ``(notebook_id, element_id, chunk_id)`` primary key instead of a
        whole-notebook chunk scan with per-row JSON decoding.

        ``ORDER BY c.ordinal`` is the PostgreSQL counterpart of SQLite's
        ``rowid`` insertion order (see POSTGRES_ROWID_ORDINAL_TABLES)."""
        ids = list(dict.fromkeys(e for e in element_ids if e))
        rows: list = []
        for offset in range(0, len(ids), CHUNK_ELEMENT_LOOKUP_BATCH):
            batch = ids[offset : offset + CHUNK_ELEMENT_LOOKUP_BATCH]
            rows.extend(
                connection.execute(
                    "SELECT ce.element_id AS element_id, ce.chunk_id AS chunk_id "
                    "FROM chunk_elements ce JOIN chunks c ON c.id = ce.chunk_id "
                    "WHERE ce.notebook_id=%s AND ce.element_id=ANY(%s) "
                    "ORDER BY c.ordinal",
                    (notebook_id, batch),
                ).fetchall()
            )
        return rows

    def rows_by_id_prefix(self, connection, source_id: str, id_prefix: str) -> list:
        return connection.execute(
            "SELECT id,text,section_path FROM chunks "
            "WHERE source_id=%s AND id LIKE %s ORDER BY id COLLATE \"C\"",
            (source_id, f"{id_prefix}%"),
        ).fetchall()

    def delete_by_ids(self, connection, chunk_ids: Sequence[str]) -> None:
        ids = list(chunk_ids)
        if ids:
            connection.execute("DELETE FROM chunks WHERE id=ANY(%s)", (ids,))

    def insert_rows(
        self,
        connection,
        notebook_id: str,
        source_id: str,
        rows: Sequence[ChunkWrite],
        *,
        created_at: TimestampInput,
    ) -> None:
        created_at = normalize_timestamp(created_at)
        values = [
            (
                row.id,
                notebook_id,
                source_id,
                row.text,
                row.section_path,
                jsonb(list(row.element_ids)),
                created_at,
            )
            for row in rows
        ]
        if not values:
            return
        # The transaction belongs to the caller; the probe runs on that same
        # connection, in the same transaction as the INSERT below.
        self._refuse_memory_source(connection, source_id)
        execute_many(
            connection,
            "INSERT INTO chunks"
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            values,
        )
        self._insert_fts_rows(
            connection, [(row[0], row[1], row[3]) for row in values]
        )
        # The projector's precise ``delete_by_ids`` already dropped the prior
        # rows for these chunks via the chunks cascade; add the new ones in the
        # same transaction so the reverse index never lags its chunk rows.
        self._insert_chunk_element_rows(connection, notebook_id, rows)

    def source_chunks(self, source_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,text FROM chunks WHERE source_id=%s ORDER BY ordinal",
                (source_id,),
            ).fetchall()
        return [{"id": row["id"], "text": row["text"]} for row in rows]

    @staticmethod
    def language_probe_rows(connection, notebook_id: str):
        return connection.execute(
            "(SELECT text FROM chunks WHERE notebook_id=%s ORDER BY ordinal LIMIT 30) "
            "UNION "
            "(SELECT text FROM chunks WHERE notebook_id=%s ORDER BY ordinal DESC LIMIT 30)",
            (notebook_id, notebook_id),
        ).fetchall()

    @staticmethod
    def retrieval_rows(connection, notebook_id: str):
        rows = connection.execute(
            "SELECT c.id,c.source_id,c.text,c.section_path,c.element_ids,"
            "s.title AS source_title FROM chunks c JOIN sources s ON s.id=c.source_id "
            "WHERE c.notebook_id=%s ORDER BY c.ordinal",
            (notebook_id,),
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def count_row(connection, notebook_id: str):
        return connection.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()

    @staticmethod
    def hydrate_rows(connection, chunk_ids: Sequence[str]):
        ids = list(chunk_ids)
        if not ids:
            return []
        rows = connection.execute(
            "SELECT c.id,c.source_id,c.text,c.section_path,c.element_ids,"
            "s.title AS source_title FROM chunks c JOIN sources s ON s.id=c.source_id "
            f"WHERE c.id IN ({placeholders(ids)}) ORDER BY c.ordinal",
            ids,
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def graph_hydrate_rows(
        connection,
        chunk_ids: Sequence[str],
        *,
        allowed_source_ids: Mapping[str, Iterable[str] | None] | None = None,
    ):
        """Hydrate one window of PPR-ranked chunk ids (across every library
        the PPR graph spans).

        ``allowed_source_ids`` holds the per-library source ceilings of the
        run: ``{notebook_id: frozen source ids}``.  A chunk of a listed library
        comes back only when its source is in that library's list (an empty
        list denies the library, a ``None`` value lists nothing); a chunk of an
        unlisted library, and every chunk when nothing is listed, comes back
        as read -- then the statement is byte for byte the one issued before
        the keyword existed.  The ceilings only filter: the <= ``_IN_CHUNK``
        candidate primary keys drive the read (pinned by
        ``tests/postgres/test_ppr_hydration_ceiling_pins.py``), and every list
        is bound through ``id_binding`` (one parameter, custom plan).
        """
        ids = list(chunk_ids)
        if not ids:
            return []
        sql = (
            "SELECT c.id,c.source_id,c.text,c.section_path,c.element_ids,"
            "c.notebook_id AS chunk_notebook_id,s.title AS source_title "
            "FROM chunks c JOIN sources s ON s.id=c.source_id "
            f"WHERE c.id IN ({placeholders(ids)})"
        )
        clause, params = _library_source_ceiling_clause(allowed_source_ids or {})
        if clause is None:
            rows = connection.execute(f"{sql} ORDER BY c.ordinal", ids).fetchall()
        else:
            rows = execute_ids(
                connection, f"{sql} AND {clause} ORDER BY c.ordinal",
                [*ids, *params],
            ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def retrieval_contribution_rows(
        connection,
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
        source_clause = ""
        params: list[object] = [notebook_id, *ids]
        bound = None
        if source_mode in {"include", "exclude"} and sources:
            bound = bind_ids(sources)
            # ``member_of`` / ``not_member_of``: <= 64 candidate keys drive and
            # the list filters them.  The semi-join form planned faster on
            # skewed statistics (49k: 218 -> 28 ms) but ran slower on uniform
            # ones (7.5 -> 11.5 ms), so it is not used (see ``id_binding``).
            source_clause = " AND " + (
                member_of("c.source_id", bound)
                if source_mode == "include"
                else not_member_of("c.source_id", bound)
            )
            params.append(bound.param)
        memory_clause = (
            " AND (s.source_type <> 'memory' OR EXISTS ("
            "SELECT 1 FROM memory_items m "
            "WHERE m.id=s.memory_id AND m.created_by=%s))"
        )
        params.append(actor_id)
        rows = execute_bound(
            connection,
            "SELECT c.id,c.source_id,c.text,c.section_path,c.element_ids,"
            "c.notebook_id AS chunk_notebook_id,s.title AS source_title "
            "FROM chunks c JOIN sources s "
            "ON s.id=c.source_id AND s.notebook_id=c.notebook_id "
            f"WHERE c.notebook_id=%s AND c.id IN ({placeholders(ids)})"
            f"{source_clause}{memory_clause} ORDER BY c.ordinal",
            params,
            bound,
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def id_element_rows(connection, notebook_id: str, page_rows: int | None = None):
        """Whole-notebook ``(id, element_ids)`` rows, streamed in ``ordinal``
        keyset pages instead of one whole-table ``fetchall`` (batch-3 W4
        T-W4-3.1). The consumer (``_elem_chunk_map``) folds every row's
        ``element_ids`` JSON into an ``{element_id: [chunk_id]}`` map and drops
        the row, so paging genuinely bounds the resident JSON rather than only
        the driver buffer.

        Key: ``ordinal``, NOT ``id``. ``uq_chunks_ordinal`` makes it globally
        unique, so it is a valid single-column cursor — and it is the key this
        read ALREADY ordered by, which here is load-bearing rather than
        cosmetic: ``_elem_chunk_map`` builds each element's chunk list in row
        order, and ``_elem_chunks_scoped`` pins that the legacy whole-scan and
        the ``chunk_elements`` reverse-index path both return "per-element
        chunk lists in chunk insertion order". Paging by ``id`` instead would
        have reordered those lists and moved the first-seen chunk a KG-source
        lookup returns on unbackfilled notebooks — a retrieval-result change,
        which this project is explicitly not allowed to make. Planner ledger
        (both regimes, measured) is the one written out in
        ``IndexProjectionStore.active_object_graph_rows``; this leg is the
        cleanest of them — at a page well below the notebook size it plans as
        a bare ``Index Scan using uq_chunks_ordinal`` range continuation with
        no Sort node at all, because the ORDER BY is exactly the index order.

        A GENERATOR: consume it inside the caller's connection scope, exactly
        once, by iteration — no ``len()``, no indexing, no second pass.

        ``page_rows=None`` resolves ``GRAPH_FETCH_BATCH`` at CALL time (not as
        a default-argument snapshot), so the paging oracle can shrink it —
        same pattern as ``KnowledgeStore.notebook_object_evidence_rows_paged``.
        The production caller (``GraphRetrievalService._elem_chunk_map``)
        passes ``settings.graph_fetch_page_rows`` explicitly.
        """
        for page in keyset_pages(
            connection, GRAPH_FETCH_BATCH if page_rows is None else page_rows,
            lambda cursor: (
                "SELECT id,element_ids,ordinal FROM chunks WHERE notebook_id=%s"
                + ("" if cursor is None else " AND ordinal>%s")
                + " ORDER BY ordinal",
                (notebook_id,) if cursor is None else (notebook_id, cursor),
            ),
            lambda row: row["ordinal"],
        ):
            for row in page:
                yield _compat_element_ids(row)

    @staticmethod
    def knowhow_chunk_rows(connection, notebook_id: str):
        rows = connection.execute(
            "SELECT c.id,c.element_ids FROM knowhow_tables kt "
            "JOIN chunks c ON c.source_id=kt.hidden_source_id "
            "WHERE kt.notebook_id=%s AND c.notebook_id=%s ORDER BY c.ordinal",
            (notebook_id, notebook_id),
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def knowhow_bridge_version_row(connection, notebook_id: str):
        row = dict(connection.execute(
            "SELECT COUNT(*) AS c,MAX(ce.created_at) AS ts "
            "FROM knowhow_tables kt "
            "JOIN chunks c ON c.source_id=kt.hidden_source_id "
            "JOIN chunk_embeddings ce ON ce.chunk_id=c.id "
            "WHERE kt.notebook_id=%s AND c.notebook_id=%s AND ce.notebook_id=%s",
            (notebook_id, notebook_id, notebook_id),
        ).fetchone())
        row["ts"] = iso_timestamp(row["ts"])
        return row

    @staticmethod
    def rows_by_ids(connection, chunk_ids: Sequence[str]):
        ids = list(chunk_ids)
        if not ids:
            return []
        rows = connection.execute(
            "SELECT id,source_id,text,section_path,element_ids FROM chunks "
            f"WHERE id IN ({placeholders(ids)}) ORDER BY ordinal",
            ids,
        ).fetchall()
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def id_rows(connection, notebook_id: str):
        return connection.execute(
            "SELECT id FROM chunks WHERE notebook_id=%s ORDER BY ordinal",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def chunks_by_section(
        connection,
        notebook_id: str,
        source_id: str,
        section_path: str,
        limit: int,
    ):
        """One section's chunks (that node plus its descendants), document
        order, hard-bounded — semantically equal to the SQLite adapter's
        `chunks_by_section`. SQL lives in `postgres/search.py` so the LIKE
        predicate stays next to the expression indexes it shares a table with.
        """
        rows = chunk_section_rows(
            connection, notebook_id, source_id, section_path, limit
        )
        return [_compat_element_ids(row) for row in rows]

    @staticmethod
    def backfill_fts(connection, notebook_id: str) -> int:
        # Search indexes derive directly from chunks; report how many base rows
        # are already covered so the neutral maintenance API remains useful.
        return int(
            connection.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id=%s", (notebook_id,)
            ).fetchone()["c"]
        )


_SOURCE_WITHOUT_STATISTICS = "(c.source_id||'')"


def _library_source_ceiling_clause(
    ceilings: Mapping[str, Iterable[str] | None],
) -> tuple[str | None, list]:
    """``graph_hydrate_rows``' per-library ceiling predicate on ``c`` and its
    parameters (``(None, [])`` when no library is listed).  Same text on the
    SQLite twin, dialect aside.

    A chunk passes when its library is not listed, or when its source is in
    its OWN library's list: each list is paired with its notebook id, so a
    ceiling can never admit another library's source.  An empty (or blank-only)
    list pairs with nothing, which denies that library; a ``None`` value is not
    a ceiling and leaves the library unlisted.  Libraries are ordered by id so
    equal arguments give one statement text; the lists go through
    ``source_ceiling.ceiling_param`` (the run's ``CeilingSet`` memoises its
    bound form).

    The source test is written on ``(c.source_id||'')``, not the column: the
    candidate primary keys drive this statement whatever the ceiling's
    estimate is, and on the bare column a custom plan estimates ``= ANY`` of
    the folded constant element by element against ``chunks.source_id``'s
    most-common values.  Measured on PostgreSQL 16, 49k-id ceiling, 20
    candidate keys, skewed chunks per source (host load ~22): 100 ms per
    execution on the column (146 ms planning, 3 ms execution), 7 ms on the
    expression, which carries no statistics -- the same move as SQLite's
    unary ``+``.  Equality is unchanged (``COLLATE "C"`` ids)."""
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
        arms.append(
            f"(c.notebook_id=%s AND {member_of(_SOURCE_WITHOUT_STATISTICS, bound)})"
        )
        params.extend((notebook_id, bound.param))
    return f"({' OR '.join(arms)})", params
