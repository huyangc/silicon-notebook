"""Online-safe PostgreSQL builder for the accumulated hot-path fix indexes.

Batch 1 contributed six query-family groups (eight plain btree/partial
indexes) -- see
``backend/app/repositories/postgres/migrations/0039_hotpath_batch1_indexes.sql``
for the full "which query family does this serve" evidence per group. Batch 2
(R6) added a second migration,
``backend/app/repositories/postgres/migrations/0042_hotpath_batch2_search_indexes.sql``,
contributing one notebook-scoped composite partial GIN index (this module's
first non-btree entry, hence the ``using``/``ddl_columns`` fields below) and
one partial btree index. Batch 3 added a third migration,
``backend/app/repositories/postgres/migrations/0043_concept_cluster_keyset_index.sql``,
contributing one plain (non-partial) composite btree keyset-covering index.
Batch 4 added a fourth migration,
``backend/app/repositories/postgres/migrations/0048_source_search_trgm_indexes.sql``,
contributing three notebook-scoped composite GIN trigram indexes (one of them
partial) that serve the three legs of the source tab's rewritten search
predicate. Batch 5 (delete jobization, batch 3 · W1 · PR-3) added a fifth
migration,
``backend/app/repositories/postgres/migrations/0049_notebook_delete_jobs.sql``,
contributing three FK/keyset-covering btree indexes alongside the delete-job
carrier tables.
Every migration and this module's ``HOTPATH_INDEX_SPECS`` are independent
hand-authored copies of the same index shapes on purpose (a migration file
cannot import Python at apply time): ``backend/tests/test_hotpath_indexes.py``
cross-checks migration 0039 against its eight batch-1 specs,
``backend/tests/test_hotpath_indexes_batch2.py`` cross-checks migration 0042
against its two batch-2 specs, ``backend/tests/test_hotpath_indexes_batch3.py``
cross-checks migration 0043 against its one batch-3 spec, and
``backend/tests/test_hotpath_indexes_batch4.py`` cross-checks migration 0048
against its three batch-4 specs, so no pairing can silently drift. One shared
builder/inspector serves every batch: there is a single advisory lock name and
a single ``HOTPATH_INDEX_SPECS`` tuple that grows with each batch, not a
lock/tuple per batch.

This is the sibling of ``retrieval_indexes.py`` (GIN trigram indexes for
large, notebook-scoped tables) but far simpler: every spec here is either a
plain btree/partial index, or (since batch 2) a notebook-scoped composite
GIN in the same ``(notebook_id public.text_ops, expr gin_trgm_ops)`` shape
as ``idx_knowledge_objects_nb_name_trgm`` -- batch 4 widens that shape to a
second trigram key on one entry (``(notebook_id, lower(title),
lower(file_name))``, so an OR of two LIKE arms can BitmapOr two scans of the
SAME index) but changes nothing structural. There is still no legacy-index
retirement dance, so ``HotpathIndexSpec`` stays a flat dataclass instead of
``retrieval_indexes.py``'s richer ``IndexShape``/collation-oid machinery.

Inspecting is always safe (read-only ``pg_index``/``pg_class`` catalog
queries, no advisory lock). Building is online-safe: every ``CREATE INDEX``
runs with ``CONCURRENTLY`` outside any transaction (the connection is
``autocommit=True``), one statement per index, so a single slow build never
blocks the others and never holds a transaction open against a live table
for its whole duration.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Callable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.repositories.text_whitespace import PY_WHITESPACE  # 后端中性,H5 谓词字面量的唯一真源


HOTPATH_INDEX_LOCK_NAME = "silicon-notebook:postgres-hotpath-indexes:v1"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class HotpathIndexError(RuntimeError):
    """Credential-free, operator-actionable hot-path-index failure."""


@dataclass(frozen=True)
class HotpathIndexSpec:
    name: str
    table: str
    # The catalog's own per-key shape used for ``_matches_shape`` comparison --
    # i.e. exactly what ``pg_get_indexdef(indexrelid, n, true)`` reports back
    # for each key column, WITHOUT any ``COLLATE``/opclass qualifier (a live
    # PostgreSQL 16 instance never echoes those back in the per-column form
    # even when the DDL declared them -- empirically verified; see this
    # module's tests). For a plain btree spec this doubles as the DDL text
    # too (``ddl_columns`` is left "" and ``column_list_sql`` falls back to
    # joining these).
    columns: tuple[str, ...]
    predicate: str  # "" for a full (non-partial) index; verbatim DDL text
    # PostgreSQL's own canonical ``pg_get_expr()`` rendering of ``predicate``,
    # "" when predicate == "". PostgreSQL rewrites some predicate syntax on
    # store (e.g. ``IN ('a','b')`` becomes ``= ANY (ARRAY['a'::text,'b'::text])``),
    # so the text a catalog read reports back can differ byte-for-byte from
    # the DDL text that created it even when the index is exactly right; this
    # field lets ``_matches_shape`` compare against what the catalog will
    # actually say instead of a shape it can never see. Empirically verified
    # against a live PostgreSQL 16 instance -- see this module's tests.
    predicate_shape: str
    serves: str  # short human description of the query family this serves
    # "" (default) => plain btree, no ``USING`` clause (byte-identical to
    # batch 1's original DDL template). Any other value (e.g. "gin") adds
    # ``USING <value>`` before the column list.
    using: str = ""
    # Override for ``column_list_sql()``'s DDL text when it must carry
    # qualifiers (``COLLATE``, an opclass) that the bare ``columns`` shape
    # above deliberately omits so it can still be compared against what the
    # catalog echoes back. "" (default) means "DDL text == columns joined by
    # ', '" (batch 1's plain btree case, unchanged).
    ddl_columns: str = ""
    # 目录形态的另外两维(codex #636 R1 P2 + 质量评审 P1):按 key 列序的
    # 期望 opclass("namespace:name")与期望 collation("namespace:name",
    # 非可排序类型/默认为 "")。空元组 = 本条不校验该维——批 1 的普通 btree
    # 条目维持既有校验面(keys+predicate+am),批 2 两条(评审实证过
    # 「少写 COLLATE \"C\" 的手建 GIN 三层全报就绪而 planner 拒用」)必须声明。
    opclasses: tuple[str, ...] = ()
    collations: tuple[str, ...] = ()

    def column_list_sql(self) -> str:
        return self.ddl_columns or ", ".join(self.columns)

    def ddl(self, schema: str, *, concurrently: bool) -> sql.Composed:
        concurrently_kw = sql.SQL("CONCURRENTLY ") if concurrently else sql.SQL("")
        using_kw = sql.SQL(f" USING {self.using} ") if self.using else sql.SQL("")
        stmt = sql.SQL(
            "CREATE INDEX {concurrently}IF NOT EXISTS {index} ON {schema}.{table}{using}({columns})"
        ).format(
            concurrently=concurrently_kw,
            index=sql.Identifier(self.name),
            schema=sql.Identifier(schema),
            table=sql.Identifier(self.table),
            using=using_kw,
            columns=sql.SQL(self.column_list_sql()),
        )
        if self.predicate:
            stmt = sql.SQL("{stmt} WHERE {predicate}").format(
                stmt=stmt, predicate=sql.SQL(self.predicate)
            )
        return stmt


# The six groups from the hot-path fix batch 1 production audit. Column and
# predicate text here must stay semantically identical to
# migrations/0039_hotpath_batch1_indexes.sql -- see this module's docstring
# and backend/tests/test_hotpath_indexes.py.
HOTPATH_INDEX_SPECS: tuple[HotpathIndexSpec, ...] = (
    HotpathIndexSpec(
        name="idx_clusters_nb_canonical",
        table="concept_clusters",
        columns=("notebook_id", "canonical_id"),
        predicate="",
        predicate_shape="",
        serves="concept-detail / co-mention peer-name / relation-endpoint-name lookups",
    ),
    HotpathIndexSpec(
        name="idx_clusters_nb_canonical_name_lower",
        table="concept_clusters",
        columns=("notebook_id", "lower(canonical_name)"),
        predicate="",
        predicate_shape="",
        serves="unified_kg_store.py:resolve_focal",
    ),
    HotpathIndexSpec(
        name="idx_extraction_runs_notebook",
        table="extraction_runs",
        columns=("notebook_id",),
        predicate="",
        predicate_shape="",
        serves="reverse-FK cover for notebook deletion cascade",
    ),
    HotpathIndexSpec(
        name="idx_knowledge_source_fact_elements_notebook",
        table="knowledge_source_fact_elements",
        columns=("notebook_id",),
        predicate="",
        predicate_shape="",
        serves="reverse-FK cover for notebook deletion cascade",
    ),
    HotpathIndexSpec(
        name="idx_memory_items_notebook",
        table="memory_items",
        columns=("notebook_id",),
        predicate="",
        predicate_shape="",
        serves="reverse-FK cover for notebook deletion cascade",
    ),
    HotpathIndexSpec(
        name="idx_knowledge_relations_nb_source_target_edge",
        table="knowledge_relations",
        columns=("notebook_id", "source_object_id", "target_object_id", "edge_type"),
        predicate="",
        predicate_shape="",
        serves="knowledge_store.py:in_network_relation_rows",
    ),
    HotpathIndexSpec(
        name="idx_chunks_source_ordinal",
        table="chunks",
        columns=("source_id", "ordinal"),
        predicate="",
        predicate_shape="",
        serves="search.py:chunk_section_rows (chunks_by_section)",
    ),
    HotpathIndexSpec(
        name="idx_sources_nb_hidden_type",
        table="sources",
        columns=("notebook_id", "source_type"),
        predicate="source_type IN ('memory', 'knowhow')",
        # PostgreSQL canonicalizes an ``IN (...)`` list predicate to an
        # ``= ANY (ARRAY[...])`` form on store; each element also gains an
        # explicit ``::text`` cast since ``source_type`` is ``text``.
        predicate_shape=(
            "source_type = ANY (ARRAY['memory'::text, 'knowhow'::text])"
        ),
        serves="source_store.py:hidden_source_ids",
    ),
    # -- Batch 2 (R6): search + checkup H5 index pair -- see
    # migrations/0042_hotpath_batch2_search_indexes.sql for the full
    # production-diag evidence (5.9s -> 3.6ms / 2.6s cold-scan numbers) and
    # backend/tests/test_hotpath_indexes_batch2.py for this module's own
    # migration<->spec and PY_WHITESPACE<->literal reconciliation tests.
    HotpathIndexSpec(
        name="idx_knowledge_objects_nb_payload_trgm",
        table="knowledge_objects",
        using="gin",
        # Bare per-key shape a live catalog read reports back -- COLLATE and
        # the opclass are never echoed in the per-column
        # ``pg_get_indexdef(indexrelid, n, true)`` form even though the DDL
        # below declares them (empirically verified against a live
        # PostgreSQL 16 instance; see idx_source_elements_nonblank's own
        # note on the same asymmetry for a partial predicate).
        # ``notebook_id`` leads (codex #636 R1 P1): a bare single-expression
        # trigram GIN repeats the cross-notebook global-bitmap failure
        # docs/operations.md already documents for the legacy lexical
        # indexes; this is the same composite shape retrieval_indexes.py
        # ships for idx_knowledge_objects_nb_name_trgm, so the mandatory
        # ``notebook_id=%s`` equality intersects inside index access.
        columns=("notebook_id", "(payload::text)"),
        # The real DDL text: the expression key must stay byte-identical to
        # search.py's own ``(payload::text) COLLATE "C"`` ILIKE-arm
        # expression (search.py:846) so the GIN index actually covers that
        # query's operator, and to migrations/0006_search_gin.sql's
        # idx_knowledge_objects_name_trgm sibling's double-parenthesized
        # ``((expr) COLLATE "C") opclass`` form. ``public.text_ops`` is
        # btree_gin's gin-AM text opclass (install_hotpath_indexes ensures
        # the extension; migration 0042 installs it on the fresh path).
        ddl_columns=(
            'notebook_id public.text_ops, '
            '((payload::text) COLLATE "C") public.gin_trgm_ops'
        ),
        # ``status!='deprecated'`` appears as a LITERAL in
        # notebook_knowledge_rows's SQL text, so the partial predicate is
        # implied even under a generic (parameter-value-blind) plan -- the
        # same literal-vs-bound-parameter mechanics documented at length on
        # idx_source_elements_nonblank below.
        predicate="status != 'deprecated'",
        predicate_shape="status <> 'deprecated'::text",
        # 目录侧的第三/第四维期望(质量评审 P1 的实证场景:少写 COLLATE "C" 的
        # 手建 GIN,keys/predicate/am/opclass 全对但 planner 因 exprCollation 不匹配
        # 拒用——collation 必须进比对面)。conftest 保证 pg_trgm/btree_gin 装在
        # public;notebook_id 建表即 COLLATE "C"(0001_initial.sql)。
        opclasses=("public:text_ops", "public:gin_trgm_ops"),
        collations=("pg_catalog:C", "pg_catalog:C"),
        serves="search.py:notebook_knowledge_rows (payload ILIKE probe)",
    ),
    HotpathIndexSpec(
        name="idx_source_elements_nonblank",
        table="source_elements",
        columns=("source_id", "id"),
        # 两列建表即 COLLATE "C"(0001_initial.sql:609-620),索引继承——声明出来
        # 让同名错 collation 的手建索引同样被拒。opclass 为 pg_catalog 默认 text_ops。
        opclasses=("pg_catalog:text_ops", "pg_catalog:text_ops"),
        collations=("pg_catalog:C", "pg_catalog:C"),
        # ``PY_WHITESPACE``-keyed non-blank predicate, rendered as a flat
        # ``chr(N) || chr(N) || ...`` concatenation (same style as
        # ``knowhow_store.py``'s ``_PG_TRIM_CHARS`` and
        # migrations/0005_memory_knowhow_governance_indexes.sql's
        # normalized-anchor index) so the DDL text is *derived from*
        # ``PY_WHITESPACE`` rather than hand-transcribed -- eliminating the
        # transcription-drift risk the task called out as this batch's
        # easiest way to get it wrong. Must stay semantically identical to
        # postgres/maintenance.py's ``_NONBLANK_TEXT_SQL`` (see that
        # module) and to migrations/0042_hotpath_batch2_search_indexes.sql's
        # copy of this same expression.
        predicate=(
            "btrim(text, "
            + " || ".join(f"chr({ord(character)})" for character in PY_WHITESPACE)
            + ") != ''"
        ),
        # PostgreSQL's ``pg_get_expr()`` re-serializes the flat ``||`` chain
        # above into a fully left-associated, fully parenthesized operator
        # tree AND rewrites ``!=`` to ``<>`` -- both empirically captured
        # against a live PostgreSQL 16 instance (see this module's
        # docstring and backend/tests/test_hotpath_indexes_batch2.py, which
        # re-derives this exact string from ``PY_WHITESPACE`` and asserts it
        # against a live catalog read so a future deparser change fails
        # loudly here instead of silently under-matching in production).
        predicate_shape=(
            "btrim(text, ((((((((((((((((((((((((((("
            "chr(9) || chr(10)) || chr(11)) || chr(12)) || chr(13)) || chr(28)) "
            "|| chr(29)) || chr(30)) || chr(31)) || chr(32)) || chr(133)) || chr(160)) "
            "|| chr(5760)) || chr(8192)) || chr(8193)) || chr(8194)) || chr(8195)) "
            "|| chr(8196)) || chr(8197)) || chr(8198)) || chr(8199)) || chr(8200)) "
            "|| chr(8201)) || chr(8202)) || chr(8232)) || chr(8233)) || chr(8239)) "
            "|| chr(8287)) || chr(12288)) <> ''::text"
        ),
        serves="postgres/maintenance.py: H5 non-blank element eligibility (count_missing_element_vectors et al.)",
    ),
    # -- Batch 3: concept-cluster keyset covering index -- see
    # migrations/0043_concept_cluster_keyset_index.sql for the full
    # production evidence and backend/tests/test_hotpath_indexes_batch3.py
    # for this module's own migration<->spec reconciliation test.
    HotpathIndexSpec(
        name="idx_clusters_nb_canonical_member",
        table="concept_clusters",
        columns=("notebook_id", "canonical_id", "member_object_id"),
        predicate="",
        predicate_shape="",
        # 三列建表即 COLLATE "C"(0001_initial.sql:144-155),普通 btree 继承列
        # collation,与 knowledge_store.py:concept_cluster_detail_rows 自己的
        # `COLLATE "C"` 比较/排序键逐字匹配——声明出来让同名错 collation 的
        # 手建索引同样被拒。opclass 为 pg_catalog 默认 text_ops。
        opclasses=("pg_catalog:text_ops", "pg_catalog:text_ops", "pg_catalog:text_ops"),
        collations=("pg_catalog:C", "pg_catalog:C", "pg_catalog:C"),
        serves=(
            "knowledge_store.py:concept_cluster_detail_rows / "
            "concept_cluster_member_total (concept-detail hub-cluster keyset pagination)"
        ),
    ),
    # -- Batch 4: the source-tab search predicate -- see
    # migrations/0048_source_search_trgm_indexes.sql for the full production
    # evidence (49k-source notebook, 363ms COUNT, source_authors 210k-row
    # parallel seq scan, source_paper_meta 39k-row seq scan, and the
    # two-char-vs-seven-char equal-cost diagnostic that proves the scans, not
    # the LIKE, are the bottleneck) and
    # backend/tests/test_hotpath_indexes_batch4.py for this module's own
    # migration<->spec reconciliation test. All three serve one leg each of
    # postgres/source_store.py:list_sources_page's rewritten three-leg UNION.
    HotpathIndexSpec(
        name="idx_sources_nb_title_file_trgm",
        table="sources",
        using="gin",
        # Bare per-key shape a live catalog read reports back: a PostgreSQL 16
        # ``pg_get_indexdef(indexrelid, n, true)`` echoes ``lower(title)`` with
        # neither the opclass nor a COLLATE qualifier, the same asymmetry
        # batch 2's two entries document.
        columns=("notebook_id", "lower(title)", "lower(file_name)"),
        # THREE keys in ONE index, deliberately: the query's two LIKE arms are
        # OR'd, and a multi-column GIN lets each arm constrain the subset
        # ``(notebook_id, its own trigram column)`` and leave the third key
        # free, so the planner answers the OR with a BitmapOr over two scans of
        # THIS index -- by default, no planner knobs. Verified by live EXPLAIN,
        # not assumed: see the migration's header and
        # test_hotpath_indexes_batch4_live.py, which pins that plan shape. Two
        # alternatives were measured and rejected: two separate two-key indexes
        # (each would still carry the same ``notebook_id`` key at the same
        # per-scan cost) and splitting the query's OR into two single-arm UNION
        # legs (a wash on selective needles, measurably worse on short ones).
        #
        # If an EXPLAIN ever shows this index NOT being used, check the GIN
        # fastupdate pending list before concluding anything -- right after a
        # bulk load it inflates every GIN cost estimate ~10x and the planner
        # rejects its own index until a VACUUM merges it. The migration header
        # carries the measured before/after.
        #
        # No explicit COLLATE, unlike batch 2's payload GIN: ``title`` and
        # ``file_name`` are ``text COLLATE "C"`` at table-creation time
        # (0001_initial.sql), and ``lower()`` derives its result collation from
        # its argument, so both expression keys already resolve to
        # pg_catalog:C (verified against a live catalog read). 0042's
        # ``(payload::text)`` needed the qualifier because a cast of a jsonb
        # column inherits nothing.
        ddl_columns=(
            "notebook_id public.text_ops, "
            "lower(title) public.gin_trgm_ops, "
            "lower(file_name) public.gin_trgm_ops"
        ),
        # Byte-identical to source_store.py's VISIBLE_SOURCE_TYPES_PREDICATE,
        # which list_sources_page interpolates into its SQL as a LITERAL (never
        # a bound parameter), so the partial-predicate implication holds even
        # under a generic, parameter-value-blind plan -- the mechanics
        # idx_source_elements_nonblank's comment above works through in full.
        predicate="source_type NOT IN ('memory','knowhow')",
        # PostgreSQL canonicalizes ``NOT IN (...)`` to ``<> ALL (ARRAY[...])``
        # on store, and casts each element to ``::text`` because ``source_type``
        # is ``text`` -- the mirror image of batch 1's
        # idx_sources_nb_hidden_type, whose ``IN (...)`` becomes
        # ``= ANY (ARRAY[...])``. Captured from a live PostgreSQL 16 catalog
        # read, not hand-guessed.
        predicate_shape=(
            "source_type <> ALL (ARRAY['memory'::text, 'knowhow'::text])"
        ),
        opclasses=("public:text_ops", "public:gin_trgm_ops", "public:gin_trgm_ops"),
        collations=("pg_catalog:C", "pg_catalog:C", "pg_catalog:C"),
        serves=(
            "source_store.py:list_sources_page (q search, title/file_name leg)"
        ),
    ),
    HotpathIndexSpec(
        name="idx_source_authors_nb_name_trgm",
        table="source_authors",
        using="gin",
        columns=("notebook_id", "lower(name)"),
        ddl_columns=(
            "notebook_id public.text_ops, lower(name) public.gin_trgm_ops"
        ),
        # Non-partial: source_authors carries no visibility dimension of its
        # own (the outer query's intersection with ``sources`` supplies it) and
        # every author row is a legitimate search target.
        predicate="",
        predicate_shape="",
        opclasses=("public:text_ops", "public:gin_trgm_ops"),
        collations=("pg_catalog:C", "pg_catalog:C"),
        serves="source_store.py:list_sources_page (q search, author-name leg)",
    ),
    HotpathIndexSpec(
        name="idx_source_paper_meta_nb_ptitle_trgm",
        table="source_paper_meta",
        using="gin",
        # ``paper_title`` is NULLABLE. A GIN trigram index simply stores no
        # entry for a NULL expression, and ``LOWER(NULL) LIKE %s`` is NULL --
        # falsy in a WHERE -- in both the old and the rewritten query, so the
        # missing entries cost nothing.
        columns=("notebook_id", "lower(paper_title)"),
        ddl_columns=(
            "notebook_id public.text_ops, lower(paper_title) public.gin_trgm_ops"
        ),
        predicate="",
        predicate_shape="",
        opclasses=("public:text_ops", "public:gin_trgm_ops"),
        collations=("pg_catalog:C", "pg_catalog:C"),
        serves="source_store.py:list_sources_page (q search, paper-title leg)",
    ),
    # -- Batch 5 (batch 3 · W1 · PR-3 Phase A): three FK/keyset-covering
    # indexes design doc Sec 1.4 registers as prerequisites for the delete-
    # jobization work -- see migrations/0049_notebook_delete_jobs.sql for the
    # full "which step turns from a seq scan into an index scan" evidence and
    # backend/tests/test_hotpath_indexes_batch5.py for this module's own
    # migration<->spec reconciliation test.
    HotpathIndexSpec(
        name="idx_agent_tokens_default_notebook",
        table="agent_access_tokens",
        columns=("default_notebook_id",),
        predicate="",
        predicate_shape="",
        # 列建表即 COLLATE "C"(0001_initial.sql:12-23),普通 btree 继承列
        # collation。opclass 为 pg_catalog 默认 text_ops。
        opclasses=("pg_catalog:text_ops",),
        collations=("pg_catalog:C",),
        serves="phase 5 finalize's `DELETE FROM notebooks` FK-cascade probe (was a full seq scan -- design doc Sec 1.1)",
    ),
    HotpathIndexSpec(
        name="idx_knowhow_cell_code_column",
        table="knowhow_cell_code",
        columns=("column_id",),
        predicate="",
        predicate_shape="",
        opclasses=("pg_catalog:text_ops",),
        collations=("pg_catalog:C",),
        serves="B-class knowhow chain's column_id leg (Phase B's batched page cleanup)",
    ),
    HotpathIndexSpec(
        name="idx_conversations_notebook",
        table="conversations",
        columns=("notebook_id", "id"),
        predicate="",
        predicate_shape="",
        opclasses=("pg_catalog:text_ops", "pg_catalog:text_ops"),
        collations=("pg_catalog:C", "pg_catalog:C"),
        serves="form-two (ctid) batch-delete loop's notebook_id-leading prefix on the closure-external conversations table (Phase B)",
    ),
)


def _schema(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value or ""):
        raise ValueError("PostgreSQL schema must be a simple identifier")
    return value


def _connect(database_url: str):
    if not database_url:
        raise ValueError("database URL is required")
    try:
        return psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
            application_name="silicon-notebook-hotpath-index-builder",
            connect_timeout=10,
        )
    except Exception:
        raise HotpathIndexError("postgres_connection_failed") from None


def _index_row(connection, schema: str, name: str):
    return connection.execute(
        "SELECT idx.relname AS index_name, tbl.relname AS table_name, "
        "tbl_ns.nspname AS table_schema, i.indisvalid, i.indisready, "
        "i.indisunique, i.indnkeyatts, i.indnatts, "
        "am.amname AS access_method, "
        "ARRAY(SELECT opc_ns.nspname||':'||opc.opcname "
        "FROM unnest(i.indclass::oid[]) WITH ORDINALITY op(oid,ord) "
        "JOIN pg_opclass opc ON opc.oid=op.oid "
        "JOIN pg_namespace opc_ns ON opc_ns.oid=opc.opcnamespace "
        "ORDER BY op.ord) AS opclasses, "
        "ARRAY(SELECT COALESCE(coll_ns.nspname||':'||coll.collname, '') "
        "FROM unnest(i.indcollation::oid[]) WITH ORDINALITY co(oid,ord) "
        "LEFT JOIN pg_collation coll ON coll.oid=co.oid "
        "LEFT JOIN pg_namespace coll_ns ON coll_ns.oid=coll.collnamespace "
        "ORDER BY co.ord) AS collations, "
        "ARRAY(SELECT pg_get_indexdef(i.indexrelid,n,true) "
        "FROM generate_series(1,i.indnkeyatts) AS n ORDER BY n) AS keys, "
        "pg_get_expr(i.indpred,i.indrelid,true) AS predicate "
        "FROM pg_index i "
        "JOIN pg_class idx ON idx.oid=i.indexrelid "
        "JOIN pg_am am ON am.oid=idx.relam "
        "JOIN pg_namespace ns ON ns.oid=idx.relnamespace "
        "JOIN pg_class tbl ON tbl.oid=i.indrelid "
        "JOIN pg_namespace tbl_ns ON tbl_ns.oid=tbl.relnamespace "
        "WHERE ns.nspname=%s AND idx.relname=%s",
        (schema, name),
    ).fetchone()


def _normalized_expr(value: str) -> str:
    return " ".join((value or "").lower().replace("::text", "").split())


def _matches_shape(row, spec: HotpathIndexSpec) -> bool:
    """Compare the catalog's actual key list / partial predicate against
    ``spec``, the same-shape check ``retrieval_indexes.py``'s ``_index_row``
    sibling does for GIN indexes. A same-named index on the right table that
    was hand-built with different columns or a different predicate must never
    be silently accepted as "ready" -- see this module's docstring for why
    ``predicate_shape`` (not ``predicate``) is the comparison target.
    """
    # 唯一性与总列数也是形态(codex #636 R2 P2):同名但声明 UNIQUE、或带
    # INCLUDE 附加列的索引,keys(只含 1..indnkeyatts)与谓词都可能全同,但
    # inspect 报就绪而迁移 0042 的 DO 块按这两维拒绝——两个校验器必须同结论。
    if bool(row["indisunique"]):
        return False
    if int(row["indnkeyatts"]) != len(spec.columns) or int(row["indnatts"]) != len(
        spec.columns
    ):
        return False
    keys = tuple(_normalized_expr(str(value)) for value in row["keys"] or ())
    expected_keys = tuple(_normalized_expr(value) for value in spec.columns)
    if keys != expected_keys:
        return False
    predicate = _normalized_expr(str(row["predicate"] or ""))
    expected_predicate = _normalized_expr(spec.predicate_shape)
    if predicate != expected_predicate:
        return False
    # 访问方法/opclass 也是形态的一部分(codex #636 R1 P2):同名 btree 冒充 GIN、
    # 或建了非 trgm opclass 的 GIN,keys/predicate 都可能一致,但对 ILIKE 毫无
    # 加速作用——照姊妹 retrieval_indexes.py 的目录检查,一并比对。spec.using
    # 为空 = 普通 btree(批 1 全部条目)。
    expected_am = spec.using or "btree"
    if str(row["access_method"]) != expected_am:
        return False
    # 期望声明在 spec 上而非硬编码在比较逻辑里(质量评审 P2):第三批若加一条
    # jsonb_path_ops 的 GIN,只需在它的 spec 里声明,不会让 install 的建后复检
    # 在一条刚建成功的索引上误报 index_verification_failed。
    if spec.opclasses:
        opclasses = tuple(str(v) for v in (row["opclasses"] or ()))
        if opclasses != spec.opclasses:
            return False
    if spec.collations:
        collations = tuple(str(v) for v in (row["collations"] or ()))
        if collations != spec.collations:
            return False
    return True


def _require_extensions(connection) -> None:
    """Mirror ``retrieval_indexes.py``'s ``_require_extensions``: pg_trgm must
    already sit in ``public`` (migration 0002 puts it there; a mislocated
    install is an operator decision this tool must not second-guess), and
    btree_gin -- which supplies the gin-AM ``public.text_ops`` opclass the
    batch-2 composite key needs -- is created on demand, exactly as migration
    0042 does on the fresh-deploy path. This runs in the install path only;
    inspect stays read-only.
    """
    row = connection.execute(
        "SELECT n.nspname AS schema_name FROM pg_extension e "
        "JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pg_trgm'"
    ).fetchone()
    if row is None or row["schema_name"] != "public":
        raise HotpathIndexError("public_pg_trgm_required")
    connection.execute("CREATE EXTENSION IF NOT EXISTS btree_gin WITH SCHEMA public")
    row = connection.execute(
        "SELECT n.nspname AS schema_name FROM pg_extension e "
        "JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='btree_gin'"
    ).fetchone()
    if row is None or row["schema_name"] != "public":
        raise HotpathIndexError("public_btree_gin_required")


def _state(connection, schema: str, spec: HotpathIndexSpec) -> dict[str, object]:
    row = _index_row(connection, schema, spec.name)
    if row is None:
        return {"name": spec.name, "serves": spec.serves, "state": "缺失"}
    if str(row["table_schema"]) != schema or str(row["table_name"]) != spec.table:
        raise HotpathIndexError(f"unexpected_index_owner:{spec.name}")
    if not _matches_shape(row, spec):
        return {"name": spec.name, "serves": spec.serves, "state": "UNEXPECTED"}
    if not bool(row["indisvalid"]) or not bool(row["indisready"]):
        return {"name": spec.name, "serves": spec.serves, "state": "INVALID"}
    return {"name": spec.name, "serves": spec.serves, "state": "存在"}


def inspect_hotpath_indexes(database_url: str, *, schema: str = "public") -> dict:
    """Read-only pg_index/pg_class check. Never takes the build advisory lock."""
    schema = _schema(schema)
    with _connect(database_url) as connection:
        states = [_state(connection, schema, spec) for spec in HOTPATH_INDEX_SPECS]
    return {"schema": schema, "indexes": states}


def install_hotpath_indexes(
    database_url: str,
    *,
    schema: str = "public",
    lock_timeout_seconds: int = 5,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Build every missing index with ``CREATE INDEX CONCURRENTLY``, one
    statement per index, outside any transaction.

    An ``INVALID`` index (a prior ``CONCURRENTLY`` build that failed midway,
    leaving a catalog row PostgreSQL will never finish on its own) is never
    auto-dropped here -- unlike ``retrieval_indexes.py``'s GIN builder, this
    one only reports operator-actionable guidance
    (``DROP INDEX CONCURRENTLY <name>;`` then rerun) and fails the whole run
    with exit code 1, so an operator always makes that destructive call
    explicitly rather than a script silently doing it in the background of
    an unrelated missing-index build.
    """
    schema = _schema(schema)
    if not isinstance(lock_timeout_seconds, int) or isinstance(lock_timeout_seconds, bool):
        raise ValueError("lock timeout must be an integer")
    if not 1 <= lock_timeout_seconds <= 300:
        raise ValueError("lock timeout must be in 1..300 seconds")
    emit = progress or (lambda _message: None)
    with _connect(database_url) as connection:
        try:
            locked = bool(
                connection.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked",
                    (HOTPATH_INDEX_LOCK_NAME,),
                ).fetchone()["locked"]
            )
            if not locked:
                raise HotpathIndexError("hotpath_index_build_already_running")
            connection.execute(
                "SELECT set_config('statement_timeout','0',false),"
                "set_config('lock_timeout',%s,false)",
                (f"{lock_timeout_seconds}s",),
            )
            # current_spec_name 必须先于 _require_extensions 绑定:下面的兜底
            # except 读它拼诊断,扩展安装抛非 HotpathIndexError 异常时(缺 contrib
            # 包 58P01、库上无 CREATE 权限 42501、与 retrieval 构建器并发的
            # DuplicateObject)否则是 UnboundLocalError 裸栈,可操作消息全丢
            # (质量评审 P1 实证)。
            invalid_names: list[str] = []
            current_spec_name: str | None = None
            _require_extensions(connection)
            for spec in HOTPATH_INDEX_SPECS:
                current_spec_name = spec.name
                state = _state(connection, schema, spec)
                if state["state"] == "存在":
                    emit(f"{spec.name}: already ready")
                    continue
                if state["state"] == "UNEXPECTED":
                    # A same-named index on the right table but a different
                    # column list or predicate is fail-closed, never repaired
                    # or dropped as if it were this tool's own interrupted
                    # artifact -- see _matches_shape's docstring.
                    raise HotpathIndexError(f"unexpected_index_definition:{spec.name}")
                if state["state"] == "INVALID":
                    invalid_names.append(spec.name)
                    emit(
                        f"{spec.name}: INVALID (a prior CONCURRENTLY build did "
                        f"not finish) -- run `DROP INDEX CONCURRENTLY {spec.name};` "
                        "then rerun --apply"
                    )
                    continue
                emit(f"{spec.name}: building concurrently")
                started = time.monotonic()
                connection.execute(spec.ddl(schema, concurrently=True))
                elapsed_ms = (time.monotonic() - started) * 1000
                state = _state(connection, schema, spec)
                if state["state"] != "存在":
                    raise HotpathIndexError(f"index_verification_failed:{spec.name}")
                emit(f"{spec.name}: ready ({elapsed_ms:.0f}ms)")
            if invalid_names:
                raise HotpathIndexError(
                    "invalid_indexes_need_manual_drop:" + ",".join(invalid_names)
                )
        except HotpathIndexError:
            raise
        except psycopg.errors.LockNotAvailable:
            raise HotpathIndexError("postgres_lock_timeout") from None
        except Exception as exc:
            # Credential-free diagnostics: which spec was in flight (already
            # a public, non-secret name from this module) plus the
            # PostgreSQL SQLSTATE code, never the exception's own message
            # (which can echo back SQL text or literal values).
            detail = f":{current_spec_name}" if current_spec_name else ""
            sqlstate = getattr(exc, "sqlstate", None)
            if sqlstate:
                detail += f":{sqlstate}"
            raise HotpathIndexError(f"hotpath_index_build_failed{detail}") from None
        finally:
            try:
                connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                    (HOTPATH_INDEX_LOCK_NAME,),
                )
            except Exception:
                pass
    return inspect_hotpath_indexes(database_url, schema=schema)
