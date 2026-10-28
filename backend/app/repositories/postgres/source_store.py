from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from app.models.sources import (
    PaginatedSourceElements,
    PaginatedSources,
    PaperAuthor,
    PaperMeta,
    SourceDetail,
    SourceElement,
    SourceSummary,
    extraction_warning_text,
    kg_analyzed_without_objects,
    INDEXING_CHUNK_FALLBACK_WARNING_PREFIX,
    has_indexing_chunk_fallback_warning,
    has_pdf_python_fallback_warning,
    paper_meta_status,
)
from app.domain.source_display import summary_display_title
from app.domain.source_positions import normalize_positions, positions_page
from app.models.question_suggestions import QUESTION_SUGGESTION_REVISION_PAGE_SIZE
from app.domain.promotion_source import PROMOTION_SOURCE_TYPE, presented_source_title
from app.repositories.promotion_title_sql import (
    mapped_title_sql,
    needle_names_current_wording,
    promotion_gate_sql,
)
from app.repositories.ports import (
    SOURCE_PAPER_META_UNSET,
    DocumentCapacityExceeded,
    SourceElementWrite,
)
from app.repositories.postgres._store_utils import (
    TimestampInput,
    execute_many,
    iso_timestamp,
    json_value,
    jsonb,
    local_datetime,
    normalized_clock,
    normalize_timestamp,
    placeholders,
)
from app.domain.source_find_terms import fold_term, like_pattern
from app.repositories.find_sources_rows import (
    assemble_scope,
    bind_term_legs,
    check_kind,
    scope_rows_dicts,
)
from app.repositories.paper_card_rows import paper_card_row, utc_iso
from app.repositories.postgres import memory_sql
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.id_binding import (
    BoundIds,
    bind_ids,
    execute_bound,
    execute_ids,
    member_of,
)
from app.repositories.postgres.memory_sql import (
    memory_source_readable,
    memory_source_type_predicate,
)


_UNSET = SOURCE_PAPER_META_UNSET


# 「用户可见文档」的判定谓词，与 SQLite 侧 ``source_store.
# VISIBLE_SOURCE_TYPES_PREDICATE`` 逐字同义:排除 Memory 派生源与 knowhow 表隐藏
# 投影源。此前这段谓词在本文件里被 list_sources / list_sources_page /
# visible_document_count 各写一遍;来源集合枚举(design doc §6.2)必须与来源页签
# 同口径，所以先把它收成一个常量，再由 ``source_change_signal_rows`` 把它作为
# **投影列**求值（`user_visible`）——否则「哪些来源用户看得见」在这一个文件里就有
# 四份拼写，任何一份漂移都会让答案里出现一张用户在来源页签上根本看不到的卡片。
# 求值放在投影里而不是另开一条 id 查询，是因为 `source_type` 上没有索引：那条查询
# 只能整表扫这个 notebook 的全部源行，而且就发生在刚扫过同一批行之后。SQL 文本
# 逐字不变。
VISIBLE_SOURCE_TYPES_PREDICATE = "source_type NOT IN ('memory','knowhow')"
# 晋升出处来源(PR-E8,``app.domain.promotion_provenance``)按上面的谓词**可见**:
# 进来源列表、进挂载库与全局问答的天花板,这正是 B-12 的修法。但它不是用户导入的
# 文档,任何文档管线都不处理它:KG 抽取目标(``rebuild`` 会先按证据清掉它支撑的
# 晋升对象再让模型重抽)、待分析计数、论文元数据补抽、体检 H2/H3。这些语句在既有
# 可见谓词之后再接这一句,作为独立的合取项而不是并进 ``NOT IN`` 列表:SQLite 的
# 部分索引只认 WHERE 里逐字出现的索引谓词(SQLite 侧同名常量)。
NOT_PROMOTION_SOURCE_PREDICATE = "source_type <> 'promotion'"


# 「这一行是私有 Memory 的合成来源」的 SQL 谓词,与 SQLite 侧
# ``source_store.MEMORY_SOURCE_TYPE_PREDICATE`` 逐字同义;两侧的理由与用法边界写在
# 那一份注释里(简述:底座聚合把 Memory 排除压进语句内,跨查询的相减/排除清单在
# READ COMMITTED 下会被并发的 Memory 增删漏掉,而漏掉的东西里包含概念名称)。
# 文本由 `memory_sql.memory_source_type_predicate()` 渲染(共享定义点)。
MEMORY_SOURCE_TYPE_PREDICATE = memory_source_type_predicate()

# ``SourceStore.memory_source_ids(holders_among=...)`` (E4-7); SQLite twin
# ``_MEMORY_HOLDERS_SQL``.  ``{member}`` is ``id_binding.member_of`` over the
# notebook ids, executed through ``execute_ids`` (a custom plan); the
# ``source_type IN`` term is ``idx_sources_nb_hidden_type``'s predicate.
_MEMORY_HOLDERS_SQL = (
    "SELECT DISTINCT notebook_id FROM sources WHERE {member} "
    "AND source_type IN ('memory','knowhow') "
    f"AND {MEMORY_SOURCE_TYPE_PREDICATE}"
)

# ``SourceStore.hidden_source_ids``;SQLite 侧 ``_HIDDEN_SOURCE_IDS_SQL`` 的孪生。
_HIDDEN_SOURCE_IDS_SQL = (
    "SELECT s.id FROM sources s WHERE s.notebook_id=%s "
    "AND s.source_type IN ('memory','knowhow') "
    f"AND {memory_source_readable('s')} "
    "ORDER BY s.id"
)
#: Schema-induction sample: no Memory element (``notebook_element_sample``).
_SAMPLE_NOT_MEMORY = f"NOT ({memory_sql.memory_source_type_predicate('s.source_type')})"

# The drift probe's one-row fingerprint (``all_visible_source_ids(...,
# digest_for_owner=)``): md5 over each half's ids in ``id`` order (``sources.id``
# is COLLATE "C", byte order == Python's code-point ``sorted`` for UTF-8)
# joined by U+001E, the visible half and the owner's hidden half in ONE
# statement.  The same two predicates as ``all_visible_source_ids`` /
# ``_HIDDEN_SOURCE_IDS_SQL``; an empty half is NULL.  Parameters:
# (notebook_id, notebook_id, owner_id).  SQLite twin: ``_UNIVERSE_DIGEST_SQL``.
_UNIVERSE_DIGEST_SQL = (
    "SELECT (SELECT md5(string_agg(id, E'\\x1e' ORDER BY id)) "
    "FROM sources WHERE notebook_id=%s "
    f"AND {VISIBLE_SOURCE_TYPES_PREDICATE}) AS visible_digest, "
    "(SELECT md5(string_agg(s.id, E'\\x1e' ORDER BY s.id)) "
    "FROM sources s WHERE s.notebook_id=%s "
    "AND s.source_type IN ('memory','knowhow') "
    f"AND {memory_source_readable('s')}) AS hidden_digest"
)


# 论文元数据补抽候选谓词(接在 ``FROM sources s`` 且已按 ``s.notebook_id`` 过滤之后)。
# 与 SQLite 侧 ``sqlite.source_store.PAPER_META_ELIGIBLE_SQL`` 同义;四个消费方
# (sources_missing_paper_meta / 离线补抽的分页读者
# maintenance.paper_metadata_source_ids_page / notebook_analytics 的 missing 计数 /
# NotebookSummary.paper_meta_missing 的 EXISTS 探针)共用这一份保证口径不漂移,
# 与下面的 PAPER_META_NO_META_SQL 一起用;仓储层不许再手抄一份
# (test_paper_meta_own_row.py 的源码守卫)。
PAPER_META_ELIGIBLE_SQL = (
    " AND s.source_type NOT IN ('memory','knowhow')"
    f" AND s.{NOT_PROMOTION_SOURCE_PREDICATE}"
    " AND s.doc_type IN ('','academic_paper')"
    " AND s.parse_status IN ('parsed','extracting','extracted')"
)
# 「这一行是这条 source 自己的论文元数据」:子表行按 source_id 连到 source,且子表
# 自身的 notebook_id 就是 source 所在的库——与 report_source_rows 家族、
# paper_meta_for_sources 同一口径,子表 notebook_id 与 source 不一致的畸形遗留行
# 不算。库的写者写不出这种行;读侧收窄让补抽能找到并改正它
# (upsert_paper_meta 冲突时写回 notebook_id)。
#
# notebook_id 写成 ``IS NOT DISTINCT FROM`` 而不是 ``=``(两列都 NOT NULL,语义
# 就是等值):``=`` 会进等价类,外层的 ``s.notebook_id=%s`` 被推成
# ``m.notebook_id=<常量>``,planner 于是按库取出子表行、再逐行比对 source_id
# (实测中等库的 EXISTS 探针 +80%)。写成过滤项后 source_id 仍是唯一的连接键,
# 覆盖索引 idx_source_paper_meta_source_nb (source_id) INCLUDE (notebook_id,
# is_paper)(迁移 0084)让探针与计数保持 Index Only Scan。
PAPER_META_OWN_ROW_SQL = (
    "m.source_id=s.id AND m.notebook_id IS NOT DISTINCT FROM s.notebook_id"
)
# 「尚无 meta 行」半句单独成段:sources_missing_paper_meta 的 --force
# (include_existing=True)要能把它拿掉。
PAPER_META_NO_META_SQL = (
    f" AND NOT EXISTS(SELECT 1 FROM source_paper_meta m WHERE {PAPER_META_OWN_ROW_SQL})"
)
# notebook_analytics 的论文元数据三态计数用的连接:has_meta / marker 只数本库
# source 自己的行,missing 是连不上的合规 source——与 NO_META 同一个条件。
PAPER_META_OWN_ROW_JOIN_SQL = (
    f" LEFT JOIN source_paper_meta m ON {PAPER_META_OWN_ROW_SQL}"
)


def _find_sources_term_legs(
    notebook_id: str, folded_terms: Sequence[str], scope: BoundIds | None = None,
) -> tuple[str, list[object]]:
    """``find_sources_metadata_rows``' term legs (0048's per-table UNION shape,
    see that method).  Every leg ORs one ``lower(col) LIKE %s`` per term and
    reports ``(source_id, field, v1, v2)``; ``v2`` is the file name on the
    title leg.  The promotion leg (a legacy-titled promotion source matched by
    its MAPPED title) appears only for a term naming today's wording, exactly as
    in ``list_sources_page``.  ``keywords`` is matched one array element at a
    time (``jsonb_array_elements``: a string or number element, its text under
    ``COLLATE "C"``), never on the jsonb text, so a quote or a backslash in a
    term means itself.  ``scope`` (the filtered scope's ids, only for a scope
    the filters narrowed -- ``find_sources_rows.bind_term_legs``) is a
    ``member_of`` filter on every leg; the leg stays driven by its own
    ``notebook_id=`` and trigram predicate."""
    patterns = [like_pattern(term) for term in folded_terms]

    def any_like(expression: str) -> tuple[str, list[object]]:
        return (
            "(" + " OR ".join(f"{expression} LIKE %s" for _ in patterns) + ")",
            list(patterns),
        )

    legs: list[str] = []
    params: list[object] = []

    def leg(head: str, expressions: Sequence[str], where: str, id_column: str) -> None:
        clauses: list[str] = []
        for expression in expressions:
            clause, values = any_like(expression)
            clauses.append(clause)
            params.extend(values)
        member = ""
        if scope is not None:
            member = f" AND {member_of(id_column, scope)}"
            params.append(scope.param)
        legs.append(
            f"{head} WHERE {where}AND (" + " OR ".join(clauses) + f"){member}"
        )

    visible = f"notebook_id=%s AND {VISIBLE_SOURCE_TYPES_PREDICATE} "
    params.append(notebook_id)
    leg(
        "SELECT id AS source_id,'title' AS field,title AS v1,file_name AS v2 "
        "FROM sources",
        ("lower(title)", "lower(file_name)"), visible, "id",
    )
    if any(needle_names_current_wording(term) for term in folded_terms):
        params.append(notebook_id)
        leg(
            f"SELECT id,'title',{mapped_title_sql('title')},'' FROM sources",
            (f"lower({mapped_title_sql('title')})",),
            f"notebook_id=%s AND {promotion_gate_sql('source_type', 'id')} ", "id",
        )
    params.append(notebook_id)
    leg(
        "SELECT id,'summary',summary,'' FROM sources", ("lower(summary)",),
        visible, "id",
    )
    params.append(notebook_id)
    leg(
        "SELECT a.source_id,'author',a.name,'' FROM source_authors a",
        ("lower(a.name)",), "a.notebook_id=%s ", "a.source_id",
    )
    params.append(notebook_id)
    leg(
        "SELECT m.source_id,'paper_title',m.paper_title,'' FROM source_paper_meta m",
        ("lower(m.paper_title)",), "m.notebook_id=%s ", "m.source_id",
    )
    params.append(notebook_id)
    leg(
        "SELECT m.source_id,'venue',m.venue,'' FROM source_paper_meta m",
        ("lower(m.venue)",), "m.notebook_id=%s ", "m.source_id",
    )
    params.append(notebook_id)
    leg(
        "SELECT m.source_id,'keywords',k.value #>> '{}','' FROM source_paper_meta m "
        "CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN "
        "jsonb_typeof(m.keywords)='array' THEN m.keywords ELSE '[]'::jsonb END) "
        "AS k(value)",
        ("lower((k.value #>> '{}') COLLATE \"C\")",),
        "m.notebook_id=%s AND jsonb_typeof(k.value) IN ('string','number') ",
        "m.source_id",
    )
    return " UNION ALL ".join(legs), params


def _sort_key(value: object) -> str:
    """A timestamp column as ORDER-BY-compatible text.

    Driver datetimes become ISO text; text columns pass through; NULL becomes
    ``""``.

    **Aware datetimes are converted to UTC first**, and that is the load-bearing
    line.  ``timestamptz`` values arrive with a UTC offset, and offsets are not
    constant within one column: across a DST transition two rows an hour apart
    can read ``…01:30:00+02:00`` and ``…01:30:00+01:00``.  Comparing those
    strings lexicographically ranks them by the wall-clock digits and then by
    the offset text, which is not chronological — ``+01:00`` sorts before
    ``+02:00`` even though it is the LATER instant.  PostgreSQL's ``ORDER BY``
    compares instants, so the roster would come out in an order the source tab
    never shows, silently, and only around a DST fold.  Normalizing to UTC makes
    every key share one offset, which is the condition under which lexicographic
    ISO comparison equals instant comparison.  (Naive datetimes carry no offset
    and are left alone: they are already single-convention, and inventing a
    timezone for them would be a guess.)

    The one asymmetry worth naming: SQL sorts NULLs last by default on
    ascending order, while ``""`` sorts first here.  ``sources.created_at`` is
    written on every insert path and has never been NULL, so this branch is
    defensive only; putting an impossible row first is preferable to raising
    mid-enumeration, and the roster's coverage would disclose nothing wrong
    either way.
    """
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if not callable(isoformat):
        return str(value)
    if getattr(value, "tzinfo", None) is not None:
        try:
            return value.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            # A datetime whose offset cannot be applied is not worth failing an
            # enumeration over; the unconverted form is still a stable key.
            return isoformat()
    return isoformat()


