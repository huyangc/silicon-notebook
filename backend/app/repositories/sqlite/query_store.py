from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from app.core.config import Settings, get_settings
from app.repositories.sqlite import access_sql
from app.models.notebooks import NotebookAnalytics
from app.models.ask import (
    SEARCH_HIT_CAP,
    NotebookSearchResponse,
    SearchHit,
)
from app.core.activity_time import (
    UNRESOLVED_INSTANT_ISO,
    cursor_instant_text,
    parse_activity_instant,
)
from app.models.sources import (
    extraction_warning_text,
    has_pdf_python_fallback_warning,
    paper_meta_status,
)
from app.repositories.group_rows import SHARED_TO_GROUPS_COLUMN
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.identity_store import (
    _UPLOAD_LIMIT_DEFAULT_KEY,
    _resolve_global_default,
)
from app.repositories.sqlite.mount_sql import MOUNT_JOIN, MOUNT_ORDER, MOUNT_VALID
from app.repositories.sqlite.source_store import (
    MEMORY_SOURCE_TYPE_PREDICATE,
    PAPER_META_ELIGIBLE_SQL,
    PAPER_META_NO_META_SQL,
    VISIBLE_SOURCE_TYPES_PREDICATE,
)
from app.domain.extraction_profiles import OBJECT_TYPE_LABELS
from app.domain.knowledge_contracts import USABLE_STATUSES
from app.domain.notebook_scale import NotebookScaleFacts


def _snippet(text: str, needle: str) -> str:
    clean = " ".join(text.split())
    lower = clean.lower()
    index = lower.find(needle)
    if index < 0:
        return clean[:180]
    start = max(0, index - 48)
    end = min(len(clean), index + len(needle) + 120)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(clean) else ""
    return f"{prefix}{clean[start:end]}{suffix}"


def _absolute_instant(column: str) -> str:
    """SQLite 侧「绝对时刻」的比较形态,用在 ORDER BY / 范围 / 游标三处。

    ``created_at`` 是文本列,而这些表里的值必然是**混合格式**:历史行是裸 naive
    ("2026-07-17T15:19:42"),``seams.now`` 现在写的却带本机 UTC offset
    ("2026-08-04T10:00:00.123456+08:00")。裸文本比较会把 ``+08:00`` 的 10:00
    (=02:00Z)排在 ``+00:00`` 的 03:30 之前——顺序是反的,而 keyset 游标按这个错序
    推进,翻页会跳行。``julianday()`` 把两种写法都折成同一根绝对时间轴(与
    ``ask_state_store`` 的会话历史排序同一条判据)。

    ``COALESCE(...)`` 兜住不可解析的历史脏值(``created_at`` 的 DDL 是
    ``TEXT NOT NULL DEFAULT ''``,空串正是这类):``julianday()`` 对它返回 NULL,而
    Python 归并键里混进 None 会直接 TypeError。兜底值刻意**不是** ``0.0`` 而是
    ``julianday(UNRESOLVED_INSTANT_ISO)``:两者都把这类行排到最末,但只有后者写得成
    ISO 串,游标才回传得回来(见 activity_time.UNRESOLVED_INSTANT_ISO 的说明——用
    ``0.0`` 时 ``next_cursor`` 会原样发出一个解析不了的 ``''``,下一页直接抛 ValueError)。

    ⚠ 性能:这是列上的函数,现有索引(``idx_sources_notebook_created`` 等)都用不上。
    实测(10 万行 sources、SQLite 3.51)单笔记本 5.7ms vs 裸列 0.0ms。**但**换成裸列
    并不能同时保住正确性:``created_at`` 是混合格式文本,裸文本序不是绝对时刻序。而且
    实测「全部笔记本」那条默认路径(``notebook_id IN (...)``,n>1)裸列写法**同样**
    落到 ``SCAN`` + 全量 TEMP B-TREE(18.6ms vs 本写法 20.1ms),换过去一分钱都不省。
    两者可以兼得,但要的是一条**匹配这个表达式的索引**(SQLite 支持表达式索引,实测
    planner 会用它消掉 TEMP B-TREE)——那是一次 schema 迁移,不在本轮缝隙整改范围内。
    """
    return f"COALESCE(julianday({column}), julianday('{UNRESOLVED_INSTANT_ISO}'))"


#: 「这一行知识对象**不**属于任何私有 Memory 合成来源」,写成语句内的相关子查询。
#: 约定被排除的那张表在外层用别名 ``o``(两个消费方都是 ``knowledge_objects o``)。
#:
#: 这是 codex #520 R2 P1 的落点:底座聚合此前先读一次 ``memory_source_ids``、再用
#: 相减(计数)或排除清单(概念名)兑现排除。两次读之间没有共享快照——PG 的
#: READ COMMITTED 下尤其明显——所以在两者之间新建/删除一个私有 Memory,就会让相减
#: 漏减、让排除清单漏项;后一种漏项泄漏的是**概念名称本身**,而它会被写进全体成员
#: 都读得到的共享块。压进同一条语句之后,排除与被排除的行由同一次求值决定。
#:
#: ``knowledge_objects.source_id`` 默认是 ``''`` 且不是外键,所以「没有归属来源」的
#: 对象匹配不到任何 ``sources`` 行、照旧保留——与旧的 ``NOT IN (memory ids)`` 逐字
#: 同义。``sources.id`` 是主键,子查询是一次点查。
_NOT_MEMORY_OWNED_SQL = (
    "NOT EXISTS (SELECT 1 FROM sources "
    f"WHERE sources.id = o.source_id AND {MEMORY_SOURCE_TYPE_PREDICATE})"
)

#: codex #520 R8 P1:``concept_clusters.canonical_name`` 是**代表名整簇复制**的,
#: 代表可能恰好选自某位成员私有 Memory 派生的对象——只过滤成员行(上面那条)洗不
#: 掉名字本身。所以取名字的查询按**整簇**排除:簇内只要有一个成员归 Memory 源所
#: 有,整簇不出名字(约定外层别名 ``c``,列 ``notebook_id``/``canonical_id``)。
#: 宁可少一个也在可见文档里出现的名字,不冒把私有 Memory 的措辞写进全员可见块的
#: 险。计数查询不受此累——计数不携带名字,成员行过滤就够了。
#: ``source_type`` 不加限定词与上一条同理:三张 join 表里只有 sources(ms) 有这列,
#: 只能解析到它——这样才能逐字复用同一份谓词常量。
_NO_MEMORY_MEMBER_CLUSTER_SQL = (
    "NOT EXISTS (SELECT 1 FROM concept_clusters mc "
    "JOIN knowledge_objects mo ON mo.id = mc.member_object_id "
    "JOIN sources ms ON ms.id = mo.source_id "
    "WHERE mc.notebook_id = c.notebook_id "
    "AND mc.canonical_id = c.canonical_id "
    f"AND {MEMORY_SOURCE_TYPE_PREDICATE})"
)