def _created_label(value: object) -> str:
    try:
        parsed = local_datetime(value)
    except (TypeError, ValueError):
        parsed = datetime.now().astimezone()
    return f"{parsed.year}年{parsed.month}月{parsed.day}日"


def _metadata_compat(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value or {}, ensure_ascii=False)


class SourceStore:
    """PostgreSQL source rows, elements, paper metadata, and summary hydration."""

    IN_CHUNK = 5_000

    # How many mounted libraries' visible sets a default retrieval ceiling
    # reads at once from this store (``CeilingReaders.read_workers``): each
    # read holds its own pooled connection, and four in parallel took six
    # 49k-source libraries from +17.5 ms to -62..-111 ms against the route
    # freeze.  Equal to ``source_scope.POSTGRES_MOUNTED_READ_WORKERS`` (this
    # layer may not import the service; a test pins the two together).
    ceiling_read_workers = 4

    # A ``table`` element whose ``location_label`` ends in " part N" (N >= 2)
    # is a continuation of an overlong table that parsing split into several
    # retrieval units (``parsers.py::_split_table_into_elements`` — the label
    # suffix is the label-generation contract that codepath documents; codex
    # N3). This predicate keeps only the first part (no " part N" suffix at
    # all, or exactly " part 1"), so a split table counts/enumerates once.
    #
    # Deliberately reads ``location_label`` and never ``metadata``:
    # `table_html` lives in `metadata`, is JSONB, and is TOASTed once it grows
    # past ~2KB — filtering on `metadata->>'table_part'` forces Postgres to
    # detoast (decompress) that whole blob for every candidate row even
    # though the predicate only needs a couple of bytes (measured ~22x more
    # buffer hits, 7ms -> 60-100ms on a 2M-row table). `location_label` is
    # short and never TOASTed, so this predicate still costs a heap fetch —
    # it is not part of `idx_source_elements_source_type`, so this is not an
    # index-only scan — but not a decompression.
    #   * ``!~ ' part ([2-9]|[1-9][0-9]+)$'`` — does NOT end in " part " plus
    #     a number >= 2 (single digit 2-9, or 2+ digits with no leading
    #     zero). "part 1" fails this alternation (excluded on purpose) and
    #     stays classified as a first part; "part 10"/"part 12" match the
    #     second alternative and stay classified as continuations.
    _NOT_TABLE_CONTINUATION_SQL = (
        "location_label !~ ' part ([2-9]|[1-9][0-9]+)$' "
    )

    def __init__(
        self,
        database: PostgresDatabase,
        *,
        now: Callable[[], TimestampInput],
        current_user_id: Callable[[], str] = lambda: "",
    ) -> None:
        self.database = database
        self.now = normalized_clock(now)
        self.current_user_id = current_user_id

    def all_visible_source_ids(
        self, notebook_id: str, *, digest_for_owner: str | None = None,
    ) -> list[str]:
        """Return the current visible-source universe for graph drift checks.

        ``digest_for_owner`` given -> NOT the ids but the drift probe's
        fingerprint: ``[visible digest, hidden digest]``, two md5 hex strings
        (``""`` for an empty half) over this universe and over
        ``hidden_source_ids(notebook_id, digest_for_owner)``, each in ``id``
        order joined by U+001E, read in ONE statement that returns one row.
        ``source_scope.universe_digest`` computes the same value from a frozen
        set, so equal digests are equal sets without shipping 49k ids per
        probe (``retrieval_candidates._unsafe_source_scope_restricted``).
        """
        if digest_for_owner is not None:
            with self.database.connect() as connection:
                row = connection.execute(
                    _UNIVERSE_DIGEST_SQL,
                    (notebook_id, notebook_id, str(digest_for_owner)),
                ).fetchone()
            return [
                str(row["visible_digest"] or ""), str(row["hidden_digest"] or ""),
            ]
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM sources WHERE notebook_id=%s "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE} ORDER BY id",
                (notebook_id,),
            ).fetchall()
        return [row["id"] for row in rows]

    def visible_source_ids_by_notebook(self, notebook_ids: Sequence[str]) -> dict[str, list[str]]:
        """Freeze a bounded global participant set in one read snapshot."""
        result = {notebook_id: [] for notebook_id in notebook_ids}
        if not result:
            return result
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,notebook_id FROM sources WHERE notebook_id = ANY(%s) "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE} ORDER BY notebook_id,id",
                (list(result),),
            ).fetchall()
        for row in rows:
            result[row["notebook_id"]].append(row["id"])
        return result

    def visible_source_owners(self, source_ids: Sequence[str]) -> dict[str, str]:
        """``{source_id: notebook_id}`` for those of ``source_ids`` that are
        visible sources NOW; a deleted or hidden one is absent.

        The terminal citation check's by-id visibility read: ONE statement over
        the sources an answer cites, so its cost follows the answer and not the
        size of the cited libraries. The id list is bounded by the answer's
        checkable references (one id per distinct cited source) and travels as
        ONE jsonb parameter -- never one placeholder per id, never ``= ANY``
        over a bound Python list -- so the statement text and its plan do not
        vary with the list's length; the ids probe the primary key
        (``test_global_citation_check_explain_pins``).
        """
        ids = list(dict.fromkeys(str(value) for value in source_ids if value))
        if not ids:
            return {}
        with self.database.connect() as connection:
            rows = connection.execute(
                # ``= ANY(ARRAY(subquery))``: the list is unnested ONCE (an
                # InitPlan) and the primary key is probed with it, in the
                # custom and the generic plan alike; an ``IN (SELECT ...)``
                # semi-join may instead hash a full index scan.
                "SELECT s.id,s.notebook_id FROM sources s WHERE s.id = ANY(ARRAY("
                "SELECT jsonb_array_elements_text(%s::jsonb))) "
                f"AND s.{VISIBLE_SOURCE_TYPES_PREDICATE}",
                (json.dumps(ids),),
            ).fetchall()
        return {row["id"]: row["notebook_id"] for row in rows}

    def source_owners(self, source_ids: Sequence[str]) -> dict[str, str]:
        """``{source_id: notebook_id}`` for those of ``source_ids`` that exist,
        of ANY type -- the hidden Memory and Knowhow projections included; a
        deleted id is absent.

        Ownership only, for the public report page's mount re-check (D-3):
        which library a cited source lives in, so a citation of a mounted
        library's projection is re-checked against the mount like any other.
        No content and no readability leave this read; whether another
        member's Memory may be published is decided before it, by
        ``share_disclosure.report_foreign_memory_ids``.  Same shape as
        ``visible_source_owners`` without its type predicate: ONE jsonb
        parameter unnested once and probed through the primary key
        (``test_public_page_owner_explain_pins``).
        """
        ids = list(dict.fromkeys(str(value) for value in source_ids if value))
        if not ids:
            return {}
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT s.id,s.notebook_id FROM sources s WHERE s.id = ANY(ARRAY("
                "SELECT jsonb_array_elements_text(%s::jsonb)))",
                (json.dumps(ids),),
            ).fetchall()
        return {row["id"]: row["notebook_id"] for row in rows}

    def hidden_source_ids(self, notebook_id: str, owner_id: str) -> list[str]:
        """Hidden Memory/Knowhow projection participants **for one user**, in
        stable id order — see the SQLite adapter for why the Memory owner
        filter lives in the SQL rather than on the result, and why this stays
        a separate read (Knowhow projections are notebook-wide, a Memory
        projection belongs to its ``memory_items.created_by``). "Readable by
        this user" is ``memory_sql.memory_source_readable``, not a copy."""
        with self.database.connect() as connection:
            rows = connection.execute(
                _HIDDEN_SOURCE_IDS_SQL, (notebook_id, owner_id),
            ).fetchall()
        return [row["id"] for row in rows]

    def visible_source_scope_snapshot(
        self, notebook_id: str, source_ids: Sequence[str]
    ) -> tuple[list[str], int]:
        """Validate compact selected ids and count the universe in one statement."""
        requested = list(dict.fromkeys(str(value) for value in source_ids if value))
        with self.database.connect() as connection:
            if not requested:
                total = int(connection.execute(
                    "SELECT COUNT(*) AS c FROM sources WHERE notebook_id=%s "
                    f"AND {VISIBLE_SOURCE_TYPES_PREDICATE}",
                    (notebook_id,),
                ).fetchone()["c"])
                return [], total
            requested_json = json.dumps(requested, ensure_ascii=False)
            rows = connection.execute(
                "WITH requested(id, ordinal) AS ("
                "SELECT value, ordinal FROM "
                "jsonb_array_elements_text(%s::jsonb) WITH ORDINALITY AS r(value, ordinal)"
                "), visible AS ("
                "SELECT requested.id, requested.ordinal FROM requested "
                "JOIN sources ON sources.id=requested.id "
                f"WHERE sources.notebook_id=%s AND {VISIBLE_SOURCE_TYPES_PREDICATE}"
                "), stats(visible_count) AS ("
                "SELECT COUNT(*) FROM sources WHERE notebook_id=%s "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE}"
                ") SELECT visible.id, stats.visible_count FROM stats "
                "LEFT JOIN visible ON TRUE ORDER BY visible.ordinal",
                (requested_json, notebook_id, notebook_id),
            ).fetchall()
            visible = [str(row["id"]) for row in rows if row["id"] is not None]
            total = int(rows[0]["visible_count"])
        return visible, total

    def list_sources(self, notebook_id: str) -> list[SourceSummary]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sources WHERE notebook_id=%s "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE} "
                "ORDER BY created_at,id COLLATE \"C\"",
                (notebook_id,),
            ).fetchall()
            return self.sources_from_rows(connection, rows)

    def list_sources_page(
        self,
        notebook_id: str,
        offset: int = 0,
        limit: int = 50,
        q: str = "",
    ) -> PaginatedSources:
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 200))
        needle = (q or "").strip().lower()
        where = f"WHERE notebook_id=%s AND {VISIBLE_SOURCE_TYPES_PREDICATE}"
        params: list[object] = [notebook_id]
        if needle:
            # 三腿 UNION 的 id 半连接,不是一个跨表 OR。生产实测(4.9 万 source 的
            # notebook):旧的 `LOWER(title) LIKE ? OR LOWER(file_name) LIKE ?
            # OR EXISTS(source_authors …) OR EXISTS(source_paper_meta …)` 让
            # planner 对整个 OR 结构选 hashed subplan —— source_authors 全表并行
            # 扫 21 万行、source_paper_meta 全表扫 3.9 万行,带 q 的 COUNT 单次
            # 363ms,页查询对同一份 where 再付一遍。判据是「2 个汉字的短词与 7
            # 字符的长词耗时几乎相同」(360ms vs 363ms):瓶颈是全表扫,不是 LIKE
            # 匹配本身。OR 底下的跨表 EXISTS 无法用索引各自收窄,而拆成三条各自
            # 独立、各自可走索引的腿再取并集就可以 —— 每条腿都以自己表的
            # `notebook_id=` 等值打头,配 migration 0048 的三条复合 GIN trgm 索引
            # (idx_sources_nb_title_file_trgm / idx_source_authors_nb_name_trgm /
            # idx_source_paper_meta_nb_ptitle_trgm)走 Bitmap Index Scan。
            #
            # 语义等价论证(三条,逐条对应一处写法):
            #
            # 1. 第一腿重复了外层的 `notebook_id=` 与 visible 谓词,不是冗余:
            #    外层 WHERE 已经把结果限定在「本 notebook 且可见」,所以 IN 集合
            #    只需要**包含**全部「可见且命中」的 id,多包含谁都被外层交掉。
            #    把 visible 谓词写进第一腿只会收窄 UNION 集合(交集不变),而它
            #    是 idx_sources_nb_title_file_trgm 那条 partial 索引可用的前提
            #    —— 谓词在 SQL 文本里是内联字面量(VISIBLE_SOURCE_TYPES_PREDICATE),
            #    所以 partial 谓词的蕴含在 generic plan 下同样成立(与迁移 0042
            #    Group 2 记的是同一套机制)。作者腿/论文腿不带 visible 谓词:
            #    隐藏源即使有作者行也进不了结果,外层交集拦得住。
            #
            # 2. 作者腿/论文腿改用**子表自己的** `notebook_id=` 收窄(旧形态是
            #    `a.source_id=sources.id` 回连外层),两者等价依赖「子表行的
            #    notebook_id 恒等于其 source 的 notebook_id」。核实过的写者:
            #    本文件的 upsert_paper_meta 先 `SELECT id FROM sources WHERE
            #    id=%s AND notebook_id=%s FOR KEY SHARE` 做归属校验再写两张表,
            #    ON CONFLICT 不更新 notebook_id;clear_paper_meta / 删源只 DELETE;
            #    notebook_copy_writer.py 的深拷贝对两张子表都同时改写 source_id 与
            #    notebook_id 到新库;sqlite_to_postgres / shadow 复制与
            #    scripts/merge_dbs.py 都是按列原样搬运。全仓没有任何一处
            #    `UPDATE sources SET notebook_id=…`,即不存在把 source 移到另一个
            #    notebook 的路径。
            #    历史脏数据的取舍(不是等价性漏洞,是明写的口径统一):早于当前
            #    写者的畸形行**可能**带着与其 source 不一致的 notebook_id ——
            #    report_source_rows 家族早就为此在 JOIN 上写了
            #    `AND m.notebook_id=s.notebook_id`,query_store 的 is_paper 计数
            #    也直接按 `source_paper_meta.notebook_id` 分组。本次改写把搜索腿
            #    收进同一口径:这类行不再算作本库的命中。两端各有一条用例把这个
            #    取舍钉死。
            #
            # 3. `paper_title` 可空,而 LIKE 对 NULL 求值为 NULL,在 WHERE 里与
            #    FALSE 同为 falsy —— 旧的 EXISTS 子查询与新的 UNION 腿在这一点上
            #    逐字同形,不需要额外的 IS NOT NULL。needle 里的 `%` / `_` 一如
            #    既往不转义(既有行为,本次不动)。
            #
            # 子查询里的 `FROM sources` 遮蔽了外层同名关系,这是有意的:IN 左边的
            # `sources.id` 在外层作用域求值,腿内的裸列名全部解析到腿自己的
            # `sources`,两侧都不需要别名。
            # A needle naming the current wording (「收录自」) adds one more leg that
            # matches a legacy-titled promotion source by its MAPPED title; every
            # other needle keeps this statement byte for byte.
            mapped = needle_names_current_wording(needle)
            where += (
                " AND sources.id IN ("
                "SELECT id FROM sources WHERE notebook_id=%s "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE} "
                "AND (LOWER(title) LIKE %s OR LOWER(file_name) LIKE %s) "
                + (f"UNION SELECT id FROM sources WHERE notebook_id=%s "
                   f"AND {promotion_gate_sql('source_type', 'id')} "
                   f"AND LOWER({mapped_title_sql('title')}) LIKE %s " if mapped else "")
                + "UNION SELECT a.source_id FROM source_authors a "
                "WHERE a.notebook_id=%s AND LOWER(a.name) LIKE %s "
                "UNION SELECT m.source_id FROM source_paper_meta m "
                "WHERE m.notebook_id=%s AND LOWER(m.paper_title) LIKE %s)"
            )
            like = f"%{needle}%"
            params.extend((notebook_id, like, like))
            if mapped:
                params.extend((notebook_id, like))
            params.extend((notebook_id, like, notebook_id, like))
        with self.database.connect() as connection:
            counted = connection.execute(
                "SELECT COUNT(*) AS c, "
                f"COUNT(*) FILTER (WHERE {NOT_PROMOTION_SOURCE_PREDICATE}) AS d "
                f"FROM sources {where}", params
            ).fetchone()
            total = counted["c"]
            rows = connection.execute(
                f"SELECT * FROM sources {where} "
                "ORDER BY created_at,id COLLATE \"C\" LIMIT %s OFFSET %s",
                (*params, limit, offset),
            ).fetchall()
            items = self.sources_from_rows(connection, rows)
        return PaginatedSources(
            items=items, total_count=total, offset=offset, limit=limit,
            # PR-E8: the document-limit count (SQLite twin)
            visible_document_count=None if needle else int(counted["d"]),
        )

    def find_sources_metadata_rows(
        self,
        connection,
        notebook_id: str,
        *,
        terms: Sequence[str] = (),
        year_from: int | None = None,
        year_to: int | None = None,
        venue: str = "",
        author: str = "",
        kind: str = "papers",
        allowed_source_ids: Sequence[str] | None = None,
        with_titles: bool = False,
    ) -> dict:
        """``find_sources``' metadata leg over ONE library (the canonical
        definition; SQLite mirrors it).  Up to three statements on the caller's
        connection:

        1. **the counts** (skipped when nothing can narrow: ``kind="all"``, no
           filter, no allow list) -- over the library's user-visible sources:
           how many there are, how many of them the allow list keeps
           (``scope_total``: the scope BEFORE the filters) and how many of
           those are eligible papers without a paper-meta row.  A year / venue
           / author filter excludes every such source -- it cannot be judged
           -- so they are reported as ``meta_pending_excluded`` (design §5.4).
        2. **the filtered scope**, ids only (``id, pub_year, pending`` --
           ``pending``: eligible for paper meta but none extracted yet; plus
           ``title, source_type, paper_title`` with ``with_titles``, the one
           ordering that needs a title for every row) -- every user-visible
           source of the library (``VISIBLE_SOURCE_TYPES_PREDICATE``, the
           source tab's predicate, so Memory/Knowhow projections never appear)
           that passes ``kind`` / year / venue / author and, when given,
           ``allowed_source_ids`` (``id_binding.member_of``, a custom plan: the
           run's frozen ceiling can hold the whole library).
           ``kind="papers"`` is ``is_paper=1`` plus the sources WITHOUT a
           paper-meta row that are eligible for one (``PAPER_META_ELIGIBLE_SQL``):
           metadata is extracted lazily and never backfilled, so an
           unextracted paper must still be findable.  Paper meta is joined on
           its OWN ``notebook_id`` as well (``list_sources_page`` comment,
           point 2).  Display rows are hydrated by the caller for one page only
           (``source_listing_rows`` / ``paper_meta_for_sources``).
        3. **the term legs** (only with ``terms``) -- 0048's shape: one leg per
           table and column group, each rooted at its own table's
           ``notebook_id=``, the visible predicate repeated as an inline
           literal on the ``sources`` legs so ``idx_sources_nb_title_file_trgm``'s
           partial predicate is implied, OR'd ``lower(col) LIKE`` per term with
           ``%``/``_``/backslash escaped (``source_find_terms.like_pattern``;
           PostgreSQL's LIKE escapes with backslash by default).  ``UNION ALL``:
           each leg reports the matched VALUE, and Python re-checks it
           (``find_sources_rows.assemble_scope``) for the field and the word
           boundary.  Bound to the scope's ids (``member_of`` on every leg)
           only when the filters really narrowed the library
           (``find_sources_rows.bind_term_legs``).

        Returns ``{"rows": [...], "scope_total", "visible_total",
        "meta_pending_excluded"}``; each row is ``source_id, notebook_id,
        pub_year, pending, matches`` (``{field: word_boundary}``, empty without
        a term hit) and ``sort_title`` with titles."""
        kind = check_kind(kind)
        folded = [term for term in dict.fromkeys(
            fold_term(value) for value in terms) if term]
        bound = None
        allowed = "TRUE"
        if allowed_source_ids is not None:
            bound = bind_ids(list(allowed_source_ids))
            allowed = member_of("s.id", bound)
        meta_filtered = (
            year_from is not None or year_to is not None
            or bool(fold_term(venue)) or bool(fold_term(author))
        )
        joined = (
            "FROM sources s LEFT JOIN source_paper_meta m "
            "ON m.source_id=s.id AND m.notebook_id=s.notebook_id "
            f"WHERE s.notebook_id=%s AND s.{VISIBLE_SOURCE_TYPES_PREDICATE}"
        )
        pending = f"(m.source_id IS NULL{PAPER_META_ELIGIBLE_SQL})"
        counts = None
        if bound is not None or kind != "all" or meta_filtered:
            counts = execute_bound(
                connection,
                "SELECT count(*) AS visible_total,"
                f"count(*) FILTER (WHERE {allowed}) AS scope_total,"
                f"count(*) FILTER (WHERE {allowed} AND {pending}) AS pending_total "
                f"{joined}",
                [*([bound.param] * 2 if bound is not None else []), notebook_id],
                bound,
            ).fetchone()
        where: list[str] = []
        params: list[object] = [notebook_id]
        if bound is not None:
            where.append(allowed)
            params.append(bound.param)
        if kind == "papers":
            where.append(
                "(m.is_paper=1 OR (m.source_id IS NULL"
                f"{PAPER_META_ELIGIBLE_SQL}))"
            )
        if year_from is not None:
            where.append("m.pub_year>=%s")
            params.append(int(year_from))
        if year_to is not None:
            where.append("m.pub_year<=%s")
            params.append(int(year_to))
        if fold_term(venue):
            where.append("lower(m.venue) LIKE %s")
            params.append(like_pattern(fold_term(venue)))
        if fold_term(author):
            where.append(
                "s.id IN (SELECT a.source_id FROM source_authors a "
                "WHERE a.notebook_id=%s AND lower(a.name) LIKE %s)"
            )
            params.extend((notebook_id, like_pattern(fold_term(author))))
        titles = ",s.title,s.source_type,m.paper_title" if with_titles else ""
        rows = scope_rows_dicts(
            execute_bound(
                connection,
                f"SELECT s.id,m.pub_year,{pending} AS pending{titles} {joined}"
                + "".join(f" AND {clause}" for clause in where),
                params, bound,
            ).fetchall(),
            notebook_id, with_titles=with_titles,
        )
        visible_total = len(rows) if counts is None else int(counts["visible_total"])
        scope_total = len(rows) if counts is None else int(counts["scope_total"])
        excluded = 0
        if meta_filtered and counts is not None:
            excluded = int(counts["pending_total"]) - sum(
                1 for row in rows.values() if row["pending"]
            )
        hit_rows: list = []
        if folded and rows:
            scope = (
                bind_ids(sorted(rows))
                if bind_term_legs(len(rows), visible_total) else None
            )
            statement, leg_params = _find_sources_term_legs(notebook_id, folded, scope)
            # ALWAYS a custom plan, list or no list: the statement text depends
            # only on the number of terms, so psycopg's auto-prepare (5th
            # execution) would share one prepared statement across different
            # terms and PostgreSQL would settle on a generic plan -- the
            # trigram GIN driven by an opaque pattern, ~450 ms for a 2-character
            # term on a 10k library where the custom plan takes ~20 ms (the
            # measurement doc).  Planning per call costs well under 1 ms here.
            hit_rows = execute_ids(connection, statement, leg_params).fetchall()
        return assemble_scope(
            rows, hit_rows, folded, scope_total=scope_total,
            visible_total=visible_total, meta_pending_excluded=excluded,
        )

    def visible_document_count(self, notebook_id: str) -> int:
        with self.database.connect() as connection:
            return self._visible_document_count_on(connection, notebook_id)

    @staticmethod
    def _visible_document_count_on(connection, notebook_id: str) -> int:
        """The visible-document COUNT on a caller-provided connection — shared
        by the read path and the atomic capacity gate inside the creation write
        transactions (mirrors the SQLite twin)."""
        # PR-E8: a promotion source never takes an uploaded-document slot
        # (SQLite twin).
        row = connection.execute(
            "SELECT COUNT(*) AS c FROM sources WHERE notebook_id=%s "
            f"AND {VISIBLE_SOURCE_TYPES_PREDICATE} "
            f"AND {NOT_PROMOTION_SOURCE_PREDICATE}",
            (notebook_id,),
        ).fetchone()
        return int(row["c"])

    @staticmethod
    def _lock_notebook_row_for_capacity(connection, notebook_id: str) -> None:
        """Serialize capacity-checked source creators on the notebook row.

        PostgreSQL's ``write()`` deliberately has no process-wide lock and runs
        READ COMMITTED, so two concurrent transactions could each COUNT, each
        see one free slot, and each INSERT — the transaction boundary alone
        closes nothing. ``FOR NO KEY UPDATE`` makes the second capacity-checked
        creator wait until the first commits; its COUNT (a fresh READ COMMITTED
        snapshot per statement) then includes the winner's row.

        Lock-mode conflict edges, stated precisely (do not paraphrase):

        * NOT blocked: the ``FOR KEY SHARE`` a plain FK insert takes — exempt
          writers (offline ``batch_ingest``, Memory/knowhow projection row
          inserts) never queue behind a capacity-checked upload's INSERT.
        * Blocked both ways, and NEW conflict edges this lock introduces:
          ordinary ``UPDATE notebooks SET …`` (rename, tier/status flips,
          ``updated_at`` bumps) takes FOR NO KEY UPDATE itself, and
          ``memory_store``'s ``FOR SHARE`` ownership probes conflict with it
          too. Both sides hold their locks for millisecond transactions, so
          this is brief queueing, not a correctness issue.
        * No deadlock by construction: this lock is the FIRST statement of its
          transaction, and everything after it (one COUNT, one INSERT whose FK
          check takes KEY SHARE on this very row, already held stronger) waits
          on nothing — a cycle needs a transaction that waits while holding,
          which this one never does. If ``postgres_lock_timeout_seconds``
          still trips (a long-lived holder elsewhere), the failure shape is
          psycopg's LockNotAvailable surfacing as a 500 — the pre-existing
          shape of every notebook-row lock timeout, not a new path.

        A vanished notebook raises ``KeyError`` (mapped to the existing 404).
        This existence probe is a PG-only bonus — the lock has to read the row
        anyway. The SQLite twin takes no row lock and deliberately adds no
        probe: both routes 404 earlier via ``get_row``/capability guards, so
        the divergence (SQLite would hit the FK on INSERT instead) is a
        registered backend difference on a production-unreachable path, not a
        parity contract."""
        row = connection.execute(
            "SELECT 1 FROM notebooks WHERE id=%s FOR NO KEY UPDATE",
            (notebook_id,),
        ).fetchone()
        if row is None:
            raise KeyError(notebook_id)

    def mark_indexing_chunk_fallback(self, source_id: str, warning_code: str) -> None:
        """见 SourceStorePort:非空写稳定前缀诊断(不覆盖既有 MinerU 诊断),空只清自己。"""
        prefix = INDEXING_CHUNK_FALLBACK_WARNING_PREFIX
        with self.database.write() as connection:
            if warning_code:
                connection.execute(
                    "UPDATE sources SET error_message=%s WHERE id=%s AND "
                    "(error_message IS NULL OR error_message='' OR error_message LIKE %s)",
                    (f"{prefix} {warning_code}", source_id, f"{prefix}%"),
                )
            else:
                connection.execute(
                    "UPDATE sources SET error_message='' WHERE id=%s "
                    "AND error_message LIKE %s",
                    (source_id, f"{prefix}%"),
                )

    @staticmethod
    def clear_chunked_at(connection, source_id: str) -> None:
        connection.execute(
            "UPDATE sources SET chunked_at=NULL WHERE id=%s", (source_id,)
        )

    def get_source(self, source_id: str) -> SourceDetail:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE id=%s", (source_id,)
            ).fetchone()
            if row is None:
                raise KeyError(source_id)
            paper_meta = self.paper_meta_for_sources(
                connection, [source_id], notebook_id=row["notebook_id"]
            ).get(source_id)
            summary = self.source_from_row(
                connection, row, paper_meta=paper_meta
            )
        return SourceDetail(
            **summary.model_dump(),
            file_path=row["file_path"],
            error_message=row["error_message"],
            paper_meta=self.paper_meta_model(paper_meta),
            updated_at=iso_timestamp(row["updated_at"]),
        )

    def source_exists(self, source_id: str) -> bool:
        with self.database.connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM sources WHERE id=%s", (source_id,)
                ).fetchone()
                is not None
            )

    @staticmethod
    def source_exists_tx(connection, source_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sources WHERE id=%s FOR KEY SHARE", (source_id,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def source_exists_for_update_tx(
        connection, source_id: str, notebook_id: str | None = None
    ) -> bool:
        """Take the aggregate lock before deleting projection children.

        Project completion takes ``FOR KEY SHARE`` on this row.  Teardown
        takes the conflicting lock first, before touching graph rows, which
        gives both paths one source -> derived-row lock order and prevents a
        deadlock or a projection resurrecting children after deletion.
        """
        if notebook_id is not None:
            SourceStore._lock_notebook_row_for_capacity(connection, notebook_id)
        return (
            connection.execute(
                "SELECT 1 FROM sources WHERE id=%s FOR UPDATE", (source_id,)
            ).fetchone()
            is not None
        )

    def source_elements(self, source_id: str) -> list[SourceElement]:
        self.get_source(source_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM source_elements WHERE source_id=%s "
                "ORDER BY created_at,id COLLATE \"C\"",
                (source_id,),
            ).fetchall()
        return [
            SourceElement(
                id=row["id"],
                source_id=row["source_id"],
                element_type=row["element_type"],
                location_label=row["location_label"],
                text=row["text"],
                metadata=json_value(row["metadata"], {}),
            )
            for row in rows
        ]

    def source_elements_after(
        self,
        source_id: str,
        after: "tuple[Any, str] | None",
        limit: int,
    ) -> "tuple[list[SourceElement], tuple[Any, str] | None]":
        """PostgreSQL twin of the whole-source keyset walk; see the SQLite
        adapter for why ``source_elements_page`` is not reused for it.

        ``created_at`` is ``timestamptz`` here, so the cursor carries the
        ``datetime`` this adapter returned — never a re-rendered string (same
        reason as ``source_element_type_page``).  The ordering matches
        ``source_elements`` verbatim, ``COLLATE "C"`` included.
        """
        limit = max(1, int(limit))
        params: list[Any] = [source_id]
        clause = ""
        if after is not None:
            clause = "AND (created_at, id COLLATE \"C\") > (%s, %s) "
            params.extend([after[0], after[1]])
        params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,source_id,element_type,location_label,text,"
                "metadata,created_at FROM source_elements WHERE source_id=%s "
                f"{clause}"
                "ORDER BY created_at,id COLLATE \"C\" LIMIT %s",
                tuple(params),
            ).fetchall()
        items = [
            SourceElement(
                id=row["id"],
                source_id=row["source_id"],
                element_type=row["element_type"],
                location_label=row["location_label"],
                text=row["text"],
                metadata=json_value(row["metadata"], {}),
            )
            for row in rows
        ]
        next_after = (
            (rows[-1]["created_at"], rows[-1]["id"]) if len(rows) == limit else None
        )
        return items, next_after

    def source_elements_page(
        self,
        source_id: str,
        offset: int = 0,
        limit: int = 40,
        anchor_element_id: str = "",
        *,
        positions: Sequence[int] | None = None,
    ) -> PaginatedSourceElements:
        """PostgreSQL twin of the bounded source-detail element reader.

        ``positions`` (keyword only) switches to the batched positional read
        whose contract lives in ``app.domain.source_positions``; see
        ``_elements_at_positions``."""
        if positions is not None:
            if anchor_element_id:
                raise ValueError("positions and anchor_element_id are exclusive")
            return self._elements_at_positions(source_id, positions)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        with self.database.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM sources WHERE id=%s", (source_id,)
            ).fetchone() is None:
                raise KeyError(source_id)
            total = int(connection.execute(
                "SELECT COUNT(*) AS c FROM source_elements WHERE source_id=%s",
                (source_id,),
            ).fetchone()["c"])
            if anchor_element_id:
                anchor = connection.execute(
                    "SELECT created_at,id FROM source_elements "
                    "WHERE source_id=%s AND id=%s",
                    (source_id, anchor_element_id),
                ).fetchone()
                if anchor is not None:
                    before = int(connection.execute(
                        "SELECT COUNT(*) AS c FROM source_elements "
                        "WHERE source_id=%s AND (created_at<%s OR "
                        "(created_at=%s AND id COLLATE \"C\"<%s))",
                        (source_id, anchor["created_at"], anchor["created_at"], anchor["id"]),
                    ).fetchone()["c"])
                    offset = (before // limit) * limit
            rows = connection.execute(
                "SELECT * FROM source_elements WHERE source_id=%s "
                "ORDER BY created_at,id COLLATE \"C\" LIMIT %s OFFSET %s",
                (source_id, limit, offset),
            ).fetchall()
        return PaginatedSourceElements(
            items=[
                SourceElement(
                    id=row["id"],
                    source_id=row["source_id"],
                    element_type=row["element_type"],
                    location_label=row["location_label"],
                    text=row["text"],
                    metadata=json_value(row["metadata"], {}),
                )
                for row in rows
            ],
            total_count=total,
            offset=offset,
            limit=limit,
        )

    # A positional read is ONE statement -- existence, count and the elements
    # -- in one of two shapes, both starting with the same three CTEs:
    #
    # * per offset: each position is a ``LATERAL ... OFFSET pos LIMIT 1`` walk
    #   of ``idx_source_elements_source_created`` and the heap, the plan the
    #   windowed read gets for one offset; the work is the sum of the offsets.
    # * head walk: one walk of that index up to the largest position
    #   (``span``) numbers the rows with ``row_number()``; only the hits go
    #   back to the heap, by primary key. The work is ``span`` -- index only
    #   where the visibility map allows; on a never-vacuumed document every
    #   row is still a heap visit and the walk measured ~1.5x a plain one.
    #
    # ``_positions_sql`` takes the head walk only when the offset sum is at
    # least twice ``span`` (so even the never-vacuumed walk is cheaper) and
    # large enough for its window and probes to pay off; a single position --
    # the "last element only" read -- therefore always keeps the per-offset
    # plan. Both choices were measured cell by cell (PR description).
    _POSITIONS_PREFIX = (
        "WITH present AS (SELECT id FROM sources WHERE id=%(source_id)s), "
        "counted AS (SELECT COUNT(*) AS c FROM source_elements "
        "WHERE source_id=%(source_id)s), "
        "wanted AS (SELECT pos,ord FROM unnest(%(positions)s::bigint[]) "
        "WITH ORDINALITY AS w(pos,ord)) "
    )
    _POSITIONS_PER_OFFSET_SQL = (
        _POSITIONS_PREFIX
        + "SELECT counted.c AS total_count, picked.* "
        "FROM present CROSS JOIN counted LEFT JOIN LATERAL ("
        "SELECT wanted.ord,element.id,element.source_id,element.element_type,"
        "element.location_label,element.text,element.metadata "
        "FROM wanted CROSS JOIN LATERAL (SELECT * FROM source_elements "
        "WHERE source_id=%(source_id)s "
        "ORDER BY created_at,id COLLATE \"C\" OFFSET wanted.pos LIMIT 1) AS element"
        ") AS picked ON true ORDER BY picked.ord"
    )
    _POSITIONS_HEAD_WALK_SQL = (
        _POSITIONS_PREFIX
        + ", ranked AS (SELECT id,row_number() OVER "
        "(ORDER BY created_at,id COLLATE \"C\")-1 AS pos "
        "FROM (SELECT created_at,id FROM source_elements "
        "WHERE source_id=%(source_id)s ORDER BY created_at,id COLLATE \"C\" "
        "LIMIT %(span)s) AS head) "
        "SELECT counted.c AS total_count, picked.* "
        "FROM present CROSS JOIN counted LEFT JOIN LATERAL ("
        "SELECT wanted.ord,element.id,element.source_id,element.element_type,"
        "element.location_label,element.text,element.metadata "
        "FROM wanted JOIN ranked ON ranked.pos=wanted.pos "
        "JOIN source_elements AS element ON element.id=ranked.id"
        ") AS picked ON true ORDER BY picked.ord"
    )
    _HEAD_WALK_MIN_RATIO = 2
    _HEAD_WALK_MIN_OFFSETS = 2048

    @classmethod
    def _positions_sql(cls, wanted: Sequence[int]) -> str:
        span = max(wanted, default=-1) + 1
        offsets = sum(position + 1 for position in wanted)
        if offsets >= max(cls._HEAD_WALK_MIN_RATIO * span, cls._HEAD_WALK_MIN_OFFSETS):
            return cls._POSITIONS_HEAD_WALK_SQL
        return cls._POSITIONS_PER_OFFSET_SQL

    def _elements_at_positions(
        self, source_id: str, positions: Sequence[int]
    ) -> PaginatedSourceElements:
        """Existence, count and every asked-for element in ONE statement.

        Under READ COMMITTED a single statement reads one snapshot, so the
        count and the elements can never disagree -- the property the old
        per-offset reads only approximated by comparing totals call by call.
        A missing source row means no row at all, raised as the ``KeyError``
        every positional caller already handles.
        """
        wanted = normalize_positions(positions)
        with self.database.connect() as connection:
            rows = connection.execute(
                self._positions_sql(wanted),
                {"source_id": source_id, "positions": wanted,
                 "span": max(wanted, default=-1) + 1},
            ).fetchall()
        if not rows:
            raise KeyError(source_id)
        return positions_page(
            [
                SourceElement(
                    id=row["id"],
                    source_id=row["source_id"],
                    element_type=row["element_type"],
                    location_label=row["location_label"],
                    text=row["text"],
                    metadata=json_value(row["metadata"], {}),
                )
                for row in rows
                if row["ord"] is not None
            ],
            int(rows[0]["total_count"]),
            wanted,
        )

    # ------------------------------------------- typed-collection catalog
    # Backend twin of the SQLite primitives; see that adapter's docstrings for
    # WHY the signal is (updated_at, parse_status, chunked_at).
    def source_change_signal_rows(
        self, connection: Any, notebook_id: str
    ) -> list[tuple[str, str, str, bool]]:
        """``[(source_id, opaque change signal, created_at sort key,
        user_visible)]`` for physical source rows, EXCLUDING the private Memory
        synthetic rows (see the SQLite adapter for why the exclusion is
        unconditional).

        The token is formatted here rather than in the service so the caller
        never has to know that PostgreSQL hands back ``datetime`` objects
        where SQLite hands back ISO text: both backends produce a string that
        is only ever compared for equality against a token from the same
        store.

        The created-at key is normalized by ``_sort_key`` for the same reason,
        but for ORDERING rather than equality: every row in one notebook goes
        through the same formatting, so lexicographic comparison over these
        strings reproduces this backend's ``ORDER BY created_at, id`` — the
        order ``list_sources`` returns and the source tab shows.

        ``user_visible`` is evaluated in SQL from this module's
        ``VISIBLE_SOURCE_TYPES_PREDICATE`` — the same constant ``list_sources``
        filters on — on a row this query already visits.  It exists so the
        catalog does NOT need a second ``sources`` read per notebook: nothing
        indexes ``source_type``, so a "which ids are hidden" query has to scan
        every source row of the notebook, and it would do that on the request
        path immediately after this query walked the same rows.
        """
        rows = connection.execute(
            "SELECT id,updated_at,parse_status,chunked_at,created_at,"
            f"({VISIBLE_SOURCE_TYPES_PREDICATE}) AS user_visible FROM sources "
            "WHERE notebook_id=%s AND source_type<>'memory'",
            (notebook_id,),
        ).fetchall()
        return [
            (
                row["id"],
                "{}|{}|{}".format(
                    row["updated_at"] if row["updated_at"] is not None else "",
                    row["parse_status"] or "",
                    row["chunked_at"] if row["chunked_at"] is not None else "",
                ),
                _sort_key(row["created_at"]),
                bool(row["user_visible"]),
            )
            for row in rows
        ]

    def memory_source_ids(
        self, connection: Any, notebook_id: str, *,
        holders_among: Sequence[str] | None = None,
    ) -> list[str]:
        """The notebook's private Memory synthetic source ids — the exact
        complement of the exclusion above; see the SQLite adapter for the
        single-definition argument and the cost shape.

        ``holders_among`` (E4-7): which of these notebooks hold any Memory
        source, in ONE statement; ``notebook_id`` is ignored.  The list is
        bound through ``id_binding`` (one parameter, a custom plan); each
        notebook is an index probe of ``idx_sources_nb_hidden_type``.
        EXPLAIN pin: tests/postgres/test_kg_service_readers_pg.py."""
        if holders_among is not None:
            notebooks = sorted({str(nb) for nb in holders_among if nb})
            if not notebooks:
                return []
            bound = bind_ids(notebooks)
            return [
                row["notebook_id"]
                for row in execute_ids(
                    connection,
                    _MEMORY_HOLDERS_SQL.format(
                        member=member_of("notebook_id", bound)),
                    (bound.param,),
                ).fetchall()
            ]
        return [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM sources "
                f"WHERE notebook_id=%s AND {MEMORY_SOURCE_TYPE_PREDICATE}",
                (notebook_id,),
            ).fetchall()
        ]

    def visible_parse_status_counts(
        self, connection: Any, notebook_id: str
    ) -> list[tuple[str, int]]:
        """``[(parse_status, count)]`` over this notebook's user-visible sources;
        see the SQLite adapter for why this is its own query rather than a
        widening of the (contractually opaque) change signal."""
        return [
            (str(row["parse_status"] or ""), int(row["c"]))
            for row in connection.execute(
                "SELECT parse_status, COUNT(*) AS c FROM sources "
                f"WHERE notebook_id=%s AND {VISIBLE_SOURCE_TYPES_PREDICATE} "
                f"AND {NOT_PROMOTION_SOURCE_PREDICATE} "
                "GROUP BY parse_status",
                (notebook_id,),
            ).fetchall()
        ]

    # Batch width for the typed-collection count, deliberately narrower than
    # the class-wide ``IN_CHUNK`` (5 000, tuned for id hydration that returns
    # one row per id).  This query returns up to len(element_types) rows PER
    # source, and the planner materializes the whole group before we see it,
    # so the batch bounds intermediate rows, not just bound parameters.  A
    # local constant rather than a change to ``IN_CHUNK``: that attribute is
    # shared with unrelated hydration paths this reasoning does not cover.
    COUNT_IN_CHUNK = 1024

    def element_type_count_rows(
        self,
        connection: Any,
        source_ids: Sequence[str],
        element_types: Sequence[str],
    ) -> list[tuple[str, str, int]]:
        """``[(source_id, element_type, count)]``, bounded and index-assisted
        via ``idx_source_elements_source_type``.

        ``= ANY(%s)`` keeps one bound parameter per list (no placeholder
        explosion).  The planner still chooses the access path — an index scan
        for a selective batch, a bitmap scan when the batch covers much of the
        table — and that choice is deliberately NOT part of the contract; what
        is fixed is that the element-type restriction stays in the query, so
        a source's prose is never read to count its formulas.

        ``table`` continuation parts (see ``_NOT_TABLE_CONTINUATION_SQL``) are
        excluded so a split table counts once — through a SEPARATE statement
        below rather than folded into the one above.  Folding it in would
        force a ``location_label`` read on every whitelisted row just to
        evaluate one type's condition, giving up the Index Only Scan the
        other kinds get from ``idx_source_elements_source_type`` alone.
        """
        ids = list(dict.fromkeys(value for value in source_ids if value))
        types = list(dict.fromkeys(value for value in element_types if value))
        if not ids or not types:
            return []
        plain_types = [t for t in types if t != "table"]
        out: list[tuple[str, str, int]] = []
        # A batched key probe (id_binding exemption class 2): each batch holds
        # at most COUNT_IN_CHUNK source ids, the leading column of
        # ``idx_source_elements_source_type``, and is the statement's only
        # selective predicate, so a generic plan probes the same index as a
        # custom one; the batches keep the plan cache instead of paying a
        # re-plan each.
        if plain_types:
            for offset in range(0, len(ids), self.COUNT_IN_CHUNK):
                batch = ids[offset : offset + self.COUNT_IN_CHUNK]
                rows = connection.execute(
                    "SELECT source_id,element_type,COUNT(*) AS c FROM source_elements "
                    "WHERE source_id=ANY(%s) AND element_type=ANY(%s) "
                    "GROUP BY source_id,element_type",
                    (batch, plain_types),
                ).fetchall()
                out.extend(
                    (row["source_id"], row["element_type"], int(row["c"]))
                    for row in rows
                )
        if "table" in types:
            for offset in range(0, len(ids), self.COUNT_IN_CHUNK):
                batch = ids[offset : offset + self.COUNT_IN_CHUNK]
                rows = connection.execute(
                    "SELECT source_id,COUNT(*) AS c FROM source_elements "
                    "WHERE source_id=ANY(%s) AND element_type='table' "
                    f"AND {self._NOT_TABLE_CONTINUATION_SQL}"
                    "GROUP BY source_id",
                    (batch,),
                ).fetchall()
                out.extend(
                    (row["source_id"], "table", int(row["c"])) for row in rows
                )
        return out

    # --------------------------------------- typed-collection enumeration
    def element_page_rows(
        self,
        connection: Any,
        source_id: str,
        element_type: str,
        after: tuple[object, str] | None,
        limit: int,
    ) -> list[Any]:
        """Backend twin of the SQLite keyset page; see that adapter for why the
        cursor value is passed back unparsed.

        Here it matters more than on SQLite: ``created_at`` is ``timestamptz``,
        so the value handed back is a ``datetime``.  Re-rendering it as text
        and re-parsing it would risk a microsecond/offset round-trip that
        silently skips or repeats a row at a page boundary.  Row values
        ``(created_at, id) > (%s, %s)`` are index-comparable, keeping this on
        ``idx_source_elements_source_type``.

        ``table`` enumeration additionally skips continuation parts (see
        ``_NOT_TABLE_CONTINUATION_SQL``): those rows are the same overlong
        table's later retrieval segments (``parsers.py::_split_table_into_elements``),
        not distinct tables, and listing each one would show a 400-row sheet
        as ~100 "tables".
        """
        params: list[Any] = [source_id, element_type]
        clause = ""
        if element_type == "table":
            clause += f"AND {self._NOT_TABLE_CONTINUATION_SQL}"
        if after is not None:
            clause += "AND (created_at,id) > (%s,%s) "
            params.extend([after[0], after[1]])
        params.append(max(1, int(limit)))
        return connection.execute(
            "SELECT id,source_id,element_type,location_label,text,created_at,"
            "metadata->>'asset_id' AS asset_id "
            "FROM source_elements WHERE source_id=%s AND element_type=%s "
            f"{clause}"
            "ORDER BY created_at,id LIMIT %s",
            tuple(params),
        ).fetchall()

    def source_display_rows(
        self, connection: Any, source_ids: Sequence[str]
    ) -> list[Any]:
        """Labels (and the owning notebook) for enumerated items, on the
        caller's connection."""
        ids = list(dict.fromkeys(value for value in source_ids if value))
        if not ids:
            return []
        out: list[Any] = []
        for offset in range(0, len(ids), self.COUNT_IN_CHUNK):
            batch = ids[offset : offset + self.COUNT_IN_CHUNK]
            out.extend(connection.execute(
                "SELECT s.id,s.notebook_id,s.title,s.file_name,"
                "m.is_paper,m.paper_title "
                "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id IS NOT DISTINCT FROM s.notebook_id "
                "WHERE s.id=ANY(%s)",
                (batch,),
            ).fetchall())
        return out

    def evidence_elements(
        self, element_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(value for value in element_ids if value))
        if not ids:
            return {}
        result: dict[str, dict[str, Any]] = {}
        with self.database.connect() as connection:
            for offset in range(0, len(ids), self.IN_CHUNK):
                batch = ids[offset : offset + self.IN_CHUNK]
                rows = connection.execute(
                    "SELECT id,source_id,element_type,location_label,text,metadata "
                    f"FROM source_elements WHERE id IN ({placeholders(batch)})",
                    batch,
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    item["metadata"] = _metadata_compat(item["metadata"])
                    result[row["id"]] = item
        return result

    def evidence_fingerprints(self, element_ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        """Validate original identities without transferring element bodies."""
        ids = list(dict.fromkeys(element_id for element_id in element_ids if element_id))
        if not ids:
            return {}
        result = {}
        with self.database.connect() as connection:
            for offset in range(0, len(ids), self.IN_CHUNK):
                batch = ids[offset:offset + self.IN_CHUNK]
                rows = connection.execute(
                    "SELECT id,source_id,encode(sha256(convert_to(text,'UTF8')),'hex') AS evidence_hash "
                    "FROM source_elements "
                    f"WHERE id IN ({placeholders(batch)})", batch,
                ).fetchall()
                for row in rows:
                    result[row["id"]] = (row["source_id"], row["evidence_hash"])
        return result

    def passage_evidence_snapshot(self, chunk_ids: Sequence[str]) -> dict[str, dict]:
        """A retrieved passage's own text and its elements' identities, together.

        The SQLite twin's contract, and the same reason for it -- see
        ``GlobalAskSourceStorePort``. ONE statement per batch and a chunk never
        spans two, so a passage's text digest and its element digests always
        come from one snapshot. PostgreSQL hashes both sides in SQL, so neither
        the passage text nor any element body ever crosses the wire.
        """
        ids = list(dict.fromkeys(chunk_id for chunk_id in chunk_ids if chunk_id))
        if not ids:
            return {}
        result: dict[str, dict] = {}
        with self.database.connect() as connection:
            for offset in range(0, len(ids), self.IN_CHUNK):
                batch = ids[offset:offset + self.IN_CHUNK]
                rows = connection.execute(
                    "SELECT c.id,"
                    "encode(sha256(convert_to(c.text,'UTF8')),'hex') AS text_sha,"
                    "e.id AS element_id,e.source_id AS element_source_id,"
                    "encode(sha256(convert_to(e.text,'UTF8')),'hex') AS element_hash "
                    "FROM chunks c "
                    "LEFT JOIN LATERAL jsonb_array_elements_text("
                    "c.element_ids::jsonb) declared(element_id) ON TRUE "
                    "LEFT JOIN source_elements e ON e.id=declared.element_id "
                    f"WHERE c.id IN ({placeholders(batch)})", batch,
                ).fetchall()
                for row in rows:
                    passage = result.setdefault(row["id"], {
                        "text_sha": row["text_sha"], "elements": {},
                    })
                    if row["element_id"] is not None:
                        passage["elements"][row["element_id"]] = (
                            row["element_source_id"], row["element_hash"],
                        )
        return result

    def image_asset_rows(
        self, element_ids: Sequence[str]
    ) -> list[tuple[str, Any]]:
        """``(id, metadata)`` for the image elements among ``element_ids``.

        The narrow sibling of ``evidence_elements`` — see ``SourceStorePort``
        for why the citation-image path may not reuse the wide reader: both
        predicates are pushed into SQL and ``text`` (the whole reason the wide
        read is expensive) is never selected.  ``metadata`` still goes through
        ``_metadata_compat`` so both backends hand the consumer the same JSON
        TEXT carrier.
        """
        ids = list(dict.fromkeys(value for value in element_ids if value))
        if not ids:
            return []
        out: list[tuple[str, Any]] = []
        with self.database.connect() as connection:
            for offset in range(0, len(ids), self.IN_CHUNK):
                batch = ids[offset : offset + self.IN_CHUNK]
                rows = connection.execute(
                    "SELECT id,metadata FROM source_elements "
                    f"WHERE id IN ({placeholders(batch)}) AND element_type='image'",
                    batch,
                ).fetchall()
                out.extend(
                    (row["id"], _metadata_compat(row["metadata"])) for row in rows
                )
        return out

    def question_suggestion_snapshot(
        self, notebook_id: str, *, source_limit: int, text_chars: int
    ) -> dict:
        """Bounded public-document projection, excluding all private synthetic sources.

        The identity digest detects corpus membership/lifecycle changes even
        outside the sampled prefix. Each selected row is clipped in SQL; no
        whole source text or library roster is hydrated.
        """
        with self.database.connect() as db:
            # Exact corpus identity in bounded keyset pages; an unchanged
            # MAX(timestamp) alone cannot detect a delete/add pair.
            digest = hashlib.sha256()
            source_count = 0
            after = None
            while True:
                lower = "AND (created_at,id)>(%s,%s) " if after else ""
                params = (notebook_id, *(after or ()), QUESTION_SUGGESTION_REVISION_PAGE_SIZE)
                page = db.execute(
                    "SELECT id,created_at,updated_at,parse_status,chunked_at FROM sources "
                    "WHERE notebook_id=%s AND source_type NOT IN ('memory','knowhow') "
                    + lower + "ORDER BY created_at,id LIMIT %s",
                    params,
                ).fetchall()
                if not page:
                    break
                source_count += len(page)
                for row in page:
                    digest.update(json.dumps(
                        [str(row[key] or "") for key in
                         ("id", "updated_at", "parse_status", "chunked_at")],
                        ensure_ascii=False,
                    ).encode())
                after = (page[-1]["created_at"], page[-1]["id"])
            revision = digest.hexdigest()
            # Admission precedes LIMIT: queued/failed operational summaries
            # and stale elements from an unsuccessful reparse are not evidence.
            # Keep those sources in the identity above so recovery invalidates
            # an earlier fallback; sample only successfully parsed text here.
            rows = db.execute(
                "SELECT s.id,s.updated_at,s.parse_status,s.chunked_at,"
                "substr(s.title,1,%s) AS title,substr(s.summary,1,%s) AS summary,"
                "(SELECT substr(e.text,1,%s) FROM source_elements e "
                "WHERE e.source_id=s.id AND btrim(e.text, chr(32)||chr(9)||chr(10)||chr(13))<>'' "
                "ORDER BY e.created_at,e.id LIMIT 1) AS excerpt "
                "FROM sources s WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND s.parse_status IN ('parsed','extracting','extracted') "
                "AND EXISTS (SELECT 1 FROM source_elements e WHERE e.source_id=s.id "
                "AND btrim(e.text, chr(32)||chr(9)||chr(10)||chr(13))<>'') "
                "ORDER BY s.created_at,s.id LIMIT %s",
                (text_chars, text_chars, text_chars, notebook_id, source_limit),
            ).fetchall()
        values = [dict(row) for row in rows]
        return {
            "source_count": source_count,
            "revision": revision,
            "sources": [
                {key: str(value or "") for key, value in row.items()}
                for row in values
            ],
        }

    def source_listing_rows(
        self, connection: Any, source_ids: Sequence[str]
    ) -> list[Any]:
        """The source-card projection (title / type / stored summary), on the
        caller's connection; ``source_metadata`` below is this method plus its
        own connection.  See the SQLite adapter for why they share one SQL."""
        ids = list(dict.fromkeys(value for value in source_ids if value))
        if not ids:
            return []
        out: list[Any] = []
        for offset in range(0, len(ids), self.IN_CHUNK):
            batch = ids[offset : offset + self.IN_CHUNK]
            out.extend(connection.execute(
                "SELECT s.id,s.notebook_id,s.title,s.file_name,s.summary,s.doc_type,"
                "s.source_type,m.is_paper,m.paper_title "
                "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id IS NOT DISTINCT FROM s.notebook_id "
                f"WHERE s.id IN ({placeholders(batch)})",
                batch,
            ).fetchall())
        return out

    def source_metadata(
        self, source_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(value for value in source_ids if value))
        if not ids:
            return {}
        with self.database.connect() as connection:
            return {
                row["id"]: dict(row)
                for row in self.source_listing_rows(connection, ids)
            }

    @staticmethod
    def retrieval_element_rows(connection, notebook_id: str, *, with_navigation: bool):
        """Every element of an unscoped, copyable notebook (the direct element
        fallback).  There is deliberately no source-list form: a source list
        routes ``_retrieve_elements`` to the bounded chunk path instead.
        ``with_navigation`` (the read-side switch) adds ``navigation``: true for
        an element some navigation chunk of the notebook lists in its own
        ``element_ids`` (a table of contents page, or a part of one), read
        forward from the flagged chunks -- the source of truth, so it holds in
        a notebook whose ``chunk_elements`` reverse index is not backfilled.
        The flagged set is one scan of the notebook's chunks, done once per
        call (this path only serves copyable notebooks); the caller drops
        those elements. Without it the statement is the historical one."""
        if not with_navigation:
            return connection.execute(
                "SELECT e.id,e.source_id,e.element_type,e.location_label,e.text,"
                "s.title AS source_title,m.vector AS vector "
                "FROM source_elements e JOIN sources s ON s.id=e.source_id "
                "LEFT JOIN element_embeddings m ON m.element_id=e.id "
                "WHERE s.notebook_id=%s ORDER BY e.ordinal",
                (notebook_id,),
            ).fetchall()
        return connection.execute(
            "WITH nav AS MATERIALIZED (SELECT DISTINCT x.value AS element_id "
            "FROM chunks c CROSS JOIN LATERAL jsonb_array_elements_text(c.element_ids) "
            "AS x(value) WHERE c.notebook_id=%s AND c.navigation<>0) "
            "SELECT e.id,e.source_id,e.element_type,e.location_label,e.text,"
            "s.title AS source_title,m.vector AS vector,"
            "e.id IN (SELECT element_id FROM nav) AS navigation "
            "FROM source_elements e JOIN sources s ON s.id=e.source_id "
            "LEFT JOIN element_embeddings m ON m.element_id=e.id "
            "WHERE s.notebook_id=%s ORDER BY e.ordinal",
            (notebook_id, notebook_id),
        ).fetchall()

    def report_source_rows(
        self, notebook_id: str, *, representative_limit: int = 20,
        distribution_limit: int = 32,
    ) -> dict[str, object]:
        """Bounded corpus profile: SQL aggregates plus representative sources."""
        representative_limit = max(1, min(int(representative_limit), 64))
        distribution_limit = max(1, min(int(distribution_limit), 64))
        with self.database.connect() as connection:
            totals = dict(connection.execute(
                "SELECT COUNT(*) AS total_sources,"
                "SUM(CASE WHEN COALESCE(m.is_paper,0)=1 "
                "AND BTRIM(COALESCE(m.paper_title,''))<>'' THEN 1 ELSE 0 END) AS metadata_sources,"
                "SUM(CASE WHEN COALESCE(m.is_paper,0)=1 "
                "AND m.pub_year BETWEEN 1000 AND 9999 THEN 1 ELSE 0 END) AS known_year_sources,"
                "SUM(CASE WHEN COALESCE(s.file_hash,'')='' AND NOT "
                "(COALESCE(m.is_paper,0)=1 AND BTRIM(COALESCE(m.paper_title,''))<>'') "
                "THEN 1 ELSE 0 END) AS identity_uncertain_sources "
                "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id "
                "WHERE s.notebook_id=%s AND s.source_type NOT IN ('memory','knowhow')",
                (notebook_id,),
            ).fetchone())
            type_rows = [dict(row) for row in connection.execute(
                "SELECT type,count FROM (SELECT "
                "COALESCE(NULLIF(BTRIM(s.doc_type),''),"
                "NULLIF(BTRIM(s.source_type),''),'unknown') AS type,COUNT(*) AS count "
                "FROM sources s WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow') GROUP BY 1) grouped "
                "ORDER BY count DESC,type COLLATE \"C\" LIMIT %s",
                (notebook_id, distribution_limit + 1),
            ).fetchall()]
            year_rows = [dict(row) for row in connection.execute(
                "SELECT m.pub_year AS year,COUNT(*) AS count FROM sources s "
                "JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND m.is_paper=1 AND m.pub_year BETWEEN 1000 AND 9999 GROUP BY m.pub_year "
                "ORDER BY m.pub_year DESC LIMIT %s",
                (notebook_id, distribution_limit + 1),
            ).fetchall()]
            duplicate_row = dict(connection.execute(
                "SELECT "
                "COALESCE((SELECT SUM(n-1) FROM (SELECT COUNT(*) AS n FROM sources s "
                "WHERE s.notebook_id=%s AND s.source_type NOT IN ('memory','knowhow') "
                "AND COALESCE(s.file_hash,'')<>'' GROUP BY s.file_hash HAVING COUNT(*)>1) d),0) "
                "AS hash_duplicate_excess,"
                "COALESCE((SELECT SUM(n-1) FROM (SELECT COUNT(*) AS n FROM sources s "
                "JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow') AND m.is_paper=1 "
                "AND BTRIM(COALESCE(m.paper_title,''))<>'' "
                "GROUP BY LOWER(BTRIM(m.paper_title)) HAVING COUNT(*)>1) d),0) "
                "AS title_duplicate_excess",
                (notebook_id, notebook_id),
            ).fetchone())
            representatives = [dict(row) for row in connection.execute(
                "WITH base AS (SELECT s.id,s.title,s.file_name,s.source_type,s.doc_type,"
                "m.paper_title,CASE WHEN m.is_paper=1 THEN m.pub_year ELSE NULL END AS pub_year,"
                "m.is_paper,s.created_at,"
                "COALESCE(NULLIF(BTRIM(s.doc_type),''),"
                "NULLIF(BTRIM(s.source_type),''),'unknown') AS type_key,"
                "CASE WHEN m.is_paper=1 AND m.pub_year BETWEEN 1000 AND 9999 "
                "THEN m.pub_year ELSE NULL END AS year_key FROM sources s "
                "LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow')),"
                "ranked AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY type_key "
                "ORDER BY created_at,id COLLATE \"C\") AS type_rank,"
                "ROW_NUMBER() OVER (PARTITION BY year_key "
                "ORDER BY created_at,id COLLATE \"C\") AS year_rank FROM base) "
                "SELECT id,title,file_name,source_type,doc_type,paper_title,pub_year,is_paper "
                "FROM ranked ORDER BY CASE WHEN type_rank=1 THEN 0 WHEN year_rank=1 THEN 1 ELSE 2 END,"
                "type_key COLLATE \"C\",year_key DESC NULLS LAST,created_at,"
                "id COLLATE \"C\" LIMIT %s",
                (notebook_id, representative_limit),
            ).fetchall()]
        return {
            **totals,
            **duplicate_row,
            "type_distribution": type_rows[:distribution_limit],
            "type_distribution_truncated": len(type_rows) > distribution_limit,
            "year_distribution": year_rows[:distribution_limit],
            "year_distribution_truncated": len(year_rows) > distribution_limit,
            "representatives": representatives,
        }

    def report_source_identity_rows(
        self, source_ids: Sequence[str]
    ) -> list[dict[str, object]]:
        ids = list(dict.fromkeys(str(value) for value in source_ids if value))[:1024]
        if not ids:
            return []
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT s.id,s.file_hash,m.paper_title,m.is_paper FROM sources s "
                "LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id WHERE s.id=ANY(%s)",
                (ids,),
            ).fetchall()
        return [dict(row) for row in rows]

    def source_titles(self, source_ids: list[str]) -> dict[str, str]:
        ids = [str(value) for value in source_ids if value]
        if not ids:
            return {}
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT id,title FROM sources WHERE id IN ({placeholders(ids)})",
                ids,
            ).fetchall()
        return {
            row["id"]: presented_source_title(row["title"], source_id=row["id"])
            for row in rows
        }

    def notebook_element_sample(
        self, notebook_id: str, *, max_chars: int = 8000
    ) -> list[dict]:
        """Deterministic, character-bounded schema-induction sample (SQLite twin's
        docstring). Memory elements are left out (M2, E5-1): the model paraphrases
        the sample into ``notebook_object_schemas`` (description, rationale, labels),
        a notebook-level row every member sees and every copy carries, so a Memory
        read here would reach both."""
        budget = max(0, int(max_chars))
        if budget == 0:
            return []
        result: list[dict] = []
        rendered = 0
        after_ordinal = 0
        with self.database.connect() as connection:
            while rendered < budget:
                rows = connection.execute(
                    "SELECT e.ordinal AS _ordinal,e.location_label,"
                    "substring(e.text FROM 1 FOR %s) AS text "
                    "FROM source_elements e JOIN sources s ON s.id=e.source_id "
                    "WHERE s.notebook_id=%s AND e.ordinal>%s "
                    f"AND {_SAMPLE_NOT_MEMORY} "
                    "ORDER BY e.ordinal LIMIT %s",
                    (budget, notebook_id, after_ordinal, 32),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    after_ordinal = int(row["_ordinal"])
                    location = str(row["location_label"] or "")
                    prefix = f"[{location}] "
                    separator = 1 if result else 0
                    available = budget - rendered - separator - len(prefix)
                    if available <= 0:
                        return result
                    text = str(row["text"] or "")[:available]
                    result.append({"location_label": location, "text": text})
                    rendered += separator + len(prefix) + len(text)
                    if rendered >= budget:
                        return result
        return result

    def insert_source(
        self,
        *,
        source_id: str,
        notebook_id: str,
        title: str,
        source_type: str,
        status: str,
        parse_status: str,
        file_name: str,
        file_path: str,
        file_size: int,
        file_hash: str,
        summary: str,
        doc_type: str,
        source_url: str = "",
        memory_id: str = "",
        agent_profile_id: str = "",
        connection=None,
        capacity_limit: "int | None" = None,
        unless_public_library: bool = False,
    ) -> bool:
        # ``unless_public_library`` (M1, E4-3): see the SQLite twin. Here the
        # INSERT … SELECT reads the notebook row FOR SHARE, which conflicts
        # with the publish UPDATE's row lock: a publish that committed first
        # is seen (the locking read re-checks the new row version and finds
        # tier='base' → nothing inserted); a publish that comes second waits
        # for this transaction. Returns whether the row was inserted.
        #
        # ``capacity_limit`` (None = exempt): enforce the notebook's
        # visible-document ceiling atomically with this insert — notebook-row
        # lock, then COUNT, then INSERT, all in one owned transaction (see
        # ``_lock_notebook_row_for_capacity`` for why the lock is the
        # serialization). Own-transaction shape only, same rule and reason as
        # the SQLite twin's docstring.
        if capacity_limit is not None and connection is not None:
            raise ValueError(
                "capacity_limit requires an owned write transaction; "
                "it cannot join a caller-provided connection"
            )
        statement = (
            "INSERT INTO sources"
            "(id,notebook_id,title,source_type,status,parse_status,file_name,file_path,"
            "source_url,file_size,file_hash,summary,doc_type,memory_id,"
            "agent_profile_id,created_at,updated_at,uploaded_by) "
            + (
                "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s "
                "FROM notebooks WHERE id=%s AND tier <> 'base' FOR SHARE"
                if unless_public_library else
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            )
        )
        now = self.now()
        values = (
            source_id,
            notebook_id,
            title,
            source_type,
            status,
            parse_status,
            file_name,
            file_path,
            source_url,
            file_size,
            file_hash,
            summary,
            doc_type,
            memory_id or None,
            # "" -> NULL, the same sentinel-free shape memory_id uses: NULL is
            # what every pre-v48 row carries and what the Agent-facing delete
            # check reads as "a person added this, never removable by an Agent".
            # Whitespace-only is the same statement as "": strip before the NULL
            # fold, or "   " lands a non-NULL row that reads as "an Agent added
            # this" while naming no agent. Same expression as the SQLite twin.
            (agent_profile_id or "").strip() or None,
            now,
            now,
            ((self.current_user_id() or "").strip() or None)
            if source_type not in {"memory", "knowhow"}
            else None,
        )
        if unless_public_library:
            values = (*values, notebook_id)
        visible = source_type not in {"memory", "knowhow"}
        if connection is not None:
            if visible:
                self._lock_notebook_row_for_capacity(connection, notebook_id)
            return connection.execute(statement, values).rowcount > 0
        with self.database.write() as owned:
            if visible:
                self._lock_notebook_row_for_capacity(owned, notebook_id)
            if capacity_limit is not None:
                current = self._visible_document_count_on(owned, notebook_id)
                if current >= capacity_limit:
                    raise DocumentCapacityExceeded(current, capacity_limit)
            return owned.execute(statement, values).rowcount > 0

    def source_id_for_memory(self, memory_id: str) -> str | None:
        if not memory_id:
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM sources WHERE memory_id=%s", (memory_id,)
            ).fetchone()
        return str(row["id"]) if row else None

    def set_status(
        self,
        source_id: str,
        status: str | None,
        *,
        summary: str | None = None,
        error_message: str = "",
    ) -> None:
        # None updates metadata without publishing or regressing a lifecycle state.
        fields = ["updated_at=%s"]
        params: list[object] = [self.now()]
        if status is not None:
            fields.extend(("status=%s", "parse_status=%s", "error_message=%s"))
            params.extend((status, status, error_message))
        if summary is not None:
            fields.append("summary=%s")
            params.append(summary)
        with self.database.write() as connection:
            connection.execute(
                f"UPDATE sources SET {','.join(fields)} WHERE id=%s",
                (*params, source_id),
            )

    def update_file_hash(
        self,
        source_id: str,
        file_hash: str,
        *,
        title: str | None = None,
        connection=None,
    ) -> None:
        fields = ["file_hash=%s", "updated_at=%s"]
        params: list[object] = [file_hash, self.now()]
        if title is not None:
            fields.insert(0, "title=%s")
            params.insert(0, title)
        statement = f"UPDATE sources SET {','.join(fields)} WHERE id=%s"
        values = (*params, source_id)
        if connection is not None:
            connection.execute(statement, values)
            return
        with self.database.write() as owned:
            owned.execute(statement, values)

    def source_id_by_hash(self, notebook_id: str, digest: str) -> str | None:
        """Existing source in this notebook whose content hash matches ``digest``
        (upload/batch dedup). Empty digest never matches (URL / metadata-only
        rows store ``file_hash=''``); memory/knowhow hidden-synthetic rows are
        excluded; oldest row wins (``ORDER BY created_at, id``). Mirrors the
        SQLite ``SourceStore.source_id_by_hash`` rule so UI and CLI dedup alike."""
        if not digest:
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM sources WHERE notebook_id=%s AND file_hash=%s "
                "AND source_type NOT IN ('memory', 'knowhow') "
                "ORDER BY created_at, id",
                (notebook_id, digest),
            ).fetchone()
        return str(row["id"]) if row else None

    def set_doc_type(self, source_id: str, doc_type: str) -> None:
        """Persist a corrected extraction-profile id on ONE source row (the
        upload path's 'same file, different doc_type' correction). Caller passes
        an already-normalized value; storage only, no vocabulary re-judging."""
        with self.database.write() as connection:
            connection.execute(
                "UPDATE sources SET doc_type=%s, updated_at=%s WHERE id=%s",
                (doc_type, self.now(), source_id),
            )

    def clear_paper_meta(self, source_id: str) -> None:
        """Reverse of upsert_paper_meta: drop this source's paper-meta marker row
        and author rows (retype to a non-paper doc_type). Idempotent."""
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM source_paper_meta WHERE source_id=%s", (source_id,)
            )
            connection.execute(
                "DELETE FROM source_authors WHERE source_id=%s", (source_id,)
            )
            # 论文卡片是论文元数据的派生缓存:不再是论文就同一事务里删掉。
            connection.execute(
                "DELETE FROM source_paper_cards WHERE source_id=%s", (source_id,)
            )

    def claim_failed_for_retry(self, source_id: str) -> bool:
        """Atomically flip a FAILED source to 'queued' to claim a retry; returns
        whether THIS caller won (rowcount==1). One WHERE-guarded UPDATE — the
        loser sees parse_status no longer 'failed' and must not reschedule."""
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE sources SET parse_status='queued', status='queued', "
                "error_message='', updated_at=%s "
                "WHERE id=%s AND parse_status='failed'",
                (self.now(), source_id),
            )
        return cursor.rowcount == 1

    def claim_reextract_if_extracted(self, source_id: str) -> bool:
        """Atomically flip a settled 'extracted' source to 'extracting' to claim a
        doc-type re-extraction; returns whether THIS caller won (rowcount==1)."""
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE sources SET parse_status='extracting', status='extracting', "
                "error_message='', updated_at=%s "
                "WHERE id=%s AND parse_status='extracted'",
                (self.now(), source_id),
            )
        return cursor.rowcount == 1

    def mark_extracted_if_doc_type(
        self, source_id: str, expected_doc_type: str, *, error_message: str = ""
    ) -> bool:
        """Atomically mark a source 'extracted' IFF its stored doc_type still
        equals ``expected_doc_type`` (what the just-finished extraction used);
        returns whether the terminal transition landed (rowcount==1). rowcount 0
        means a concurrent retype changed the type — the caller re-extracts."""
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE sources SET status='extracted', parse_status='extracted', "
                "error_message=%s, updated_at=%s "
                "WHERE id=%s AND doc_type=%s",
                (error_message, self.now(), source_id, expected_doc_type),
            )
        return cursor.rowcount == 1

    def insert_source_if_absent(
        self,
        *,
        source_id: str,
        notebook_id: str,
        digest: str,
        title: str,
        source_type: str,
        status: str,
        parse_status: str,
        file_name: str,
        file_path: str,
        file_size: int,
        summary: str,
        doc_type: str,
        agent_profile_id: str = "",
        capacity_limit: "int | None" = None,
    ) -> str | None:
        """Atomic content-dedup insert (postgres): the whole ``write()`` block is
        one transaction, so the dedup re-check and the insert commit together —
        two concurrent identical first-uploads can't both create a row. Returns an
        existing same-content visible source's id (caller reuses it, no row
        created) or None (a new row was inserted with ``file_hash=digest``).
        Mirrors the SQLite SourceStore rule (same dedup SELECT + insert_source
        shape); the SQLite side takes RESERVED via BEGIN IMMEDIATE, here the
        transaction boundary is the atomicity.

        ``agent_profile_id`` reaches the insert branch only — a reused row keeps
        the provenance of whoever created it (see the SQLite twin).

        ``capacity_limit`` (None = exempt) enforces the visible-document
        ceiling in this same transaction: the notebook-row lock is taken FIRST
        (before even the dedup re-check, so both re-checks run after the
        serialization point — see ``_lock_notebook_row_for_capacity``), the
        COUNT runs on this connection, and an at-or-over-limit notebook raises
        ``DocumentCapacityExceeded``. The dedup re-check still decides first:
        re-uploaded existing bytes reuse their row even in a full notebook."""
        with self.database.write() as connection:
            if source_type not in {"memory", "knowhow"}:
                self._lock_notebook_row_for_capacity(connection, notebook_id)
            if digest:
                row = connection.execute(
                    "SELECT id FROM sources WHERE notebook_id=%s AND file_hash=%s "
                    "AND source_type NOT IN ('memory', 'knowhow') "
                    "ORDER BY created_at, id",
                    (notebook_id, digest),
                ).fetchone()
                if row is not None:
                    return str(row["id"])
            if capacity_limit is not None:
                current = self._visible_document_count_on(connection, notebook_id)
                if current >= capacity_limit:
                    raise DocumentCapacityExceeded(current, capacity_limit)
            self.insert_source(
                source_id=source_id,
                notebook_id=notebook_id,
                title=title,
                source_type=source_type,
                status=status,
                parse_status=parse_status,
                file_name=file_name,
                file_path=file_path,
                file_size=file_size,
                file_hash=digest,
                summary=summary,
                doc_type=doc_type,
                agent_profile_id=agent_profile_id,
                connection=connection,
            )
        return None

    def replace_elements(
        self,
        connection,
        source_id: str,
        elements: Sequence[SourceElementWrite],
        *,
        created_at: TimestampInput,
    ) -> None:
        """Backend twin of the SQLite swap — including the ``updated_at`` bump
        that flips the change signal atomically with the new element
        generation (see the SQLite docstring for why it exists and why it is
        harmless)."""
        created_at = normalize_timestamp(created_at)
        source = connection.execute(
            "SELECT notebook_id,source_type FROM sources WHERE id=%s", (source_id,)
        ).fetchone()
        if source is None:
            raise KeyError(source_id)
        if source["source_type"] not in {"memory", "knowhow"}:
            self._lock_notebook_row_for_capacity(
                connection, str(source["notebook_id"])
            )
        # Fixed writer order is notebook -> source, matching the generation
        # publisher and closing both phantom-source and element-swap races.
        locked = connection.execute(
            "SELECT id FROM sources WHERE id=%s FOR UPDATE", (source_id,)
        ).fetchone()
        if locked is None:
            raise KeyError(source_id)
        connection.execute("DELETE FROM source_elements WHERE source_id=%s", (source_id,))
        execute_many(
            connection,
            "INSERT INTO source_elements"
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    item.id,
                    source_id,
                    item.element_type,
                    item.location_label,
                    item.text,
                    jsonb(dict(item.metadata)),
                    created_at,
                )
                for item in elements
            ],
        )
        connection.execute(
            "UPDATE sources SET updated_at=%s WHERE id=%s", (created_at, source_id)
        )

    def delete_source_row(self, connection, source_id: str) -> None:
        self.delete_source_rows(connection, [source_id])

    @staticmethod
    def delete_source_rows(connection, source_ids: Sequence[str]) -> None:
        """Delete source rows (elements and the other child rows go through
        their cascading foreign keys) — the batch form ``delete_source_row``
        uses, so a single delete and a Memory purge page share one statement."""
        ids = sorted({str(source_id) for source_id in source_ids if source_id})
        if ids:
            connection.execute("DELETE FROM sources WHERE id = ANY(%s)", (ids,))

    @staticmethod
    def lock_sources_for_teardown_tx(
        connection, source_ids: Sequence[str]
    ) -> list[dict]:
        """``FOR UPDATE`` the existing rows among ``source_ids``, in id order,
        and return ``{id, notebook_id, source_type, file_path, is_memory}``
        for each (``is_memory`` is ``memory_sql``'s Memory-source predicate).

        The batch form of ``source_exists_for_update_tx`` (without its
        capacity lock): the teardown takes the aggregate lock before any
        derived row, and taking every lock of a batch in one id-ordered
        statement gives two concurrent teardowns one global order. A missing
        id is simply absent (already removed)."""
        ids = sorted({str(source_id) for source_id in source_ids if source_id})
        if not ids:
            return []
        rows = connection.execute(
            "SELECT id,notebook_id,source_type,file_path,"
            f"({memory_source_type_predicate()}) AS is_memory FROM sources "
            "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (ids,),
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_elements(
        self,
        connection,
        source_id: str,
        elements: Sequence[SourceElementWrite],
        *,
        created_at: TimestampInput,
    ) -> None:
        created_at = normalize_timestamp(created_at)
        execute_many(
            connection,
            "INSERT INTO source_elements"
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    item.id,
                    source_id,
                    item.element_type,
                    item.location_label,
                    item.text,
                    jsonb(dict(item.metadata)),
                    created_at,
                )
                for item in elements
            ],
        )

    def delete_elements_by_knowhow_row(
        self, connection, source_id: str, row_id: str
    ) -> None:
        connection.execute(
            "DELETE FROM source_elements WHERE source_id=%s "
            "AND metadata #>> '{knowhow,row_id}'=%s",
            (source_id, row_id),
        )

    @staticmethod
    def _promotion_sources_in_graph(connection, rows) -> set:
        """PR-E8: see the SQLite twin."""
        ids = sorted({
            str(row["id"]) for row in rows
            if row["source_type"] == PROMOTION_SOURCE_TYPE
        })
        if not ids:
            return set()
        bound = bind_ids(ids)
        return {
            str(found["source_id"]) for found in execute_ids(
                connection,
                "SELECT DISTINCT source_id FROM knowledge_object_sources "
                f"WHERE {member_of('source_id', bound)}",
                (bound.param,),
            ).fetchall()
        }

    def source_from_row(
        self,
        connection,
        row: Mapping[str, Any],
        *,
        paper_meta: dict | None | object = _UNSET,
    ) -> SourceSummary:
        source_id = row["id"]
        element_count = int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM source_elements WHERE source_id=%s",
                (source_id,),
            ).fetchone()["count"]
        )
        # 三个 run 子查询原本相关到 ``o.source_id``,写在 EXISTS 内部,于是每扫到一行
        # knowledge_objects 就重算一遍;而它们只依赖 source_id,对这次调用是**常量**。
        # 提到 EXISTS 外面直接绑定 %s 之后:KO 存在性是一次 EXISTS 索引探针
        # (idx_knowledge_objects_source 首行即停,单行路径没有连接),run 判定是 ≤3 次
        # 索引探针(idx_extraction_runs_source_created)。``o.source_id<>''`` 保留在
        # EXISTS 内:等值连接把它传递到绑定参数上,与原查询逐字等价(绑定的 id 为空串
        # 时两版都判 false)。批量兄弟见 sources_from_rows——那边驱动集是页内多个 id,
        # 才是真正的半连接。
        kg_extracted = bool(
            connection.execute(
                "SELECT EXISTS(SELECT 1 FROM knowledge_objects o "
                "WHERE o.source_id=%s AND o.source_id<>'') AND COALESCE(("
                "SELECT r.status FROM extraction_runs r "
                "WHERE r.source_id=%s AND r.run_type='kg' "
                "ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1"
                "),'completed')='completed' AND NOT ("
                "COALESCE((SELECT r.error_message FROM extraction_runs r "
                " WHERE r.source_id=%s AND r.run_type='kg' "
                " ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1),'') "
                " ~ 'windows_failed=[1-9][0-9]*/[0-9]+' OR "
                "strpos(COALESCE((SELECT r.error_message FROM extraction_runs r "
                " WHERE r.source_id=%s AND r.run_type='kg' "
                " ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1),''),"
                " 'retry_incomplete=1')>0) AS ok",
                (source_id, source_id, source_id, source_id),
            ).fetchone()["ok"]
        )
        if row["source_type"] == PROMOTION_SOURCE_TYPE:
            # PR-E8: see the SQLite twin -- cited by some object's evidence.
            kg_extracted = source_id in self._promotion_sources_in_graph(
                connection, [row]
            )
        meta = (
            self.paper_meta_for_sources(
                connection, [source_id], notebook_id=row["notebook_id"]
            ).get(source_id)
            if paper_meta is _UNSET
            else paper_meta
        )
        # 一次读取喂两个派生字段(告警 + 「已分析但零产出」);见 SQLite 侧同名方法。
        latest_run_status, latest_run_error = self.latest_run_state(
            connection, source_id
        )
        summary = SourceSummary(
            id=source_id,
            notebook_id=row["notebook_id"],
            title=row["title"],
            display_title=summary_display_title(row, meta),
            type=row["source_type"],
            status=row["status"],
            summary=row["summary"],
            element_count=element_count,
            file_name=row["file_name"],
            file_size=row["file_size"],
            file_hash=row["file_hash"],
            parse_status=row["parse_status"],
            created_at=iso_timestamp(row["created_at"]),
            created_label=_created_label(row["created_at"]),
            doc_type=row.get("doc_type", ""),
            source_url=row.get("source_url", ""),
            # v48 provenance: "the column is not NULL", never the raw agent id.
            agent_created=row.get("agent_profile_id") is not None,
            extraction_warning=extraction_warning_text(latest_run_error),
            parse_quality_warning=has_pdf_python_fallback_warning(row["error_message"]),
            indexing_chunk_fallback=has_indexing_chunk_fallback_warning(row["error_message"]),
            kg_extracted=kg_extracted,
            kg_analyzed_empty=kg_analyzed_without_objects(
                latest_run_status, latest_run_error
            ),
            authors=[author["name"] for author in meta["authors"]] if meta else [],
            pub_year=meta["pub_year"] if meta else None,
            venue=meta["venue"] if meta else None,
        )
        summary.paper_meta_status = self._paper_meta_status_for(row, meta)
        return summary

    def sources_from_rows(
        self, connection, rows: list[Mapping[str, Any]]
    ) -> list[SourceSummary]:
        if not rows:
            return []
        source_ids = [row["id"] for row in rows]
        paper_meta = self.paper_meta_for_rows(connection, rows)
        element_counts: dict[str, int] = {}
        kg_extracted_ids: set[str] = set()
        latest_error: dict[str, str] = {}
        latest_status: dict[str, str] = {}
        for offset in range(0, len(source_ids), self.IN_CHUNK):
            batch = source_ids[offset : offset + self.IN_CHUNK]
            marker = placeholders(batch)
            for row in connection.execute(
                "SELECT source_id,COUNT(*) AS c FROM source_elements "
                f"WHERE source_id IN ({marker}) GROUP BY source_id",
                batch,
            ).fetchall():
                element_counts[row["source_id"]] = int(row["c"])
            # kg_extracted 的驱动集是**页内的 source id**,不是 knowledge_objects 行。
            #
            # 原查询 ``SELECT DISTINCT o.source_id FROM knowledge_objects o WHERE ...``
            # 把三个 run 子查询挂在每一行 KO 上;它们只依赖 ``o.source_id``,对固定
            # source 是常量,而 DISTINCT 又把结果折回 source 粒度——所以「每行算一次」
            # 的开销纯属浪费。现场数据:4.9 万 source 的 notebook 取一页 50 条,页内
            # 共 33.9 万 KO 行,这一条查询 3650ms、101 万次子查询执行、508 万 shared
            # buffers,是来源页签 3.3s 墙钟的大头。
            #
            # 改写后每个 source 至多 3 次 run 索引探针(idx_extraction_runs_source_created)
            # 加 1 次 KO 半连接(idx_knowledge_objects_source,首行即停),一页 50 个 id
            # 约 200 次探测取代百万次。``o.source_id<>''`` 留在 EXISTS 内:等值连接把它
            # 传递到 page_sources.source_id 上,与原查询逐字等价。
            #
            # 这不依赖「每个 source 只有一条 run」这一生产观测:latest-run 的取法
            # (同一条 ORDER BY + LIMIT 1)一字未动,多条 run 时语义与原查询一致。
            #
            # 同一教训的前例见 knowledge_counts_cache.py 的 _pending_sql:那边把
            # latest-run 标量子查询留在了 sources 驱动的 LATERAL 里,没有下沉进
            # knowledge_objects 的 EXISTS。
            #
            # CTE 用 ``WITH x(col) AS (VALUES ...)`` 而不是 LATERAL 或 ``AS t(col)``
            # 表别名列名:前者两端都支持,后两者 SQLite 没有,而这条查询与 SQLite 侧
            # 同名方法是同构的一对(见那边同一处注释)。
            page_values = ",".join("(%s)" for _ in batch)
            for row in connection.execute(
                f"WITH page_sources(source_id) AS (VALUES {page_values}) "
                "SELECT source_id FROM page_sources "
                "WHERE EXISTS(SELECT 1 FROM knowledge_objects o "
                " WHERE o.source_id=page_sources.source_id AND o.source_id<>'') "
                "AND COALESCE((SELECT r.status FROM extraction_runs r "
                "WHERE r.source_id=page_sources.source_id AND r.run_type='kg' "
                "ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1),'completed')='completed' "
                "AND NOT (COALESCE((SELECT r.error_message FROM extraction_runs r "
                " WHERE r.source_id=page_sources.source_id AND r.run_type='kg' "
                " ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1),'') "
                " ~ 'windows_failed=[1-9][0-9]*/[0-9]+' OR "
                "strpos(COALESCE((SELECT r.error_message FROM extraction_runs r "
                " WHERE r.source_id=page_sources.source_id AND r.run_type='kg' "
                " ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1),''),"
                " 'retry_incomplete=1')>0)",
                batch,
            ).fetchall():
                kg_extracted_ids.add(row["source_id"])
            for row in connection.execute(
                "SELECT source_id,status,error_message FROM extraction_runs "
                f"WHERE source_id IN ({marker}) "
                "ORDER BY source_id COLLATE \"C\",created_at DESC,ordinal DESC",
                batch,
            ).fetchall():
                # 同一行的两列一起取:选中的是哪一行完全由上面的 ORDER BY 决定,
                # status 只是搭同一趟车,不新增查询。
                if row["source_id"] in latest_error:
                    continue
                latest_error[row["source_id"]] = row["error_message"] or ""
                latest_status[row["source_id"]] = row["status"] or ""

        def warning(source_id: str) -> str | None:
            # 派生规则只有一份(app.models.sources),这里只负责取到「最近一次抽取的
            # error_message」这个输入。
            return extraction_warning_text(latest_error.get(source_id, ""))

        kg_extracted_ids |= self._promotion_sources_in_graph(connection, rows)
        output: list[SourceSummary] = []
        for row in rows:
            source_id = row["id"]
            meta = paper_meta.get(source_id)
            summary = SourceSummary(
                id=source_id,
                notebook_id=row["notebook_id"],
                title=row["title"],
                display_title=summary_display_title(row, meta),
                type=row["source_type"],
                status=row["status"],
                summary=row["summary"],
                element_count=element_counts.get(source_id, 0),
                file_name=row["file_name"],
                file_size=row["file_size"],
                file_hash=row["file_hash"],
                parse_status=row["parse_status"],
                created_at=iso_timestamp(row["created_at"]),
                created_label=_created_label(row["created_at"]),
                doc_type=row.get("doc_type", ""),
                source_url=row.get("source_url", ""),
                # v48 provenance: "the column is not NULL", never the raw agent id.
                agent_created=row.get("agent_profile_id") is not None,
                extraction_warning=warning(source_id),
                parse_quality_warning=has_pdf_python_fallback_warning(row["error_message"]),
            indexing_chunk_fallback=has_indexing_chunk_fallback_warning(row["error_message"]),
                kg_extracted=source_id in kg_extracted_ids,
                kg_analyzed_empty=kg_analyzed_without_objects(
                    latest_status.get(source_id, ""),
                    latest_error.get(source_id, ""),
                ),
                authors=[author["name"] for author in meta["authors"]] if meta else [],
                pub_year=meta["pub_year"] if meta else None,
                venue=meta["venue"] if meta else None,
            )
            summary.paper_meta_status = self._paper_meta_status_for(row, meta)
            output.append(summary)
        return output

    def extraction_warning(self, connection, source_id: str) -> str | None:
        return extraction_warning_text(
            self.latest_run_state(connection, source_id)[1]
        )

    @staticmethod
    def latest_run_state(connection, source_id: str) -> "tuple[str, str]":
        """最近一次抽取记录的 ``(status, error_message)``——两个派生字段的共同输入。
        与 SQLite 侧同名方法逐条对应(含「刻意不加 run_type 过滤」那条理由)。"""
        run = connection.execute(
            "SELECT status,error_message FROM extraction_runs WHERE source_id=%s "
            "ORDER BY created_at DESC,ordinal DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if run is None:
            return ("", "")
        return (str(run["status"] or ""), str(run["error_message"] or ""))

    @staticmethod
    def meta_source_rows(
        connection, notebook_id: str, pending_source_id: str = ""
    ) -> list[dict]:
        rows = connection.execute(
            "SELECT title,doc_type,CASE WHEN status IN ('parsed','extracting','extracted') "
            "THEN summary ELSE '' END AS summary FROM sources WHERE notebook_id=%s "
            "AND source_type NOT IN ('memory','knowhow') "
            "ORDER BY created_at,id COLLATE \"C\"",
            (notebook_id,),
        ).fetchall()
        return [
            {"title": row["title"], "doc_type": row["doc_type"], "summary": row["summary"]}
            for row in rows
        ]

    def meta_sources(self, notebook_id: str, pending_source_id: str = "") -> list[dict]:
        with self.database.connect() as connection:
            return self.meta_source_rows(connection, notebook_id, pending_source_id)

    def upsert_paper_meta(self, source_id: str, notebook_id: str, meta: dict) -> None:
        now = self.now()
        raw_json = json_value(meta.get("raw_json") or {}, {})
        with self.database.write() as connection:
            notebook = connection.execute(
                "SELECT id FROM notebooks WHERE id=%s FOR KEY SHARE",
                (notebook_id,),
            ).fetchone()
            if notebook is None:
                raise KeyError(notebook_id)
            source = connection.execute(
                "SELECT id FROM sources WHERE id=%s AND notebook_id=%s "
                "FOR KEY SHARE",
                (source_id, notebook_id),
            ).fetchone()
            if source is None:
                raise KeyError(source_id)
            connection.execute(
                "INSERT INTO source_paper_meta"
                "(source_id,notebook_id,is_paper,paper_title,venue,pub_year,doi,keywords,"
                "raw_json,model,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(source_id) DO UPDATE SET "
                # notebook_id 已由上面的归属校验确认是这条 source 的库;冲突时一并
                # 写回,畸形遗留行(子表 notebook_id 与 source 不一致)重抽一次即
                # 被改正,而不会被 paper_meta_for_sources 的收窄永远藏住。正常行
                # 上它与旧值相同。作者行本来就整组按这个 notebook_id 重写。
                "notebook_id=excluded.notebook_id,"
                "is_paper=excluded.is_paper,paper_title=excluded.paper_title,"
                "venue=excluded.venue,pub_year=excluded.pub_year,doi=excluded.doi,"
                "keywords=excluded.keywords,raw_json=excluded.raw_json,model=excluded.model,"
                "updated_at=excluded.updated_at",
                (
                    source_id,
                    notebook_id,
                    1 if meta.get("is_paper") else 0,
                    meta.get("paper_title"),
                    meta.get("venue"),
                    meta.get("pub_year"),
                    meta.get("doi"),
                    jsonb(meta.get("keywords") or []),
                    jsonb(raw_json),
                    str(meta.get("model") or ""),
                    now,
                    now,
                ),
            )
            connection.execute("DELETE FROM source_authors WHERE source_id=%s", (source_id,))
            execute_many(
                connection,
                "INSERT INTO source_authors"
                "(id,source_id,notebook_id,position,name,affiliation,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                [
                    (
                        f"{source_id}:auth:{int(author.get('position', 0)):03d}",
                        source_id,
                        notebook_id,
                        int(author.get("position", 0)),
                        str(author.get("name") or "").strip(),
                        str(author.get("affiliation") or "").strip(),
                        now,
                    )
                    for author in meta.get("authors") or []
                ],
            )

    @staticmethod
    def _paper_meta_dict(row: Mapping[str, Any], authors: list[Mapping[str, Any]]) -> dict:
        return {
            "source_id": row["source_id"],
            "is_paper": bool(row["is_paper"]),
            "paper_title": row["paper_title"],
            "venue": row["venue"],
            "pub_year": row["pub_year"],
            "doi": row["doi"],
            "keywords": json_value(row["keywords"], []),
            "model": row["model"],
            "authors": [
                {
                    "position": author["position"],
                    "name": author["name"],
                    "affiliation": author["affiliation"],
                }
                for author in authors
            ],
        }

    @staticmethod
    def _paper_meta_status_for(
        row: Mapping[str, Any], meta: dict | None
    ) -> str | None:
        """SQLite 孪生方法的对等实现:分类规则本身只有一份
        (`app.models.sources.paper_meta_status`),这里只翻译入参。"""
        return paper_meta_status(
            None if meta is None else bool(meta.get("is_paper")),
            row.get("source_type", ""),
            row.get("doc_type", ""),
            row.get("parse_status", ""),
        )

    @staticmethod
    def paper_meta_model(meta: dict | None) -> PaperMeta | None:
        if meta is None:
            return None
        return PaperMeta(
            is_paper=meta["is_paper"],
            title=meta["paper_title"],
            venue=meta["venue"],
            year=meta["pub_year"],
            doi=meta["doi"],
            keywords=list(meta["keywords"]),
            authors=[
                PaperAuthor(name=author["name"], affiliation=author["affiliation"])
                for author in meta["authors"]
            ],
        )

    def get_paper_meta(self, source_id: str, notebook_id: str) -> dict | None:
        """``notebook_id`` 是这条 source 自己所在的库(``sources.notebook_id``),
        不是调用方此刻所在的库;口径见 ``paper_meta_for_sources``。"""
        with self.database.connect() as connection:
            return self.paper_meta_for_sources(
                connection, [source_id], notebook_id=notebook_id
            ).get(source_id)

    def paper_meta_for_rows(
        self, connection, rows: Sequence[Mapping[str, Any]]
    ) -> dict[str, dict]:
        """``paper_meta_for_sources`` 的按行入口:按每行 source 自己的
        ``notebook_id`` 分组后逐组水合。现有调用方(列表页)一页只有一个库,
        于是恰好一组、语句数与按 id 直接调用相同;分组只是为了让一页跨库的
        行也各按各的库收窄,而不是被第一行的库误收窄。"""
        by_notebook: dict[str, list[str]] = {}
        for row in rows:
            by_notebook.setdefault(row["notebook_id"], []).append(row["id"])
        hydrated: dict[str, dict] = {}
        for notebook_id, ids in by_notebook.items():
            hydrated.update(
                self.paper_meta_for_sources(connection, ids, notebook_id=notebook_id)
            )
        return hydrated

    def paper_meta_for_sources(
        self, connection, source_ids: Sequence[str], *, notebook_id: str
    ) -> dict[str, dict]:
        """按 source_id 批量取 source_paper_meta/source_authors,并按子表自身的
        ``notebook_id`` 收窄到 ``notebook_id``(这些 source 自己所在的库)。

        与 list_sources_page 的搜索腿、report_source_rows 家族
        (``m.notebook_id=s.notebook_id``)和 notebook_analytics 的 is_paper 计数
        同一口径:子表行的 notebook_id 与其 source 不一致的畸形遗留行不算这条
        source 的论文元数据——不显示、不进 paper_meta_status,也不让元数据抽取
        的幂等探针(get_paper_meta)判成「已有」,于是下一次抽取会重写它,
        upsert_paper_meta 在冲突时一并改正 notebook_id。当前写者写不出这种行
        (见 list_sources_page 搜索腿注释第 2 条的写者清单)。见
        test_source_search_ignores_a_legacy_cross_notebook_child_row。

        计划:``notebook_id=%s`` 只作过滤,大库上 custom 与 generic 计划都仍由
        page 级 IN 列表经 pk_source_paper_meta / idx_source_authors_source 驱动,
        与收窄前同一条索引(按 4.9 万源 / 3.9 万 meta / 21 万作者行的规模实测)。
        只有库小到「按库取全部行」比 IN 探针还便宜时,custom 计划才会改走
        idx_source_*_nb —— 取的行数以那个小库的大小为界。见
        test_paper_meta_hydration_plan_stays_on_source_id。"""
        meta_rows: dict[str, Mapping[str, Any]] = {}
        author_rows: dict[str, list[Mapping[str, Any]]] = {}
        ids = list(source_ids)
        for offset in range(0, len(ids), self.IN_CHUNK):
            batch = ids[offset : offset + self.IN_CHUNK]
            marker = placeholders(batch)
            for row in connection.execute(
                f"SELECT * FROM source_paper_meta WHERE source_id IN ({marker}) "
                "AND notebook_id=%s",
                [*batch, notebook_id],
            ).fetchall():
                meta_rows[row["source_id"]] = row
            for row in connection.execute(
                "SELECT source_id,position,name,affiliation FROM source_authors "
                f"WHERE source_id IN ({marker}) AND notebook_id=%s "
                "ORDER BY source_id COLLATE \"C\",position",
                [*batch, notebook_id],
            ).fetchall():
                author_rows.setdefault(row["source_id"], []).append(row)
        return {
            source_id: self._paper_meta_dict(row, author_rows.get(source_id, []))
            for source_id, row in meta_rows.items()
        }

    # 卡片读取与见证批读的每条语句 id 数上限(绑定 id 清单规则:分批的键探针,
    # PostgreSQL 每条不超过 1024 个占位符)。
    _CARD_IN_CHUNK = 1_024

    def paper_cards_for_sources(
        self, connection, source_ids: Sequence[str], *, notebook_id: str
    ) -> dict[str, dict]:
        """按 source_id 批量取论文卡片行,并按子表自身的 ``notebook_id`` 收窄到
        ``notebook_id``(这些 source 自己所在的库),口径同 ``paper_meta_for_sources``。
        每批(<=1024 个 id)一条 SQL,经 ``pk_source_paper_cards`` 驱动。有效性
        (basis / 版本 / 是否仍是论文)由调用方判定;这里只读行。"""
        cards: dict[str, dict] = {}
        ids = list(source_ids)
        for offset in range(0, len(ids), self._CARD_IN_CHUNK):
            batch = ids[offset : offset + self._CARD_IN_CHUNK]
            for row in connection.execute(
                "SELECT * FROM source_paper_cards "
                f"WHERE source_id IN ({placeholders(batch)}) AND notebook_id=%s",
                [*batch, notebook_id],
            ).fetchall():
                cards[row["source_id"]] = paper_card_row(row)
        return cards

    @staticmethod
    def _card_basis_of(row) -> str:
        return utc_iso(None if row is None else row["basis"])

    def upsert_paper_card(
        self,
        source_id: str,
        notebook_id: str,
        *,
        basis: str,
        card_version: int,
        status: str,
        card: dict,
        raw_json: dict,
        model: str,
    ) -> bool:
        """写入/覆盖一张论文卡片,返回是否真的写入。

        锁序与 ``upsert_paper_meta`` 相同:先 notebook 后 source(``FOR KEY SHARE``),
        校验归属;来源已被删则 ``KeyError``。``status`` 只接受 ``ready`` / ``failed``
        (否则 ``ValueError``,不发任何 SQL)。持锁后重读当前见证,传入的 ``basis``
        已过期就拒写(返回 False):在途的旧任务不顶掉新内容上的卡片。**条件写**:
        同 basis 同版本已是 ``ready`` 时,``failed`` 不得覆盖它(返回 False);
        ``failed`` 时 ``attempts`` 在 SQL 里自增,basis 或版本变了则从 1 重新计,
        ``ready`` 写入时清零。"""
        if status not in ("ready", "failed"):
            raise ValueError(f"unknown paper card status: {status!r}")
        now = self.now()
        ready = status == "ready"
        with self.database.write() as connection:
            if connection.execute(
                "SELECT id FROM notebooks WHERE id=%s FOR KEY SHARE", (notebook_id,)
            ).fetchone() is None:
                raise KeyError(notebook_id)
            if connection.execute(
                "SELECT id FROM sources WHERE id=%s AND notebook_id=%s FOR KEY SHARE",
                (source_id, notebook_id),
            ).fetchone() is None:
                raise KeyError(source_id)
            current = connection.execute(
                "SELECT MAX(created_at) AS basis FROM source_elements WHERE source_id=%s",
                (source_id,),
            ).fetchone()
            if self._card_basis_of(current) != basis:
                return False
            cursor = connection.execute(
                "INSERT INTO source_paper_cards"
                "(source_id,notebook_id,basis,card_version,status,card,raw_json,model,"
                "attempts,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(source_id) DO UPDATE SET "
                "notebook_id=excluded.notebook_id,basis=excluded.basis,"
                "card_version=excluded.card_version,status=excluded.status,"
                "card=excluded.card,raw_json=excluded.raw_json,model=excluded.model,"
                + (
                    "attempts=0,"
                    if ready
                    else "attempts=CASE WHEN source_paper_cards.basis=excluded.basis"
                    " AND source_paper_cards.card_version=excluded.card_version"
                    " THEN source_paper_cards.attempts+1 ELSE 1 END,"
                )
                + "updated_at=excluded.updated_at"
                + (
                    ""
                    if ready
                    else " WHERE NOT (source_paper_cards.status='ready'"
                    " AND source_paper_cards.basis=excluded.basis"
                    " AND source_paper_cards.card_version=excluded.card_version)"
                ),
                (
                    source_id,
                    notebook_id,
                    basis,
                    int(card_version),
                    status,
                    jsonb(card if ready else {}),
                    jsonb(raw_json or {}),
                    str(model or ""),
                    0 if ready else 1,
                    now,
                    now,
                ),
            )
            return int(cursor.rowcount or 0) > 0

    def source_card_basis(self, source_id: str) -> str:
        """论文卡片的 ``basis``:来源元素的生成见证
        (``MAX(source_elements.created_at)``),渲染成 UTC ISO 字符串,与 PG 会话时区
        无关——basis 要存要比,按会话时区渲染会在时区变化时让全部卡片失效。无元素
        给 ``""``。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT MAX(created_at) AS basis FROM source_elements WHERE source_id=%s",
                (source_id,),
            ).fetchone()
        return self._card_basis_of(row)

    def source_card_bases(self, connection, source_ids: Sequence[str]) -> dict[str, str]:
        """``source_card_basis`` 的批量版,在调用方的连接上:每批(<=1024 个 id)一条
        ``GROUP BY source_id``,经 ``idx_source_elements_source_type`` 的前缀走。没有
        元素的来源不在结果里(调用方按 ``""`` 处理)。"""
        bases: dict[str, str] = {}
        ids = list(source_ids)
        for offset in range(0, len(ids), self._CARD_IN_CHUNK):
            batch = ids[offset : offset + self._CARD_IN_CHUNK]
            for row in connection.execute(
                "SELECT source_id, MAX(created_at) AS basis FROM source_elements "
                f"WHERE source_id IN ({placeholders(batch)}) GROUP BY source_id",
                batch,
            ).fetchall():
                bases[row["source_id"]] = self._card_basis_of(row)
        return bases

    def sources_missing_paper_meta(
        self, notebook_id: str, include_existing: bool = False
    ) -> list[str]:
        missing = "" if include_existing else PAPER_META_NO_META_SQL
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT s.id FROM sources s WHERE s.notebook_id=%s"
                + PAPER_META_ELIGIBLE_SQL
                + missing
                + " ORDER BY s.created_at,s.id COLLATE \"C\"",
                (notebook_id,),
            ).fetchall()
        return [row["id"] for row in rows]