class QueryStore:
    def __init__(
        self, database: SqliteDatabase, settings: "Settings | None" = None
    ) -> None:
        self.database = database
        # 注入的 settings 供 list_user_usage 解析全局文档上限默认的 config 回退。
        # runtime 构造时总会传入(见 repository_runtime);唯一 settings=None 的构造点
        # 是 NotebookCatalogService 的备用 QueryStore(它从不调 list_user_usage),
        # 那条路径下再兜底到 get_settings()。
        self.settings = settings

    def warm_open_path_caches(self, progress=None) -> int:
        from app.repositories.sqlite import knowledge_counts_cache

        with self.database.connect() as db:
            return knowledge_counts_cache.warm_all(db, progress)

    def invalidate_knowledge_counts(self, notebook_id: str) -> None:
        from app.repositories.sqlite import knowledge_counts_cache

        knowledge_counts_cache.invalidate(notebook_id)

    # NotebookSummary projection primitives.  The caller deliberately retains
    # the connection so one summary is hydrated from one read snapshot.
    @staticmethod
    def count_rows(
        db: sqlite3.Connection, table: str, column: str, value: str
    ) -> int:
        row = db.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE {column} = ?", (value,)
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def knowledge_type_count_rows(
        db: sqlite3.Connection, notebook_id: str, statuses: tuple[str, ...]
    ) -> "list[dict]":
        # Served from the seq-gated count cache (one GROUP BY per kg_mutation_seq
        # instead of per open/list/poll). Returns row-like dicts with the same
        # ["object_type"] / ["c"] keys the callers read.
        from app.repositories.sqlite import knowledge_counts_cache
        counts = knowledge_counts_cache.type_counts(db, notebook_id, statuses)
        return [{"object_type": ot, "c": c} for ot, c in counts.items()]

    # Bound parameters per batch, leaving room for the notebook id and the
    # status placeholders under SQLite's default variable limit.
    _SOURCE_COUNT_IN_CHUNK = 512

    @classmethod
    def knowledge_type_count_rows_for_sources(
        cls,
        db: sqlite3.Connection,
        notebook_id: str,
        source_ids: "list[str]",
        statuses: tuple[str, ...],
    ) -> "list[dict]":
        """``[{object_type, c}]`` restricted to objects owned by the GIVEN
        sources — the enumeration denominator's subtrahend.

        Deliberately generic ("count these sources' objects by type") rather
        than "count the Memory objects": the caller already owns the one
        definition of which sources are Memory
        (``SourceStore.memory_source_ids``), and re-spelling that predicate
        here would give the exclusion a second, drift-prone home.

        Bounded and index-assisted: ``idx_knowledge_objects_source``
        (source_id) turns this into one seek per id, and the id list is the
        notebook's Memory count.  NOT served from
        ``knowledge_counts_cache``: that memo holds the whole notebook's
        ``(type, status)`` breakdown and knows nothing about sources, and it is
        the board's counting path — widening it for one enumeration subtrahend
        would put the exclusion into every count in the product.
        """
        ids = list(dict.fromkeys(source_id for source_id in source_ids if source_id))
        if not ids or not statuses:
            return []
        status_marks = ",".join("?" for _ in statuses)
        totals: "dict[str, int]" = {}
        for offset in range(0, len(ids), cls._SOURCE_COUNT_IN_CHUNK):
            batch = ids[offset:offset + cls._SOURCE_COUNT_IN_CHUNK]
            id_marks = ",".join("?" for _ in batch)
            for row in db.execute(
                "SELECT object_type, COUNT(*) AS c FROM knowledge_objects "
                f"WHERE notebook_id = ? AND source_id IN ({id_marks}) "
                f"AND status IN ({status_marks}) GROUP BY object_type",
                (notebook_id, *batch, *statuses),
            ).fetchall():
                totals[row["object_type"]] = (
                    totals.get(row["object_type"], 0) + int(row["c"])
                )
        return [{"object_type": ot, "c": c} for ot, c in totals.items()]

    @staticmethod
    def knowledge_type_count_rows_excluding_memory(
        db: sqlite3.Connection, notebook_id: str, statuses: tuple[str, ...]
    ) -> "list[dict]":
        """``[{object_type, c}]`` for the notebook, MINUS the objects a private
        Memory owns — the exclusion evaluated inside this one statement.

        Deliberately NOT ``knowledge_type_count_rows`` minus
        ``knowledge_type_count_rows_for_sources(memory ids)``: that arithmetic
        spans three reads with no shared snapshot, so a Memory created or
        deleted between them makes the subtrahend describe a different library
        than the minuend, and the shared base then carries a count derived from
        one member's private Memory (codex #520 R2 P1).  The generic
        ``…_for_sources`` variant stays for the enumeration denominator, whose
        caller genuinely holds an arbitrary id list.

        NOT served from ``knowledge_counts_cache`` for the same reason that one
        is not: the memo holds the whole notebook's ``(type, status)``
        breakdown and knows nothing about sources, and it is the board's
        counting path — the board answers "how much knowledge is in this
        library", which is a different question from this one.

        Empty ``statuses`` returns nothing rather than "all statuses", matching
        every other counting query here.
        """
        allowed = tuple(statuses)
        if not allowed:
            return []
        status_marks = ",".join("?" for _ in allowed)
        rows = db.execute(
            "SELECT o.object_type AS object_type, COUNT(*) AS c "
            "FROM knowledge_objects o "
            f"WHERE o.notebook_id = ? AND o.status IN ({status_marks}) "
            f"AND {_NOT_MEMORY_OWNED_SQL} "
            "GROUP BY o.object_type",
            (notebook_id, *allowed),
        ).fetchall()
        return [
            {"object_type": str(row["object_type"]), "c": int(row["c"] or 0)}
            for row in rows
        ]

    @staticmethod
    def top_concept_names(
        db: sqlite3.Connection,
        notebook_id: str,
        statuses: tuple[str, ...],
        limit: int,
    ) -> "list[tuple[str, int]]":
        """``[(canonical concept name, members)]``, most-supported first.

        Same shape as ``UnifiedKgStore.largest_clusters`` (group the notebook's
        concept cluster rows, probe each member into ``knowledge_objects``,
        order by member count) plus one thing that one does not have: private
        Memory's objects are excluded, so a concept that exists only inside one
        member's Memory cannot reach a notebook-shared prompt block.  That is
        the reason this query exists next to ``largest_clusters`` at all.

        ⚠ The exclusion is a predicate INSIDE this statement, not an id list
        the caller passes in (codex #520 R2 P1).  A caller-supplied list is
        read at a different instant than this query runs, and the window
        between them leaks CONCEPT NAMES — the one input here that is not a
        count — into a block every member of a shared notebook reads.

        ⚠ Cost class is ``largest_clusters``' own: the LIMIT truncates output,
        not the scan.  Background-chain use only; see the port docstring.

        Empty ``statuses`` returns nothing rather than "all statuses": every
        caller means "the usable ones", and a silent widening here would put
        deprecated objects into an agent's understanding.
        """
        allowed = tuple(statuses)
        if not allowed or limit <= 0:
            return []
        status_marks = ",".join("?" for _ in allowed)
        rows = db.execute(
            "SELECT COALESCE(MIN(NULLIF(c.canonical_name, '')), '') AS name, "
            "       COUNT(o.id) AS members "
            "FROM concept_clusters c "
            "JOIN knowledge_objects o ON o.id = c.member_object_id "
            "     AND o.notebook_id = c.notebook_id "
            f"     AND o.status IN ({status_marks}) "
            f"     AND {_NOT_MEMORY_OWNED_SQL} "
            "WHERE c.notebook_id = ? AND c.object_type = 'concept' "
            f"AND {_NO_MEMORY_MEMBER_CLUSTER_SQL} "
            "GROUP BY c.canonical_id "
            "HAVING name <> '' "
            "ORDER BY members DESC, name ASC LIMIT ?",
            (*allowed, notebook_id, int(limit)),
        ).fetchall()
        return [(str(row["name"]), int(row["members"])) for row in rows]

    @staticmethod
    def knowhow_knowledge_type_rows(
        db: sqlite3.Connection, notebook_id: str, statuses: tuple[str, ...]
    ) -> "list[dict]":
        """Return only dynamic KO types owned by hidden Knowhow sources.

        Custom schema types are not necessarily Knowhow columns. Scoping through
        ``knowledge_objects.source_id -> knowhow_tables.hidden_source_id`` keeps
        default-on Ask retrieval from widening to unrelated user-defined types.
        Starting from the notebook's few tables also avoids a cold GROUP BY over
        every usable KG object; both join sides use existing indexes.
        """
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        return db.execute(
            "SELECT ko.object_type, COUNT(*) AS c "
            "FROM knowhow_tables kt JOIN knowledge_objects ko "
            "ON ko.source_id=kt.hidden_source_id "
            "WHERE kt.notebook_id=? "
            "AND ko.notebook_id=? "
            f"AND ko.status IN ({placeholders}) GROUP BY ko.object_type "
            "ORDER BY ko.object_type",
            (notebook_id, notebook_id, *statuses),
        ).fetchall()

    @staticmethod
    def notebook_has_kg(db: sqlite3.Connection, notebook_id: str) -> bool:
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id = ?)",
            (notebook_id,),
        ).fetchone()
        return bool(row[0])

    @staticmethod
    def notebook_has_usable_kg(db: sqlite3.Connection, notebook_id: str) -> bool:
        """本库是否有**可用状态**的 knowledge_objects(USABLE_STATUSES,排除 deprecated
        等)。检索侧就按 USABLE_STATUSES 过滤,故 ask_available 用它,而非 notebook_has_kg
        (后者含 deprecated)——避免只剩已弃用 KG 的库被误判为可对话(codex PR#334 第4轮
        P2-1)。注意 notebook_has_kg 仍保留:kg_ready 是"是否建过图"的既有全应用口径。"""
        placeholders = ",".join("?" for _ in USABLE_STATUSES)
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM knowledge_objects "
            f"WHERE notebook_id = ? AND status IN ({placeholders}))",
            (notebook_id, *USABLE_STATUSES),
        ).fetchone()
        return bool(row[0])

    @staticmethod
    def notebook_has_usable_base_kg(
        db: sqlite3.Connection, notebook_id: str
    ) -> bool:
        """本库挂载的参考库中是否有任一含**可用状态** KG。对齐检索口径:base_kg_available
        (mounted_bases_row 的 has_kg)含 deprecated,ask_available 改用这个 usable 版
        (codex PR#334 第4轮 P2-1)。"""
        placeholders = ",".join("?" for _ in USABLE_STATUSES)
        row = db.execute(
            "SELECT EXISTS(SELECT 1 " + MOUNT_JOIN + MOUNT_VALID
            + " AND EXISTS(SELECT 1 FROM knowledge_objects ko "
            f"WHERE ko.notebook_id = b.id AND ko.status IN ({placeholders})))",
            (notebook_id, *USABLE_STATUSES),
        ).fetchone()
        return bool(row[0])

    @staticmethod
    def notebook_has_chunk(db: sqlite3.Connection, notebook_id: str) -> bool:
        """本库是否有任意 chunk。chunks 表**不按 source_type 区分**——文档 chunk 与
        knowhow 格子 chunk 同表,是默认 chunk 模式实际能检索到的证据全集。故这是
        NotebookSummary.ask_available 的关键信号:visible_source_count 排除掉的
        knowhow-only 库(尤其无锚点列、零 knowledge_objects 的表)在这里为真——它没有
        可见来源、没有 KG,却仍可对话。"""
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM chunks WHERE notebook_id = ?)",
            (notebook_id,),
        ).fetchone()
        return bool(row[0])

    @staticmethod
    def notebook_paper_meta_missing(
        db: sqlite3.Connection, notebook_id: str
    ) -> bool:
        """本库是否存在缺论文元数据的合规候选源(NotebookSummary.paper_meta_missing,
        「补全论文信息」按钮的显示门)。谓词与 sources_missing_paper_meta /
        notebook_analytics 的 missing 计数共用 source_store 的 PAPER_META_*_SQL
        常量;EXISTS 短路,走 idx_sources_nb_parse_status_type。"""
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM sources s "
            "WHERE s.notebook_id = ?"
            f"{PAPER_META_ELIGIBLE_SQL}{PAPER_META_NO_META_SQL})",
            (notebook_id,),
        ).fetchone()
        return bool(row[0])

    @staticmethod
    def notebook_has_confirmed_memory(
        db: sqlite3.Connection, notebook_id: str, user_id: str
    ) -> bool:
        """本库对该用户是否有**已确认** memory。只有 confirmed 会被 ask 注入为证据
        (candidate 不算),且 memory 按 created_by 私有——检索侧就是按当前用户取 confirmed
        memory,故此处同样按 user_id 限定(与 counts["memories"] 的 all-status 计数口径
        不同,是有意的)。用于 NotebookSummary.ask_available:无来源/无 KG 但有 confirmed
        memory 的库仍可对话。"""
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM memory_items "
            "WHERE notebook_id = ? AND created_by = ? AND status = 'confirmed')",
            (notebook_id, user_id),
        ).fetchone()
        return bool(row[0])

    @staticmethod
    def pending_kg_source_count(db: sqlite3.Connection, notebook_id: str) -> int:
        # Served from the seq-gated count cache (one correlated scan per
        # kg_mutation_seq instead of ~2s per open at 48k sources).
        from app.repositories.sqlite import knowledge_counts_cache
        return knowledge_counts_cache.pending_source_count(db, notebook_id)

    @staticmethod
    def visible_pending_kg_source_count(
        db: sqlite3.Connection, notebook_id: str
    ) -> int:
        from app.repositories.sqlite import knowledge_counts_cache

        return knowledge_counts_cache.visible_pending_source_count(db, notebook_id)

    @staticmethod
    def visible_source_count(db: sqlite3.Connection, notebook_id: str) -> int:
        """NotebookSummary's user-facing source count — excludes Memory-derived
        AND knowhow-table hidden synthetic sources (source_type IN ('memory',
        'knowhow')): both are internal derivation links with no independent
        user-visible content, which would otherwise double-count next to the
        Memory panel's own count / inflate the count past what list_sources
        shows (SourceStore.list_sources carries the SAME exclusion — see its
        docstring). Internal paths (pending_kg_source_count above, copy
        materialization, scale-index scans) keep counting the true full set
        and must NOT reuse this method."""
        row = db.execute(
            "SELECT COUNT(*) AS count FROM sources "
            "WHERE notebook_id = ? AND source_type NOT IN ('memory', 'knowhow')",
            (notebook_id,),
        ).fetchone()
        return int(row["count"])

    @classmethod
    def notebook_source_ids_among(
        cls, db: sqlite3.Connection, notebook_id: str, source_ids
    ) -> set:
        """给定的 source id 里,**属于本 notebook** 的那一批(不存在的 id 自然落选)。

        用途:把**进程全局**的活跃租约快照(``source_ingestion._active_sources``,跨所有
        notebook 共用一个 dict)收窄成「本库的那几个」。CheckupService 的 H4/H5 memo 拿它当
        缓存键——键若用全局快照,别的库上传一个文件就会把每个库的缓存都冲掉,而本库补齐 job
        逐源推进时键每轮都变、命中率≈0。

        代价:``active`` 是个位数~并发度量级的集合,这条按主键 ``sources.id`` 逐 id seek,
        比「两条全表 anti-join」便宜几个数量级——它存在的意义就是让那两条别跑。
        参数按 ``_SOURCE_COUNT_IN_CHUNK`` 分批,避开 SQLite 的绑定变量上限。

        ⚠ ``notebook_id`` 的比较**刻意放在 Python 里**、不写进 WHERE:本仓库从不对生产库跑
        ``ANALYZE``,``WHERE notebook_id=? AND id IN (...)`` 会被 planner 选成
        ``idx_sources_notebook_file_hash (notebook_id=?)``,即扫遍该 notebook 的**每一行**
        sources 去找这两三个 id(EXPLAIN 实测,谓词前后调换也一样);裸 ``id IN (...)`` 在有无
        统计信息下都走 ``sqlite_autoindex_sources_1 (id=?)`` 主键 seek。判据完全等价——
        比较的是同一列同一个值,只是求值位置从 SQL 挪到了调用方,而结果集本就被 ``ids``
        的长度封死(不存在「读回一大堆再过滤」的风险)。两后端同款拼写。"""
        ids = list(dict.fromkeys(sid for sid in source_ids if sid))
        if not ids:
            return set()
        found: set = set()
        for offset in range(0, len(ids), cls._SOURCE_COUNT_IN_CHUNK):
            batch = ids[offset:offset + cls._SOURCE_COUNT_IN_CHUNK]
            marks = ",".join("?" for _ in batch)
            found.update(
                row["id"] for row in db.execute(
                    f"SELECT id, notebook_id FROM sources WHERE id IN ({marks})",
                    tuple(batch),
                ).fetchall()
                if row["notebook_id"] == notebook_id
            )
        return found

    @staticmethod
    def sources_missing_chunks(db: sqlite3.Connection, notebook_id: str) -> set:
        """H3(缺分块)的 SQL 候选集:本 notebook 下**有 elements、却没有分块完成标记**
        (`chunked_at IS NULL`)的用户源 source_id 集合。P1.5 的 chunked_at 是世代感知的
        完成标记——分块成功(含纯标题 0-chunk)才置位、失败留 NULL,故 NULL 即「本代
        elements 没成功走完分块」的损坏信号(承 source-completion-marker 设计)。

        ⚠ **排除 source_type IN ('memory','knowhow')**:这些隐藏合成源不走
        build_chunks_for_source 的置位路径(knowhow 投影器复用 replace_source_chunks 时
        **不传** mark_chunked_at,见 P1.5 收 codex 第 1 轮),chunked_at 恒为 NULL——含进来
        会把每个隐藏源误报缺分块。H3 本就是用户可见体检项,只看 list_sources 口径的用户源。

        返回的是**候选集**:调用方(CheckupService)还要减去内存活跃租约快照
        (在途 reparse 的源瞬时 chunked_at=NULL 属正常、非损坏),减法在 service 层做——
        store 只出 SQL 判据,不碰进程内运行态。"""
        return {
            row["id"] for row in db.execute(
                "SELECT s.id AS id FROM sources s "
                "WHERE s.notebook_id = ? "
                "AND s.source_type NOT IN ('memory', 'knowhow') "
                "AND s.chunked_at IS NULL "
                "AND EXISTS (SELECT 1 FROM source_elements e WHERE e.source_id = s.id)",
                (notebook_id,),
            ).fetchall()
        }

    @staticmethod
    def sources_without_elements(db: sqlite3.Connection, notebook_id: str) -> set:
        """H2(空源)的 SQL 候选集:本 notebook 下**解析已终态、却没有任何 source_elements**
        的用户源 source_id 集合——「流水线声称处理过、却没落下任何解析产物」的损坏信号
        (承 pipeline-damage-recovery 设计「二·体检层」H2,是 knowledge_store.sources_with_elements
        的对称取反)。

        判据三条:
        - `source_type NOT IN ('memory','knowhow')`:与 H3 / list_sources 同口径,只看用户可见源;
          隐藏合成源(Memory 派生 / knowhow 投影)本就不走文档解析,无 elements 是常态、非损坏。
        - `parse_status IN ('parsed','extracting','extracted')`:**白名单**——只有「终态且本应已成功
          落下解析产物」的源,无 elements 才是「解析未落地」的损坏。刻意用白名单而非黑名单(承 P1.5
          回填同款状态集):它精确排除了所有「无 elements 属正常」的态——`queued`/`parsing`(在途,
          瞬时无 elements)、`metadata-only`(仅导入元数据、内容待用户上传,`source_ingestion.py:315`
          按设计无 elements,黑名单会把它误报成损坏+建议 reparse,而正确动作是「上传文件」)、
          `failed`(解析已明确失败、错误已呈现给用户,不是「静默空源」,已由错误态面对用户,不该再
          被体检当损坏复报)。H2 抓的是**看着成功、却静默没落 elements** 的那种损坏。
        - `NOT EXISTS source_elements`:无任何解析产物。走 idx_source_elements_source(source_id)。

        返回的是**候选集**:调用方(CheckupService)还要减去内存活跃租约快照(在途解析的源瞬时
        无 elements 属正常、非损坏),减法在 service 层做——store 只出 SQL 判据,不碰进程内运行态
        (与 sources_missing_chunks 同一分工)。走 idx_sources_nb_parse_status_type
        (notebook_id, parse_status, source_type)。"""
        return {
            row["id"] for row in db.execute(
                "SELECT s.id AS id FROM sources s "
                "WHERE s.notebook_id = ? "
                "AND s.source_type NOT IN ('memory', 'knowhow') "
                "AND s.parse_status IN ('parsed', 'extracting', 'extracted') "
                "AND NOT EXISTS (SELECT 1 FROM source_elements e WHERE e.source_id = s.id)",
                (notebook_id,),
            ).fetchall()
        }

    @staticmethod
    def mounted_bases_row(db: sqlite3.Connection, notebook_id: str):
        """本库挂载的有效参考库 + 各自是否有 KG —— 一次查询同时供 NotebookSummary 的
        base_notebooks 与 base_kg_available。"""
        return db.execute(
            "SELECT b.id AS id, b.name AS name, b.tier AS tier, "
            "EXISTS(SELECT 1 FROM knowledge_objects ko WHERE ko.notebook_id = b.id) AS has_kg "
            + MOUNT_JOIN + MOUNT_VALID + MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def summary_notebook_row(db: sqlite3.Connection, notebook_id: str):
        """单库详情的行。

        `_owner_username` 与列表侧 `joined_notebook_rows` / `granted_notebook_rows`
        的同名列同义,喂 `NotebookSummary.shared_from`(P1-T4 修:详情投影此前**从不**
        回填 access/shared_from,reader 顶栏整段是死代码)。用 LEFT JOIN 而不是另发
        一次点查:这条路径是「打开笔记本」的现场,多一次往返比多一个 join 贵得多。
        """
        # ⚠ `notebooks` 刻意不起别名:`SHARED_TO_GROUPS_COLUMN` 的关联子查询写的是
        # `notebooks.id`,而 SQLite/PG 一旦给表起了别名,原表名就不再可用。
        return db.execute(
            "SELECT notebooks.*, u.username AS _owner_username, "
            "COALESCE(ip.indexing_pipeline_id,'') AS _published_pipeline_id,"
            "COALESCE(ip.indexing_pipeline_version,'builtin.chunk.v1') "
            "AS _published_pipeline_version, "
            + SHARED_TO_GROUPS_COLUMN
            + " FROM notebooks LEFT JOIN users u ON u.id = notebooks.created_by "
            "LEFT JOIN unified_kg_state ip ON ip.notebook_id=notebooks.id "
            "WHERE notebooks.id = ? AND notebooks.status != 'copying'",
            (notebook_id,),
        ).fetchone()

    @staticmethod
    def owned_notebook_rows(db: sqlite3.Connection, user_id: str):
        return db.execute(
            "SELECT notebooks.*,"
            "COALESCE(ip.indexing_pipeline_id,'') AS _published_pipeline_id,"
            "COALESCE(ip.indexing_pipeline_version,'builtin.chunk.v1') "
            "AS _published_pipeline_version, " + SHARED_TO_GROUPS_COLUMN
            + " FROM notebooks LEFT JOIN unified_kg_state ip "
            "ON ip.notebook_id=notebooks.id "
            "WHERE created_by = ? AND status != 'copying' "
            "ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()

    @staticmethod
    def joined_notebook_rows(
        db: sqlite3.Connection, user_id: str, notebook_id: "str | None" = None
    ):
        """经只读共享(`notebook_members`)加入的笔记本。

        传 `notebook_id` 就是点查形态(P1-T4:单库详情要判「我在不在这本库的成员行
        里」)。与 `granted_notebook_rows` 同一个理由做成参数而不是另写一条:去重口径
        是**成员行优先**,详情与列表必须用同一条谓词判「有没有成员行」,分叉了会让同
        一本库在卡片上和打开之后是两副面孔。
        """
        point_filter = "" if notebook_id is None else "AND m.notebook_id = ? "
        params = (user_id,) if notebook_id is None else (user_id, notebook_id)
        return db.execute(
            "SELECT nb.*, u.username AS _owner_username,"
            "COALESCE(ip.indexing_pipeline_id,'') AS _published_pipeline_id,"
            "COALESCE(ip.indexing_pipeline_version,'builtin.chunk.v1') "
            "AS _published_pipeline_version FROM notebook_members m "
            "JOIN notebooks nb ON nb.id = m.notebook_id "
            "LEFT JOIN users u ON u.id = nb.created_by "
            "LEFT JOIN unified_kg_state ip ON ip.notebook_id=nb.id "
            "WHERE m.user_id = ? " + point_filter
            + "AND nb.status != 'copying' "
            "ORDER BY m.added_at ASC",
            params,
        ).fetchall()

    @staticmethod
    def granted_notebook_rows(
        db: sqlite3.Connection, user_id: str, notebook_id: "str | None" = None
    ):
        """「经群组授权边可读」的笔记本列表投影(群组知识共享 P1-T3)。

        ⚠ 传 `notebook_id` 就是同一条查询的**点查**形态(P1-T4:单库详情要回填
        `granted_via`)。刻意做成参数而不是另写一条:两处的谓词——两个群组主体、
        `group_admins` 只到组管理员、排除 copying——必须逐字相同,而这份谓词分叉了
        没有任何测试会自然抓到(它不报错,只是详情页与列表页对同一本库给出不同的
        来源标注)。

        与 `joined_notebook_rows` 是**同一类**东西,理由也一样(见
        `access_sql.py` 模块 docstring 的「刻意不收口」):它不是「我能不能读这个
        notebook」的授权判定,而是一条**列表**查询——所以它不复用读权谓词,而且刻意
        只覆盖两个**群组**主体:
        * `user` 主体的边在 P1 里还没有任何写入口(只读共享仍走 `notebook_members`,
          由 `joined_notebook_rows` 那条负责),收进来只会凭空多一条空路径;
        * `everyone` 主体沿用 `tier='base'` 的既有隐藏惯例——公共知识库不进常规
          列表,只在挂载选择器出现。把它收进来会让每个人的笔记本列表突然多出全部
          公共库。
        自有库与只读成员库的去重留给 service 层按 id 做(成员行优先),这里不写
        `NOT EXISTS`:两处判据只该有一份,而 service 手上本来就有已产出的 id 集合。

        每行是「一本笔记本 × 一个把它共享给我的群组」,组信息带 `_group_*` 前缀随行
        返回,由 service 折成 `granted_via`。`group_admins` 那条边只对组内 role='admin'
        的人生效——这一条与读权谓词同义,但这里是 JOIN 出来的,不是那份四值白名单的
        复刻(它一次只看两个群组主体,永远碰不到 `everyone` 与哨兵停车行)。

        ⚠ `_grant_role` 是**这条边自己的** `notebook_grants.role`(P2-T2 新增的一列,
        零新增查询),不要与上一段里 `gm.role='admin'` 那个**组成员角色**混为一谈:
        前者答「这条共享边给的是只读还是管理」,后者答「你在这个组里是不是管理员」,
        `group_admins` 主体的边两者都要为真。service 用它算 `can_manage_content`。

        **登记的取舍**:这条查询只覆盖 `group` / `group_admins` 两个主体,所以由它
        派生的 `can_manage_content` 也只覆盖这两类边;而后端的管理权谓词
        (`access_sql.NOTEBOOK_ADMIN_SQL`)是四臂全收。两者今天等价,因为
        `models/groups.py::GRANTABLE_PRINCIPAL_TYPES` 只收这两个主体——`user` /
        `everyone` 的授权边在产品里根本没有写入口,更不可能带 `role='admin'`。这条
        前提由 `test_group_routes.py` 的 grantable-principal 守卫钉住;真要给
        `user` 主体开写入口,那道守卫会先红,提醒把这条投影一并扩掉。方向也是安全的:
        投影漏判只会少画按钮,不会多给权限(权威判定永远在守卫那一侧)。

        ⚠ **`CROSS JOIN` 是给 SQLite 查询规划器的驱动顺序提示,不是语义**(SQLite 对
        `CROSS JOIN ... ON` 与 `JOIN ... ON` 的结果集完全相同,区别只是前者禁止重排
        表顺序)。少了它,planner 会从 `notebook_grants` 起步、按
        `idx_notebook_grants_principal (principal_type=?)` **单列**扫掉全库所有群组
        授权边,再逐行探组成员——代价随**整个部署的共享边总数**增长,而这条查询挂在
        每次打开笔记本列表的路径上。钉死从 `group_members (user_id=?)` 起步之后,
        同一条索引变成 `(principal_type=?, principal_id=?)` 的双列点查,代价只随
        **该用户加入的组数**增长(EXPLAIN QUERY PLAN 实测)。PG 侧刻意**不**照抄:
        标准 SQL 的 `CROSS JOIN` 不带 `ON`,而 PG 有真实统计信息、自己就会选对。
        """
        # 点查那半刻意**在下面那条语句之外**算好:
        # `test_group_partition_query_is_bounded_by_my_memberships` 把这条语句的查询
        # 计划钉死,而它靠 `inspect.getsource` 把执行调用之后的全部字符串字面量拼成
        # SQL —— 点查子句写在里面会被无条件粘进列表形态,参数个数当场对不上。
        point_filter = "" if notebook_id is None else "AND ng.notebook_id = ? "
        params = (user_id,) if notebook_id is None else (user_id, notebook_id)
        return db.execute(
            "SELECT nb.*, u.username AS _owner_username, "
            "g.id AS _group_id, g.name AS _group_name, g.kind AS _group_kind, "
            "ng.role AS _grant_role,"
            "COALESCE(ip.indexing_pipeline_id,'') AS _published_pipeline_id,"
            "COALESCE(ip.indexing_pipeline_version,'builtin.chunk.v1') "
            "AS _published_pipeline_version "
            "FROM group_members gm "
            "CROSS JOIN notebook_grants ng ON ng.principal_id = gm.group_id "
            "AND ng.principal_type IN ('group', 'group_admins') "
            "JOIN groups g ON g.id = gm.group_id "
            "JOIN notebooks nb ON nb.id = ng.notebook_id "
            "LEFT JOIN users u ON u.id = nb.created_by "
            "LEFT JOIN unified_kg_state ip ON ip.notebook_id=nb.id "
            "WHERE gm.user_id = ? " + point_filter
            + "AND (ng.principal_type = 'group' OR gm.role = 'admin') "
            "AND nb.status != 'copying' "
            "ORDER BY nb.created_at ASC, nb.id ASC, g.id ASC",
            params,
        ).fetchall()

    @staticmethod
    def memory_counts_by_owner_notebook(
        db: sqlite3.Connection, user_id: str
    ) -> dict[tuple[str, str], int]:
        """One owner-scoped grouped query for every notebook card.

        Memory is private to ``created_by`` even when the notebook is shared;
        grouping by both privacy key and notebook id makes that scope explicit
        and avoids a per-card query.
        """
        rows = db.execute(
            "SELECT created_by, notebook_id, COUNT(*) AS c FROM memory_items "
            "WHERE created_by=? GROUP BY created_by, notebook_id",
            (user_id,),
        ).fetchall()
        return {
            (row["created_by"], row["notebook_id"]): int(row["c"])
            for row in rows
        }

    def list_user_usage(self) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            users = db.execute(
                "SELECT id, username, display_name, role, created_at "
                "FROM users ORDER BY created_at, id"
            ).fetchall()
            notebooks = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT created_by AS k, COUNT(*) AS c FROM notebooks "
                    "WHERE status != 'copying' GROUP BY created_by"
                ).fetchall()
            }
            sources = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT k,SUM(c) AS c FROM ("
                    "SELECT nb.created_by AS k,COUNT(*) AS c FROM sources s "
                    "JOIN notebooks nb ON nb.id=s.notebook_id GROUP BY nb.created_by "
                    "UNION ALL "
                    "SELECT a.notebook_owner_id AS k,COUNT(*) AS c "
                    "FROM retained_user_activity a WHERE a.activity_type='source' "
                    "AND julianday(a.expires_at)>julianday('now') "
                    "AND NOT EXISTS(SELECT 1 FROM notebooks live "
                    "WHERE live.id=a.notebook_id) GROUP BY a.notebook_owner_id"
                    ") GROUP BY k"
                ).fetchall()
            }
            conversations = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT created_by AS k, COUNT(*) AS c FROM conversations GROUP BY created_by"
                ).fetchall()
            }
            questions = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT k,SUM(c) AS c FROM ("
                    "SELECT created_by AS k,COUNT(*) AS c FROM ask_jobs GROUP BY created_by "
                    "UNION ALL "
                    "SELECT a.actor_id AS k,COUNT(*) AS c "
                    "FROM retained_user_activity a WHERE a.activity_type='ask' "
                    "AND julianday(a.expires_at)>julianday('now') "
                    "AND NOT EXISTS(SELECT 1 FROM notebooks live "
                    "WHERE live.id=a.notebook_id) GROUP BY a.actor_id"
                    ") GROUP BY k"
                ).fetchall()
            }
            # 报告按**创建者**归集,不按笔记本 owner(群组知识共享 P1-T3b 裁决)。
            # 共享笔记本里成员建的是**自己的**报告(行级 created_by 隔离,owner 都看
            # 不见),把它记到库 owner 头上等于让一个人的用量出现在另一个人的账上。
            # 与三条既有口径同一谓词:`questions`(按 ask_jobs.created_by)、
            # `list_user_notebooks` 的展开报告数、以及活动流的报告条目——那三处早已
            # 按 created_by 收窄,只有这个总数还在 join notebooks,同一屏自相矛盾。
            reports = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT k,SUM(c) AS c FROM ("
                    "SELECT created_by AS k,COUNT(*) AS c FROM reports GROUP BY created_by "
                    "UNION ALL "
                    "SELECT a.actor_id AS k,COUNT(*) AS c "
                    "FROM retained_user_activity a WHERE a.activity_type='report' "
                    "AND julianday(a.expires_at)>julianday('now') "
                    "AND NOT EXISTS(SELECT 1 FROM notebooks live "
                    "WHERE live.id=a.notebook_id) GROUP BY a.actor_id"
                    ") GROUP BY k"
                ).fetchall()
            }
            # 「最近活跃」与管理员活动流使用同一批用户动作:上传可见来源、提交
            # 提问、发起深度报告。取 created_at 而不是后台 worker 会继续改动的
            # updated_at，避免用户离开后仍因解析/生成完成而被显示成刚刚活跃。
            #
            # SQLite 的时间列混有裸 UTC 与带 offset 的 ISO 串，不能直接 MAX(text)。
            # 每一类先按用户压成一个候选，再只在至多 4×用户数的候选上做窗口
            # 排序；_absolute_instant 与活动流的排序/游标使用同一条绝对时间
            # 判据。SQLite 对单个 MIN/MAX aggregate 的 bare columns 保证取自
            # 胜出行，因此 m/activity_id 与 MAX(sort_instant) 属于同一条活动；
            # 绝对时刻并列时两种原始 offset 写法都表示同一个 last_active。
            activity_candidates = (
                "SELECT s.uploaded_by AS k,s.created_at AS m,"
                f"MAX({_absolute_instant('s.created_at')}) AS sort_instant,"
                "'source:'||s.id AS activity_id FROM sources s "
                "JOIN notebooks nb ON nb.id=s.notebook_id "
                "WHERE nb.status!='copying' "
                "AND s.uploaded_by IS NOT NULL AND s.uploaded_by!='' "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE} GROUP BY s.uploaded_by "
                "UNION ALL "
                "SELECT j.created_by AS k,j.created_at AS m,"
                f"MAX({_absolute_instant('j.created_at')}) AS sort_instant,"
                "'ask:'||j.id AS activity_id FROM ask_jobs j GROUP BY j.created_by "
                "UNION ALL "
                "SELECT r.created_by AS k,r.created_at AS m,"
                f"MAX({_absolute_instant('r.created_at')}) AS sort_instant,"
                "'report:'||r.id AS activity_id FROM reports r GROUP BY r.created_by "
                "UNION ALL "
                "SELECT a.actor_id AS k,a.created_at AS m,"
                f"MAX({_absolute_instant('a.created_at')}) AS sort_instant,"
                "'retained:'||a.activity_type||':'||a.record_id AS activity_id "
                "FROM retained_user_activity a "
                "WHERE a.activity_type IN ('source','ask','report') "
                "AND a.actor_id!='' "
                "AND julianday(a.expires_at)>julianday('now') "
                "AND NOT EXISTS(SELECT 1 FROM notebooks live "
                "WHERE live.id=a.notebook_id) GROUP BY a.actor_id"
            )
            active = {
                row["k"]: (
                    None
                    if cursor_instant_text(row["m"]) == UNRESOLVED_INSTANT_ISO
                    else cursor_instant_text(row["m"])
                )
                for row in db.execute(
                    "SELECT k,m FROM (SELECT k,m,ROW_NUMBER() OVER ("
                    "PARTITION BY k ORDER BY sort_instant DESC,activity_id DESC"
                    ") AS rn FROM (" + activity_candidates + ")) WHERE rn=1"
                ).fetchall()
            }
            # 每笔记本文档数量上限:一次批量取所有 per-user 覆盖值(仅非空行),
            # 再把全局默认解析一次 —— 都不 per-user 开连接/重复回退,满足效率约束。
            overrides = {
                row["k"]: int(row["v"])
                for row in db.execute(
                    "SELECT user_id AS k, upload_document_limit AS v FROM user_profiles "
                    "WHERE upload_document_limit IS NOT NULL"
                ).fetchall()
            }
            default_row = db.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (_UPLOAD_LIMIT_DEFAULT_KEY,),
            ).fetchone()
        # 全局默认只算一次,供所有无覆盖用户共用;回退语义与强制路径同一真源
        # (_resolve_global_default:坏值/缺省回退 config)。settings 缺省时(备用
        # QueryStore,不走这条路)兜底 get_settings。
        global_default = _resolve_global_default(
            default_row["value"] if default_row is not None else None,
            self.settings or get_settings(),
        )
        result: list[dict[str, Any]] = []
        for user in users:
            override = overrides.get(user["id"])
            result.append(
                {
                    "id": user["id"],
                    "username": user["username"] or user["display_name"] or user["id"],
                    "role": user["role"],
                    "created_at": user["created_at"],
                    "notebooks": notebooks.get(user["id"], 0),
                    "sources": sources.get(user["id"], 0),
                    "conversations": conversations.get(user["id"], 0),
                    "questions": questions.get(user["id"], 0),
                    "reports": reports.get(user["id"], 0),
                    "last_active": active.get(user["id"]),
                    "upload_limit": override if override is not None else global_default,
                    "upload_limit_overridden": override is not None,
                }
            )
        return result

    def list_user_notebooks(self, user_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            notebooks = db.execute(
                "SELECT id, name, status, created_at, updated_at FROM notebooks "
                "WHERE created_by = ? AND status != 'copying' ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            ids = [row["id"] for row in notebooks]
            sources: dict[str, int] = {}
            conversations: dict[str, int] = {}
            questions: dict[str, int] = {}
            reports: dict[str, int] = {}
            if ids:
                placeholders = ",".join("?" * len(ids))
                # VISIBLE_SOURCE_TYPES_PREDICATE 不可省:它是「面向用户可见来源数」的
                # 单一真源(见 source_store 里那条注释)。少了它,这个数会把 memory /
                # knowhow 投影行一起算进去——存过一条 Memory 或建过一张 Knowhow 表的
                # 用户,左栏表头写「来源 3」而展开的清单(list_sources_page,带谓词)
                # 只有 1 条,同一屏自相矛盾。
                sources = {
                    row["k"]: row["c"]
                    for row in db.execute(
                        f"SELECT notebook_id AS k, COUNT(*) AS c FROM sources "
                        f"WHERE notebook_id IN ({placeholders}) "
                        f"AND {VISIBLE_SOURCE_TYPES_PREDICATE} GROUP BY notebook_id",
                        ids,
                    ).fetchall()
                }
                conversations = {
                    row["k"]: row["c"]
                    for row in db.execute(
                        f"SELECT notebook_id AS k, COUNT(*) AS c FROM conversations "
                        f"WHERE notebook_id IN ({placeholders}) GROUP BY notebook_id",
                        ids,
                    ).fetchall()
                }
                questions = {
                    row["k"]: row["c"]
                    for row in db.execute(
                        f"SELECT notebook_id AS k, COUNT(*) AS c FROM ask_jobs "
                        f"WHERE notebook_id IN ({placeholders}) AND created_by = ? "
                        f"GROUP BY notebook_id",
                        [*ids, user_id],
                    ).fetchall()
                }
                # created_by = ? 同 questions 一条理由:list_user_activity 展开清单
                # 也是按 created_by 收窄的 owner-only 报告(见该方法 report_rows 那条
                # SQL)。少了它,共享笔记本里别的可写成员建的报告会被算进这个笔记本
                # owner 的表头,而点开活动流却看不到对应条目——同一屏自相矛盾。
                reports = {
                    row["k"]: row["c"]
                    for row in db.execute(
                        f"SELECT notebook_id AS k, COUNT(*) AS c FROM reports "
                        f"WHERE notebook_id IN ({placeholders}) AND created_by = ? "
                        f"GROUP BY notebook_id",
                        [*ids, user_id],
                    ).fetchall()
                }
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "sources": sources.get(row["id"], 0),
                "conversations": conversations.get(row["id"], 0),
                "questions": questions.get(row["id"], 0),
                "reports": reports.get(row["id"], 0),
            }
            for row in notebooks
        ]

    def notebook_exists_for_owner(self, notebook_id: str, user_id: str) -> bool:
        """这一行是不是这位用户自有的(非 copying)笔记本 —— **一行**的问题一行回答。

        谓词与 list_user_activity 里的 owned 分支逐字相同,刻意不复用
        list_user_notebooks:那是一次全量用量聚合(1 条 notebooks 查询 + 4 条覆盖该用户
        全部笔记本的 GROUP BY COUNT),而调用方(来源清单端点)每展开一个笔记本就问一次。
        """
        with self.database.connect() as db:
            row = db.execute(
                "SELECT 1 FROM notebooks WHERE id = ? AND created_by = ? "
                "AND status != 'copying'",
                (notebook_id, user_id),
            ).fetchone()
        return row is not None

    def list_user_activity(
        self,
        user_id: str,
        *,
        activity_type: str | None = None,
        include_inaccessible_questions: bool = False,
        notebook_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        before_ts: str | None = None,
        before_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Admin-only 用户活动流:ask_jobs / sources / reports 三类各自 keyset 分页,
        在 Python 内按 (绝对时刻, id) 降序归并——不做三表 UNION(大库上 UNION ALL +
        全局 ORDER BY 会退化成排序爆炸;三份各自有界 + 内存归并的边界可诚实回报「本页
        覆盖到 HH:MM 为止」)。真源见
        docs/superpowers/specs/2026-08-04-user-activity-log-view-design_zh.md §4.1。

        排序键统一是 (绝对时刻 DESC, id DESC);ask_jobs 用 created_at 排序而不是
        asked_at(后者是浏览器提交时刻、可能为空串,拿它当排序键会让游标不稳定,只作为
        字段返回)。「绝对时刻」由 ``_absolute_instant`` 定义(``julianday()``,见那里
        对混合 offset 的说明),SQL 的 ORDER BY / 范围 / 游标与 Python 归并键**用同一
        个值**:它作为 ``sort_instant`` 列跟着行一起回来,Python 不再自己算一遍,也就不
        可能与 SQL 口径分叉。复合游标谓词按 (绝对时刻, id) 的严格字典序比较,避免并列
        时间戳下只比 ts 跳行。

        混合流与按来源/报告过滤时都按**该用户自有的笔记本**收窄(owner-only),与
        sources 侧同口径。只有无 notebook_id 的 ask-only「提问概览」跟用户总览的
        questions 合计对齐，按 created_by 纳入该用户在共享库里的提交；本人查看时
        仍叠加当前实时读权，管理员审计可显式纳入已经失权的历史提交。显式选某库仍
        只接受左栏列出的自有笔记本。

        ``activity_type`` 可把读取收窄到 ask/source/report 中的一类；筛选发生在各表
        的 LIMIT 之前，因此「只看提问」仍能完整翻页，不会被较新的来源或报告挤出窗口。

        ``since``/``until`` 是半开区间 [since, until);它与 ``before_ts`` 一起在方法
        入口经 ``parse_activity_instant`` 归一成绝对时刻(**双后端共用同一份解析**),
        再渲染成本后端的比较形态(交给 ``julianday()``)。畸形输入抛 ``ValueError``,
        不静默忽略——见 ``app.core.activity_time`` 的契约。范围与游标同时生效(范围管
        窗口、游标管翻页)。
        """
        limit = max(1, min(200, int(limit)))
        fetch_limit = limit + 1
        empty: dict[str, Any] = {"items": [], "has_more": False, "next_cursor": None}
        cursor_active = before_ts is not None and before_id is not None
        # 归一后的绝对时刻直接作为 julianday() 的入参:裸 naive 输入按 UTC 读,因而
        # 历史行原样回传的游标 ts 与它自己那一行的 julianday 逐位相等(精确往返)。
        since_arg = (
            parse_activity_instant(since, field="since").isoformat()
            if since is not None else None
        )
        until_arg = (
            parse_activity_instant(until, field="until").isoformat()
            if until is not None else None
        )
        before_arg = (
            parse_activity_instant(before_ts, field="before_ts").isoformat()
            if cursor_active else None
        )

        def _range_and_cursor_clause(prefix: str) -> tuple[str, list[Any]]:
            instant = _absolute_instant(f"{prefix}created_at")
            clause = ""
            params: list[Any] = []
            if since_arg is not None:
                clause += f" AND {instant} >= julianday(?)"
                params.append(since_arg)
            if until_arg is not None:
                clause += f" AND {instant} < julianday(?)"
                params.append(until_arg)
            if cursor_active:
                clause += (
                    f" AND ({instant} < julianday(?) OR "
                    f"({instant} = julianday(?) AND {prefix}id < ?))"
                )
                params.extend([before_arg, before_arg, before_id])
            return clause, params

        def _retained_range_and_cursor_clause() -> tuple[str, list[Any]]:
            instant = _absolute_instant("a.created_at")
            clause = ""
            params: list[Any] = []
            if since_arg is not None:
                clause += f" AND {instant} >= julianday(?)"
                params.append(since_arg)
            if until_arg is not None:
                clause += f" AND {instant} < julianday(?)"
                params.append(until_arg)
            if cursor_active:
                clause += (
                    f" AND ({instant} < julianday(?) OR "
                    f"({instant} = julianday(?) AND a.record_id < ?))"
                )
                params.extend([before_arg, before_arg, before_id])
            return clause, params

        with self.database.connect() as db:
            if notebook_id is not None:
                # 显式收窄到单个 notebook 时,必须先校验它属于该用户——不属于就返回
                # 空结果(不抛异常、不泄露存在性),口径与 list_user_notebooks 一致。
                owned = db.execute(
                    "SELECT 1 FROM notebooks WHERE id = ? AND created_by = ? "
                    "AND status != 'copying'",
                    (notebook_id, user_id),
                ).fetchone()
                if owned is None:
                    return empty
                owned_notebook_ids = [notebook_id]
            else:
                owned_notebook_ids = [
                    row["id"]
                    for row in db.execute(
                        "SELECT id FROM notebooks WHERE created_by = ? "
                        "AND status != 'copying'",
                        (user_id,),
                    ).fetchall()
                ]
            all_question_submissions = activity_type == "ask" and notebook_id is None
            owned_placeholders = ",".join("?" for _ in owned_notebook_ids)

            # 1. 提问:created_by 之外还收窄到自有笔记本(owner-only,同 sources)。
            # notebook_id 已在上面解析成 owned_notebook_ids == [notebook_id],所以这
            # 条 IN 同时兑现了「按库过滤」和「归属校验」两件事,不需要第二条谓词。
            ask_rows = []
            if activity_type in (None, "ask") and (
                all_question_submissions or owned_notebook_ids
            ):
                # 提问概览与用户总览的 questions 合计同口径：无 notebook_id 时按
                # created_by 覆盖该用户在自有库和共享库里的全部提交。混合流以及显式
                # 选中左栏某库时仍保持既有 owner-only 展开语义。
                ask_notebook_clause = ""
                ask_params: list[Any] = [user_id]
                if all_question_submissions and not include_inaccessible_questions:
                    ask_notebook_clause = (
                        " AND "
                        + access_sql.read_access_exists_clause("ask_jobs")
                    )
                    ask_params.extend(access_sql.read_access_params(user_id))
                elif not all_question_submissions:
                    ask_notebook_clause = (
                        f" AND notebook_id IN ({owned_placeholders})"
                    )
                    ask_params.extend(owned_notebook_ids)
                ask_range_clause, ask_range_params = _range_and_cursor_clause("")
                ask_params.extend(ask_range_params)
                ask_rows = db.execute(
                    "SELECT id, notebook_id, created_at, asked_at, conversation_id, "
                    "question, mode, status, answer_id, error, "
                    f"{_absolute_instant('created_at')} AS sort_instant FROM ask_jobs "
                    f"WHERE created_by = ?{ask_notebook_clause}"
                    f"{ask_range_clause} "
                    f"ORDER BY {_absolute_instant('created_at')} DESC, id DESC LIMIT ?",
                    [*ask_params, fetch_limit],
                ).fetchall()

            # 2. 来源:sources 没有 created_by 列,靠"该用户自己的笔记本 id"限定范围
            # (与 list_user_notebooks 完全同口径)。必须带
            # VISIBLE_SOURCE_TYPES_PREDICATE 排除隐藏合成源(memory 派生源按创建者
            # 私有、knowhow 投影行同样非用户可见,漏掉这条谓词就会把别人的私有 Memory
            # 摊开)。source_paper_meta 用 LEFT JOIN 一次性带出 is_paper/paper_title,
            # 不为它多发一次查询——上层 source_display_title 需要这两个字段判断
            # 是否显示论文标题。s.doc_type / s.error_message 一并带出仅用于 Python 侧
            # 派生 paper_meta_status / parse_quality_warning,本身都不作为返回字段
            # (error_message 是 str(exc) 原样落库、可能带服务端绝对路径,见下)。
            source_rows = []
            if activity_type in (None, "source") and owned_notebook_ids:
                source_params: list[Any] = list(owned_notebook_ids)
                source_range_clause, source_range_params = _range_and_cursor_clause("s.")
                source_params.extend(source_range_params)
                source_rows = db.execute(
                    "SELECT s.id, s.notebook_id, s.created_at, s.title, s.file_name, "
                    "s.source_type, s.parse_status, s.status, s.error_message, "
                    "s.doc_type, m.is_paper, m.paper_title, "
                    f"{_absolute_instant('s.created_at')} AS sort_instant "
                    "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id = s.id "
                    f"WHERE s.notebook_id IN ({owned_placeholders}) "
                    f"AND {VISIBLE_SOURCE_TYPES_PREDICATE}{source_range_clause} "
                    f"ORDER BY {_absolute_instant('s.created_at')} DESC, s.id DESC LIMIT ?",
                    [*source_params, fetch_limit],
                ).fetchall()

            # 2b. extraction_warning 是「最近一次 extraction_runs.error_message 里的
            # windows_failed=N/T 标记」派生态,不是 sources 上的列——必须单独一条批量
            # 查询(不是逐行 N+1),只对本页已取回的 source id 集合取一次。
            latest_extraction_error: dict[str, str] = {}
            source_ids_on_page = [row["id"] for row in source_rows]
            if source_ids_on_page:
                ph2 = ",".join("?" for _ in source_ids_on_page)
                for r in db.execute(
                    "SELECT source_id, error_message FROM extraction_runs "
                    f"WHERE source_id IN ({ph2}) ORDER BY source_id, created_at DESC",
                    source_ids_on_page,
                ).fetchall():
                    latest_extraction_error.setdefault(
                        r["source_id"], r["error_message"] or ""
                    )

            # 3. 报告:与提问同口径(created_by + 自有笔记本)。understanding_json 一并
            # 带出仅用于 Python 侧提取 generation_started_at(镜像 ReportStore.
            # row_to_dict 同一个 "$._generation_started_at" 表达式,不发明第二套提取
            # 写法),understanding_json 本身不作为返回字段。
            report_rows = []
            if activity_type in (None, "report") and owned_notebook_ids:
                report_params: list[Any] = [user_id, *owned_notebook_ids]
                report_range_clause, report_range_params = _range_and_cursor_clause("")
                report_params.extend(report_range_params)
                report_rows = db.execute(
                    "SELECT id, notebook_id, created_at, updated_at, question, depth, "
                    "status, understanding_json, "
                    f"{_absolute_instant('created_at')} AS sort_instant FROM reports "
                    f"WHERE created_by = ? AND notebook_id IN ({owned_placeholders})"
                    f"{report_range_clause} "
                    f"ORDER BY {_absolute_instant('created_at')} DESC, id DESC LIMIT ?",
                    [*report_params, fetch_limit],
                ).fetchall()

            # 4. 已删除 notebook 的最小分析快照。它没有 notebook FK，且
            # expires_at 是强读闸：物理 GC 即使稍后执行，过期行也不会再返回。
            # 普通用户的 self-service 视图仍要求实时 notebook 读权，因此删除
            # 后的快照只进入管理员审计（include_inaccessible_questions=True）。
            retained_rows = []
            if notebook_id is None and include_inaccessible_questions:
                retained_params: list[Any] = []
                if activity_type == "ask":
                    retained_scope = "a.activity_type='ask' AND a.actor_id=?"
                    retained_params.append(user_id)
                elif activity_type == "source":
                    retained_scope = (
                        "a.activity_type='source' AND a.notebook_owner_id=?"
                    )
                    retained_params.append(user_id)
                elif activity_type == "report":
                    retained_scope = (
                        "a.activity_type='report' AND a.actor_id=? "
                        "AND a.notebook_owner_id=?"
                    )
                    retained_params.extend([user_id, user_id])
                else:
                    retained_scope = (
                        "((a.activity_type='ask' AND a.actor_id=? "
                        "AND a.notebook_owner_id=?) OR "
                        "(a.activity_type='source' AND a.notebook_owner_id=?) OR "
                        "(a.activity_type='report' AND a.actor_id=? "
                        "AND a.notebook_owner_id=?))"
                    )
                    retained_params.extend(
                        [user_id, user_id, user_id, user_id, user_id]
                    )
                retained_range, retained_range_params = (
                    _retained_range_and_cursor_clause()
                )
                retained_params.extend(retained_range_params)
                retained_rows = db.execute(
                    "SELECT a.*," + _absolute_instant("a.created_at")
                    + " AS sort_instant FROM retained_user_activity a "
                    f"WHERE {retained_scope} "
                    "AND julianday(a.expires_at)>julianday('now')"
                    " AND NOT EXISTS(SELECT 1 FROM notebooks live "
                    "WHERE live.id=a.notebook_id)"
                    f"{retained_range} "
                    f"ORDER BY {_absolute_instant('a.created_at')} DESC,"
                    "a.record_id DESC LIMIT ?",
                    [*retained_params, fetch_limit],
                ).fetchall()

        # 归并键与 SQL 逐位同源:(sort_instant, id) 就是 SQL 自己算出来的那两个值。
        pool: list[tuple[float, str, dict[str, Any]]] = []
        for row in ask_rows:
            pool.append(
                (row["sort_instant"], row["id"], {
                    "type": "ask",
                    "id": row["id"],
                    "notebook_id": row["notebook_id"],
                    "created_at": row["created_at"],
                    "asked_at": row["asked_at"],
                    "conversation_id": row["conversation_id"],
                    "question": row["question"],
                    "mode": row["mode"],
                    "status": row["status"],
                    "answer_id": row["answer_id"],
                    "error": row["error"],
                })
            )
        for row in source_rows:
            pool.append(
                (row["sort_instant"], row["id"], {
                    "type": "source",
                    "id": row["id"],
                    "notebook_id": row["notebook_id"],
                    "created_at": row["created_at"],
                    "title": row["title"],
                    "file_name": row["file_name"],
                    "source_type": row["source_type"],
                    "parse_status": row["parse_status"],
                    "status": row["status"],
                    # 刻意**不**返回 sources.error_message:它是 str(exc) 原样落库的
                    # 异常串,可能带服务端绝对路径(FileNotFoundError: /…/storage/…),
                    # 而这条流是给 admin 看**别人**的活动。安全事实按
                    # ScopedSourceDetail 的既有做法换成 parse_failed 布尔,原始诊断
                    # 一个字都不跨出这个库;质量事实另由 parse_quality_warning 承载。
                    "parse_failed": row["parse_status"] == "failed",
                    "is_paper": bool(row["is_paper"]),
                    "paper_title": row["paper_title"] or "",
                    "extraction_warning": extraction_warning_text(
                        latest_extraction_error.get(row["id"])
                    ),
                    "parse_quality_warning": has_pdf_python_fallback_warning(
                        row["error_message"]
                    ),
                    "paper_meta_status": paper_meta_status(
                        row["is_paper"], row["source_type"], row["doc_type"],
                        row["parse_status"],
                    ),
                })
            )
        for row in report_rows:
            understanding = json.loads(row["understanding_json"] or "{}")
            generation_started_at = str(
                understanding.get("_generation_started_at", "") or ""
            )
            pool.append(
                (row["sort_instant"], row["id"], {
                    "type": "report",
                    "id": row["id"],
                    "notebook_id": row["notebook_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "question": row["question"],
                    "depth": int(row["depth"]),
                    "status": row["status"],
                    "generation_started_at": generation_started_at,
                })
            )
        for row in retained_rows:
            common = {
                "type": row["activity_type"],
                "id": row["record_id"],
                "notebook_id": row["notebook_id"],
                "notebook_name": row["notebook_name"],
                "notebook_deleted_at": row["deleted_at"],
                "retained_until": row["expires_at"],
                "created_at": row["created_at"],
                "status": row["status"],
            }
            if row["activity_type"] == "ask":
                item = {
                    **common,
                    "asked_at": row["asked_at"],
                    "conversation_id": row["conversation_id"],
                    "question": row["question"],
                    "mode": row["mode"],
                    "answer_id": "",
                    "error": "",
                }
            elif row["activity_type"] == "source":
                item = {
                    **common,
                    "display_title": row["display_title"],
                    "file_name": row["file_name"],
                    "source_type": row["source_type"],
                    "parse_status": row["parse_status"],
                    "parse_failed": bool(row["parse_failed"]),
                    "extraction_warning": "",
                    "parse_quality_warning": False,
                    "paper_meta_status": "",
                }
            else:
                item = {
                    **common,
                    "updated_at": row["updated_at"],
                    "question": row["question"],
                    "depth": int(row["depth"]),
                    "generation_started_at": row["generation_started_at"],
                }
            pool.append((row["sort_instant"], row["record_id"], item))

        # Every SELECT above is bounded, but SQLite's legacy read connection
        # does not hold one snapshot across those statements. If notebook
        # deletion commits after the live rows and before retained_rows, both
        # lifecycles can be present here. Retained rows are appended last and
        # describe the later lifecycle, so they authoritatively replace the
        # same public identity instead of producing duplicate React keys.
        by_identity: dict[tuple[str, str], tuple[float, str, dict[str, Any]]] = {}
        for entry in pool:
            item = entry[2]
            by_identity[(item["type"], item["id"])] = entry
        pool = list(by_identity.values())

        pool.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        has_more = (
            len(ask_rows) > limit
            or len(source_rows) > limit
            or len(report_rows) > limit
            or len(retained_rows) > limit
            or len(pool) > limit
        )
        page = [entry[2] for entry in pool[:limit]]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            # 游标 ts 走 cursor_instant_text:可解析就原样回传(julianday() 对它与对本行
            # 的列值逐位相等,下一页的并列判据因此精确命中这一行);**不可解析**(空串
            # 这类历史脏值)则回传哨兵——那正是该行在 _absolute_instant 里实际参与排序
            # 的值。原样回传不可解析的串会让下一页在 parse_activity_instant 抛
            # ValueError:游标必须只发自己解析得回来的值(见 activity_time 契约)。
            next_cursor = {
                "ts": cursor_instant_text(last["created_at"]),
                "id": last["id"],
            }
        return {"items": page, "has_more": has_more, "next_cursor": next_cursor}

    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics:
        with self.database.connect() as db:
            exists = db.execute(
                "SELECT 1 FROM notebooks WHERE id = ? AND status != 'copying'",
                (notebook_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(notebook_id)
            answers_total = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM answers WHERE notebook_id = ?",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            useful = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE notebook_id = ? AND rating = 'useful'",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            not_useful = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE notebook_id = ? AND rating = 'not_useful'",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            low_rated = [
                row["question"]
                for row in db.execute(
                    "SELECT DISTINCT a.question FROM feedback f "
                    "JOIN answers a ON a.id = f.answer_id "
                    "WHERE f.notebook_id = ? AND f.rating = 'not_useful' "
                    "ORDER BY f.created_at DESC LIMIT 10",
                    (notebook_id,),
                ).fetchall()
            ]
            # seq-gated count cache (non-deprecated) — same GROUP BY, memoized.
            from app.repositories.sqlite import knowledge_counts_cache
            knowledge_counts = knowledge_counts_cache.type_counts(db, notebook_id)
            # Memory-derived AND knowhow-projection hidden synthetic sources
            # (source_type IN ('memory', 'knowhow')) are excluded — this feeds
            # the /analytics 看板 parse_status distribution, a user-facing
            # surface; see visible_source_count.
            source_status_counts = {
                row["parse_status"]: int(row["c"])
                for row in db.execute(
                    "SELECT parse_status, COUNT(*) AS c FROM sources "
                    "WHERE notebook_id = ? AND source_type NOT IN ('memory', 'knowhow') "
                    "GROUP BY parse_status",
                    (notebook_id,),
                ).fetchall()
            }
            # paper-meta 三态计数(看板;paper-metadata Task 4)。paper_meta 写入
            # (create_source/upsert_paper_meta)不 bump kg_mutation_seq,故这两条
            # GROUP BY 必须未缓存直读——不能像 knowledge_counts 那样走
            # knowledge_counts_cache 的 seq 门(会读到陈旧值,见该模块 docstring
            # 对 sources COUNT 的同款排除说明)。is_paper 计数走
            # idx_source_paper_meta_nb(notebook_id)。
            by_is_paper = {
                int(row["is_paper"]): int(row["c"])
                for row in db.execute(
                    "SELECT is_paper, COUNT(*) AS c FROM source_paper_meta "
                    "WHERE notebook_id = ? GROUP BY is_paper",
                    (notebook_id,),
                ).fetchall()
            }
            # missing 计数是 SourceStore.sources_missing_paper_meta 的 COUNT 镜像
            # (WHERE 谓词共用 source_store 的 PAPER_META_*_SQL 常量,构造性同口径),
            # 走 idx_sources_nb_parse_status_type(notebook_id, parse_status, source_type)。
            missing = int(db.execute(
                "SELECT COUNT(*) AS c FROM sources s "
                "WHERE s.notebook_id = ?"
                f"{PAPER_META_ELIGIBLE_SQL}{PAPER_META_NO_META_SQL}",
                (notebook_id,),
            ).fetchone()["c"])
            paper_meta_counts = {
                "has_meta": by_is_paper.get(1, 0),
                "marker": by_is_paper.get(0, 0),
                "missing": missing,
            }
        rated = useful + not_useful
        return NotebookAnalytics(
            answers_total=answers_total,
            feedback_useful=useful,
            feedback_not_useful=not_useful,
            usefulness_rate=round(useful / rated, 3) if rated else 0.0,
            low_rated_questions=low_rated,
            knowledge_counts=knowledge_counts,
            source_status_counts=source_status_counts,
            paper_meta_counts=paper_meta_counts,
        )

    def pending_actions_projection_rows(self, user_id: str) -> dict:
        items: list[dict[str, Any]] = []
        with self.database.connect() as db:
            mine = db.execute(
                "SELECT id, name FROM notebooks WHERE created_by = ? AND status != 'copying'",
                (user_id,),
            ).fetchall()
            name_of = {row["id"]: row["name"] for row in mine}
            notebook_ids = list(name_of)
            # 报告这一半**不在** `if notebook_ids:` 闸内(P1-T3b):它的谓词只有
            # `created_by = 当前用户`,一个 notebook id 都不消费。放在闸内时,
            # 共享库里的成员只要自己没建过库,待确认报告就整体缺席——铃铛对他
            # 永远是 0,而报告卡在 intent_ready 等他确认。零新增查询:同一条
            # SQL 只是挪出了那个 if。
            #
            # 库名走 LEFT JOIN 而不是上面那份自有库映射(P1-T4):共享库里建的报告
            # 在 `name_of` 里查不到,条目会显示成一条没有出处的「深度报告《…》」。
            # 零新增往返——同一条语句多一个 join。报告是本人建的(`created_by`
            # 已经在 WHERE 里),所以库名对他不是新披露。
            #
            # 另叠加规范读谓词(codex #517 R1 P2):创建者可能在报告等确认期间
            # 失去该库的读权(撤授权边/被移出组/共享撤销)——此时每个报告端点都
            # 对他 404,铃铛却还挂着一条永远点不开的待确认项。按「授权即时生效」
            # 的同一口径,失权的报告不进铃铛;恢复读权后自动回来(条目从未删除)。
            reports = db.execute(
                "SELECT r.id AS id, r.question AS question, "
                "r.notebook_id AS notebook_id, r.created_at AS created_at, "
                "nb.name AS notebook_name "
                "FROM reports r LEFT JOIN notebooks nb ON nb.id = r.notebook_id "
                "WHERE r.status IN ('intent_ready','outline_ready') "
                "AND r.created_by = ? AND "
                + access_sql.read_access_exists_clause("r")
                + " ORDER BY r.updated_at DESC",
                (user_id, *access_sql.read_access_params(user_id)),
            ).fetchall()
            for row in reports:
                items.append(
                    {
                        "type": "report_outline",
                        "notebook_id": row["notebook_id"],
                        "notebook_name": row["notebook_name"] or "",
                        "report_id": row["id"],
                        "title": (row["question"] or "")[:60],
                        "created_at": row["created_at"],
                    }
                )
            # 组管理员的待审批共享申请(群组知识共享 P2-T3)。谓词是「我在这个组里是
            # admin」+「申请仍 pending」,一个 notebook id 都不消费——所以和报告那一半
            # 一样放在 `if notebook_ids:` **之外**:一个只在别人库里当组管理员、自己没
            # 建过库的人,铃铛同样要提醒他有待审批申请。`gm.role='admin'` 与
            # `sr.status='pending'` 都是**精确匹配**(v50 迁移点名的红线:否定式会把
            # shadow 停车行误判成正常状态)。
            # GROUP BY 走 groups 主键 `g.id`,让 `g.name`/`g.created_at` 因函数依赖可被
            # 选择与排序而不必进 GROUP BY——PostgreSQL 对非 PK 的 `sr.group_id` 不认这层
            # 依赖(会报 GroupingError),两侧统一按 g.id 分组,行为逐字一致。
            share_requests = db.execute(
                "SELECT g.id AS group_id, g.name AS group_name, COUNT(*) AS c "
                "FROM notebook_share_requests sr "
                "JOIN group_members gm ON gm.group_id = sr.group_id "
                "AND gm.user_id = ? AND gm.role = 'admin' "
                "JOIN groups g ON g.id = sr.group_id "
                "WHERE sr.status = 'pending' "
                "GROUP BY g.id, g.name, g.created_at "
                "ORDER BY g.created_at ASC, g.id ASC",
                (user_id,),
            ).fetchall()
            for row in share_requests:
                items.append(
                    {
                        "type": "share_request",
                        "group_id": row["group_id"],
                        "group_name": row["group_name"] or "",
                        "count": int(row["c"]),
                    }
                )
            if notebook_ids:
                role_row = db.execute(
                    "SELECT role FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                is_admin = bool(role_row) and role_row["role"] == "admin"
                placeholders = ",".join("?" for _ in notebook_ids)
                governance = [
                    ("merge", "concept_merge_candidates", "status = 'pending'"),
                    ("edge", "knowledge_relations", "review_status = 'pending'"),
                ]
                if is_admin:
                    governance.append(
                        (
                            "promotion",
                            "promotion_candidates",
                            "status IN ('proposed','under_review')",
                        )
                    )
                for subtype, table, predicate in governance:
                    grouped = db.execute(
                        f"SELECT notebook_id, COUNT(*) AS c FROM {table} "
                        f"WHERE notebook_id IN ({placeholders}) AND {predicate} GROUP BY notebook_id",
                        notebook_ids,
                    ).fetchall()
                    for row in grouped:
                        if row["c"] > 0:
                            items.append(
                                {
                                    "type": "governance",
                                    "subtype": subtype,
                                    "notebook_id": row["notebook_id"],
                                    "notebook_name": name_of.get(row["notebook_id"], ""),
                                    "count": row["c"],
                                }
                            )
        return {
            "notebook_ids": notebook_ids,
            "notebook_names": name_of,
            "items": items,
        }

    @staticmethod
    def _knowledge_headline(object_type: str, payload: dict) -> str:
        keys = {
            "rule": ("title", "statement"),
            "method": ("name", "use_when"),
            "risk": ("title", "description"),
            "glossary": ("term", "definition"),
            "case": ("symptom", "context"),
            "checklist": ("question",),
            "claim": ("name", "statement"),
            "formula": ("name", "statement"),
            "procedure": ("name", "title"),
            "concept": ("name", "term", "definition"),
            "finding": ("name", "statement", "metric"),
            "principle": ("statement", "rationale"),
            "example": ("title", "problem"),
        }.get(object_type, ("name", "title", "statement", "term", "question"))
        for key in keys:
            value = str(payload.get(key, "")).strip()
            if value:
                return value[:120]
        return object_type

    @staticmethod
    def _payload_join(payload: dict) -> str:
        parts: list[str] = []
        for key, value in payload.items():
            if str(key).startswith("_"):
                continue
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, (list, tuple)):
                parts.extend(str(item) for item in value)
        return " ".join(parts)

    def search_notebook(
        self, notebook_id: str, query: str
    ) -> NotebookSearchResponse:
        needle = query.strip().lower()
        with self.database.connect() as db:
            notebook = db.execute(
                "SELECT * FROM notebooks WHERE id = ? AND status != 'copying'",
                (notebook_id,),
            ).fetchone()
            if notebook is None:
                raise KeyError(notebook_id)
            if not needle:
                return NotebookSearchResponse(query=query, hits=[])
            like = f"%{needle}%"
            cap = SEARCH_HIT_CAP
            hits: list[SearchHit] = []
            for scope, value in (
                ("Notebook", notebook["name"]),
                ("Domain", notebook["primary_domain"]),
            ):
                if needle in f"{scope} {value}".lower():
                    hits.append(
                        SearchHit(
                            scope=scope,
                            notebook_id=notebook_id,
                            label=scope,
                            text=_snippet(value or scope, needle),
                            source_id="",
                            element_id="",
                        )
                    )
            # source_type NOT IN ('memory', 'knowhow') keeps Memory-derived AND
            # knowhow-projection hidden synthetic sources out of the search box
            # (GET /notebooks/{id}/search) — same user-facing hide as
            # list_sources; a knowhow hidden source's title ("Knowhow 表：…")
            # would otherwise surface as a dead-end "Source" hit with no
            # coherent source view to jump to (citation-jump to the row detail
            # drawer is PR-2 scope, per the design spec).
            source_rows = db.execute(
                "SELECT * FROM sources WHERE notebook_id = ? "
                "AND source_type NOT IN ('memory', 'knowhow') AND "
                "(LOWER(title) LIKE ? OR LOWER(summary) LIKE ? OR LOWER(file_name) LIKE ?) "
                "ORDER BY created_at ASC LIMIT ?",
                (notebook_id, like, like, like, cap),
            ).fetchall() if len(hits) < cap else ()
            for row in source_rows:
                label = row["title"] or row["file_name"]
                body = row["summary"] or row["file_name"] or row["title"]
                hits.append(
                    SearchHit(
                        scope="Source",
                        notebook_id=notebook_id,
                        label=label,
                        text=_snippet(body, needle),
                        source_id=row["id"],
                        element_id="",
                    )
                )
            # Same hide on the element leg: a memory source's or knowhow
            # hidden source's element text must not leak in as a
            # scope="Element" hit either (a knowhow cell's own element would
            # otherwise show up here with the same dead-end-navigation issue
            # as the "Source" leg above).
            element_rows = db.execute(
                "SELECT se.*, s.title AS source_title FROM source_elements se "
                "JOIN sources s ON s.id = se.source_id "
                "WHERE s.notebook_id = ? AND s.source_type NOT IN ('memory', 'knowhow') AND "
                "(LOWER(se.text) LIKE ? OR LOWER(se.location_label) LIKE ? OR LOWER(s.title) LIKE ?) "
                "LIMIT ?",
                (notebook_id, like, like, like, cap),
            ).fetchall() if len(hits) < cap else ()
            for row in element_rows:
                label = f"{row['source_title']} · {row['location_label']}"
                hits.append(
                    SearchHit(
                        scope="Element",
                        notebook_id=notebook_id,
                        label=label,
                        text=_snippet(row["text"] or label, needle),
                        source_id=row["source_id"],
                        element_id=row["id"],
                    )
                )
            knowledge_rows = db.execute(
                "SELECT id, object_type, payload FROM knowledge_objects "
                "WHERE notebook_id = ? AND status != 'deprecated' AND LOWER(payload) LIKE ? LIMIT ?",
                (notebook_id, like, cap),
            ).fetchall() if len(hits) < cap else ()
            for row in knowledge_rows:
                payload = json.loads(row["payload"] or "{}")
                label = OBJECT_TYPE_LABELS.get(row["object_type"], row["object_type"])
                headline = self._knowledge_headline(row["object_type"], payload)
                body = self._payload_join(payload)
                if needle not in f"{label} {headline} {body}".lower():
                    continue
                hits.append(
                    SearchHit(
                        scope=label,
                        notebook_id=notebook_id,
                        label=headline,
                        text=_snippet(body or headline, needle),
                        source_id="",
                        element_id="",
                    )
                )
        return NotebookSearchResponse(query=query, hits=hits[:SEARCH_HIT_CAP])

    def load_notebook_scale_facts(
        self, notebook_id: str
    ) -> NotebookScaleFacts:
        with self.database.connect() as db:
            def one(sql: str) -> int:
                return int(db.execute(sql, (notebook_id,)).fetchone()[0])

            return NotebookScaleFacts(
                one(
                    "SELECT COALESCE(SUM(file_size), 0) FROM sources WHERE notebook_id = ?"
                ),
                one("SELECT COUNT(*) FROM sources WHERE notebook_id = ?"),
                one("SELECT COUNT(*) FROM chunks WHERE notebook_id = ?"),
                one("SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id = ?"),
                one("SELECT COUNT(*) FROM knowledge_relations WHERE notebook_id = ?"),
            )

    def is_mounted_by_anyone(self, notebook_id: str) -> bool:
        """被任何笔记本当作参考库挂着(Task 6)—— NotebookScaleProfile.index_eligible
        的挂载分支消费。刻意不区分挂载边是否仍然「有效」(不走 mount_sql.py 的
        MOUNT_VALID_EXPR):那是解析参与集用的谓词,边失效通常是临时的,不该让
        索引跟着过期。IndexProjectionStore.is_mounted_by_anyone 是
        ScaleArtifactRuntime.eligible 侧的镜像实现,两处必须保持同一判定。"""
        with self.database.connect() as db:
            return bool(db.execute(
                "SELECT EXISTS(SELECT 1 FROM notebook_bases WHERE base_notebook_id=?)",
                (notebook_id,),
            ).fetchone()[0])
