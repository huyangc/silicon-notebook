from __future__ import annotations

import json
import logging
import shutil
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

from app.domain.indexing_pipeline import (
    BUILTIN_INDEXING_PIPELINE_VERSION,
    IndexingPipelineHostPort,
    IndexingPipelineOption,
)

from app.models.groups import GrantedGroupRef
from app.models.kg import KgBuildJobStatus
from app.models.notebooks import (
    NotebookAnalytics,
    NotebookCreate,
    NotebookRef,
    NotebookSummary,
    NotebookUpdate,
)
from app.models.ask import SEARCH_HIT_CAP, NotebookSearchResponse, SearchHit
from app.core import diagnostics_runtime as diagnostics
from app.core.query_syntax import strip_accepted_quote_markers
from app.repositories.group_rows import (
    fold_granted_notebook_groups,
    notebook_grant_confers_admin,
)
from app.repositories.ports import (
    IdentityStorePort,
    KgBuildJobStorePort,
    NotebookStorePort,
    QueryStorePort,
    RepositoryDatabasePort,
)
# Canonical implementation lives with the SourceFileStore (Task 11); the
# private alias keeps this module's delete_notebook cleanup call sites and
# historical importers unchanged.
from app.repositories.source_files import delete_source_file as _delete_source_file
from app.services.knowledge_contracts import USABLE_STATUSES


# Search has one canonical total-hit contract in app.models.ask. Memory is an
# additive search family and gets a smaller reserved candidate pool so it
# cannot evict every source/element/KG match from that total.
_MEMORY_SEARCH_HIT_CAP = 8
_SEARCH_HIT_EXCERPT_CHARS = 400
_log = logging.getLogger("silicon_notebook.notebook_catalog")


def _created_label(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = datetime.now()
    return f"{dt.year}年{dt.month}月{dt.day}日"


def _delete_notebook_asset_dir(storage_dir: Path, notebook_id: str) -> None:
    """Remove the whole per-notebook pasted-image-asset directory
    (``storage_dir/assets/<notebook_id>/`` — see
    ``app.services.knowhow.assets.AssetService.path_for`` for the same path
    formula).

    ``notebook_assets`` ROWS need no explicit delete here: the column is
    ``notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE``
    (migration 16) and every connection runs with ``PRAGMA foreign_keys = ON``
    (``SqliteDatabase._new_connection``), so they disappear automatically the
    moment ``delete_row_and_orphan_embeddings`` deletes the ``notebooks`` row
    above — verified empirically (knowhow-tables PR-2+3 Task 14): unlike
    ``knowledge_embeddings`` (which has no FK and needs the explicit delete a
    few lines up), ``notebook_assets`` cascades cleanly. Only the on-disk
    FILES need this explicit cleanup (cascade never touches the filesystem).
    Mirrors ``_delete_source_file``'s exists-before-touch tolerance so a
    notebook that never had an asset uploaded (directory never created)
    deletes cleanly."""
    asset_dir = Path(storage_dir) / "assets" / notebook_id
    if asset_dir.exists():
        shutil.rmtree(asset_dir, ignore_errors=True)


def _delete_notebook_source_files_dir(storage_dir: Path, notebook_id: str) -> None:
    """Remove the WHOLE per-notebook uploaded-source-files directory
    (``storage_dir/notebooks/<notebook_id>/`` — the exact same formula
    ``SourceFileStore.write_upload`` uses to compute ``source_dir``).

    P1（codex PR#659 round 3）: a directory-level ``rmtree``, not a per-file
    loop over DB-tracked paths (``delete_source_file`` on each
    ``notebook_delete_files``/``sources.file_path`` row — that is phase 4's
    job, and what this function's caller runs AFTER that already-complete
    sweep). The gap this closes: the notebook's exclusive claim (§4.3) only
    excludes an in-flight scale BUILD, never an in-flight upload/reparse — an
    upload that already passed ``get_notebook``'s existence check before the
    tombstone landed can still call ``SourceFileStore.write_upload`` (write
    the file) AFTER phase 4's sweep has already run and finished, landing a
    straggler file with a ``sources`` row that phase 3 already deleted (or
    that never got a chance to insert at all, if the row-insert itself later
    fails against the now-gone ``notebooks`` row — see
    ``SourceIngestionService.upload_sources``'s compensating-unlink fix for
    that half). Neither case leaves ANY row anywhere for a future cleanup
    pass to key off of, so this sweep is deliberately blind to rows — it
    just removes every file under this notebook's directory, tracked or not.
    Called once, AFTER phase 5's finalize transaction (or the residual path's
    terminal cleanup) commits — at that point ``notebooks`` (and every
    request-time ``get_row``/FK check gating a future write against it) is
    unconditionally gone, so no write racing this sweep can land AFTER it and
    still succeed; the only residual gap is a write whose file-write half
    completes concurrently with (or a few instructions before) this sweep's
    own ``rmtree`` — see the compensating-unlink fix in ``source_ingestion.py``
    for that narrower, self-healing tail. ``ignore_errors=True`` and an
    exists-before-touch guard: idempotent, safe to call unconditionally
    (a notebook whose directory was never created, or a residual-cleanup
    path where phase 4 already emptied everything, both no-op cleanly)."""
    source_dir = Path(storage_dir) / "notebooks" / notebook_id
    if source_dir.exists():
        shutil.rmtree(source_dir, ignore_errors=True)


def kg_build_status(row) -> KgBuildJobStatus | None:
    if row is None:
        return None
    return KgBuildJobStatus(
        job_id=row["id"],
        mode=row["mode"],
        status=row["status"],
        stage=row["stage"],
        total_sources=int(row["total_sources"]),
        completed_sources=int(row["completed_sources"]),
        failed_sources=int(row["failed_sources"]),
        error_code=row["error_code"],
        user_message=row["error_message"],
        updated_at=row["updated_at"],
    )


class NotebookSummaryQuery:
    """Cross-table NotebookSummary projection: knowledge-type counts, base-KG
    availability and pending-source aggregation over an open connection."""

    # object_type -> counts-dict key mapping shared by from_row's GROUP BY
    # aggregation (C5: N+1 fix — was 6 separate COUNT(*) queries per notebook,
    # one per object_type; a single GROUP BY object_type query gets all 6 in
    # one round trip, restricted to USABLE_STATUSES same as the old per-type
    # queries).  The facade re-exports this map as `_NOTEBOOK_COUNT_TYPES`.
    _NOTEBOOK_COUNT_TYPES: Dict[str, str] = {
        "rule": "rules", "case": "cases", "checklist": "checklist_items",
        "method": "methods", "risk": "risks", "glossary": "glossary",
    }

    def __init__(
        self,
        database: RepositoryDatabasePort,
        queries: QueryStorePort,
        kg_build_jobs: "KgBuildJobStorePort | None" = None,
        indexing_pipelines: "IndexingPipelineHostPort | None" = None,
    ) -> None:
        self.database = database
        self.queries = queries
        self.kg_build_jobs = kg_build_jobs
        self.indexing_pipelines = indexing_pipelines

    def _indexing_option_map(self) -> dict[str, IndexingPipelineOption]:
        """Resolve live availability once per summary/list projection."""
        builtin = IndexingPipelineOption(
            pipeline_id="",
            label="内建管线",
            description="",
            version=BUILTIN_INDEXING_PIPELINE_VERSION,
            overrides_chunking=False,
            overrides_kg_extraction=False,
            available=True,
        )
        options = {"": builtin}
        if self.indexing_pipelines is not None:
            options.update(
                (option.pipeline_id, option)
                for option in self.indexing_pipelines.options()
            )
        return options

    def count(
        self, db: object, table: str, column: str, value: str
    ) -> int:
        return self.queries.count_rows(db, table, column, value)

    def knowledge_type_counts(
        self, db: object, notebook_id: str
    ) -> Dict[str, int]:
        """{counts-dict key: count} for the 6 knowledge object_types
        from_row surfaces, via ONE GROUP BY query instead of 6 separate
        per-type COUNT(*) calls. Same USABLE_STATUSES filter and same
        zero-default for absent types as the old per-type COUNT(*) calls."""
        rows = self.queries.knowledge_type_count_rows(
            db, notebook_id, USABLE_STATUSES
        )
        by_type = {r["object_type"]: int(r["c"]) for r in rows}
        return {
            key: by_type.get(otype, 0)
            for otype, key in self._NOTEBOOK_COUNT_TYPES.items()
        }

    def has_kg(self, db: object, notebook_id: str) -> bool:
        return self.queries.notebook_has_kg(db, notebook_id)

    def visible_source_count(
        self, db: object, notebook_id: str
    ) -> int:
        """NotebookSummary's counts["sources"] — excludes Memory-derived and
        Knowhow-table synthetic sources (source_type IN ('memory', 'knowhow')); see
        QueryStore.visible_source_count. The generic ``count`` helper above
        stays unfiltered (it is a table-agnostic primitive shared with the
        facade's ``_count`` re-export / its equivalence-oracle test)."""
        return self.queries.visible_source_count(db, notebook_id)

    def count_pending_kg_sources(
        self, db: object, notebook_id: str
    ) -> int:
        """Count every physical parsed source without a complete KG extraction."""
        return self.queries.pending_kg_source_count(db, notebook_id)

    def visible_pending_kg_sources(
        self, db: object, notebook_id: str
    ) -> int:
        """Count user-visible parsed sources without a complete KG extraction.

        Memory-derived and Knowhow-table synthetic sources are excluded.
        """
        return self.queries.visible_pending_kg_source_count(db, notebook_id)

    def mounted_bases(
        self, notebook_id: str, db: "object | None" = None
    ) -> "tuple[list[NotebookRef], list[str]]":
        """(参考库列表, 其中**已建 KG 的库 id**) —— 一次查询同时供 NotebookSummary 的
        base_notebooks、base_kg_notebook_ids 与 base_kg_available,避免每条 summary 各查
        一次。未挂载 → ([], [])。

        第二项过去是 `any(has_kg)` 后的单个布尔;现在返回那批 id 本身,
        `base_kg_available` 退化成 `bool(...)`,取值逐字不变(空列表 falsy、非空 truthy),
        而前端得以按「本次勾选集 ∩ 带图库」判定严格推理是否真的取得到图。

        零新增查询:`mounted_bases_row` 的**每一行本来就带 has_kg**(见两侧 QueryStore 的
        SQL:`EXISTS(... knowledge_objects ... ko.notebook_id = b.id) AS has_kg`),这里
        只是不再把它 any(...) 掉。"""
        if db is not None:
            rows = self.queries.mounted_bases_row(db, notebook_id)
        else:
            with self.database.connect() as conn:
                rows = self.queries.mounted_bases_row(conn, notebook_id)
        refs = [
            NotebookRef(id=r["id"], name=r["name"], tier=r["tier"] or "personal")
            for r in rows
        ]
        return (refs, [str(r["id"]) for r in rows if bool(r["has_kg"])])

    def from_row(
        self,
        connection: object,
        row: Any,
        *,
        memory_count: int = 0,
        indexing_options: Mapping[str, IndexingPipelineOption] | None = None,
    ) -> NotebookSummary:
        # 注意:kg_building/paper_meta_backfilling 仅经 get(kg_building=...,
        # paper_meta_backfilling=...) 回填为真值;list_for_user 等走 from_row 的
        # 路径恒为 False（当前无消费方读列表里的该字段）。
        counts = {
            "sources": self.visible_source_count(connection, row["id"]),
            "memories": memory_count,
            **self.knowledge_type_counts(connection, row["id"]),
        }
        keys = row.keys()

        def _list(field: str) -> List[str]:
            if field not in keys or not row[field]:
                return []
            try:
                value = json.loads(row[field])
                return [str(v) for v in value] if isinstance(value, list) else []
            except (json.JSONDecodeError, TypeError):
                return []

        # 一次读取,两种投影:布尔 base_kg_available 与它的分解 base_kg_notebook_ids。
        # 自洽(非空 ⟺ 为真)因此是构造性的 —— 不存在两个独立求值的机会。
        base_refs, base_kg_ids = self.mounted_bases(row["id"], connection)
        options = indexing_options or self._indexing_option_map()
        desired_id = str(row["indexing_pipeline"] or "") if "indexing_pipeline" in keys else ""
        desired_version = (
            str(row["indexing_pipeline_version"] or BUILTIN_INDEXING_PIPELINE_VERSION)
            if "indexing_pipeline_version" in keys
            else BUILTIN_INDEXING_PIPELINE_VERSION
        )
        published_id = (
            str(row["_published_pipeline_id"] or "")
            if "_published_pipeline_id" in keys
            else ""
        )
        published_version = (
            str(
                row["_published_pipeline_version"]
                or BUILTIN_INDEXING_PIPELINE_VERSION
            )
            if "_published_pipeline_version" in keys
            else BUILTIN_INDEXING_PIPELINE_VERSION
        )
        job_id = (
            str(row["indexing_pipeline_job_id"] or "")
            if "indexing_pipeline_job_id" in keys
            else ""
        )
        selected = options.get(desired_id)
        selected_version = selected.version if selected is not None else desired_version
        stale = (
            desired_id != published_id
            or desired_version != selected_version
            or selected_version != published_version
        )
        return NotebookSummary(
            id=row["id"],
            name=row["name"],
            purpose=row["purpose"],
            primary_domain=row["primary_domain"],
            status=row["status"],
            counts=counts,
            created_label=_created_label(row["created_at"]),
            target_users=row["target_users"] if "target_users" in keys else "",
            expected_questions=_list("expected_questions"),
            source_types=_list("source_types"),
            taxonomy=_list("taxonomy"),
            access_scope=row["access_scope"] if "access_scope" in keys else "",
            tier=row["tier"] if "tier" in keys else "personal",
            kg_ready=self.has_kg(connection, row["id"]),
            base_kg_available=bool(base_kg_ids),
            base_kg_notebook_ids=base_kg_ids,
            base_notebooks=base_refs,
            kg_pending_sources=self.visible_pending_kg_sources(
                connection, row["id"]
            ),
            # 「已分享」徽标的口径是**这本库不只我一个人能看**,所以它是两件事的并集:
            # 只读共享(`notebooks.is_shared` 这一列)**或**存在指向某个群组的授权边
            # (`_shared_to_groups`,由 summary/owned 两条行查询顺带带回,零新增往返)。
            # 少了后者,owner 会看到一本「没有分享过」的库其实整组人可读(P1-T4)。
            #
            # ⚠ 覆盖面按查询而不是按角色,如实说清:**列表**投影里成员那两条查询
            # (`joined_notebook_rows` / `granted_notebook_rows`)不带 `_shared_to_groups`,
            # 所以成员在列表上看不到「群组共享」那一半;但它们各自 `SELECT nb.*`,
            # `notebooks.is_shared` 那一列成员本来就看得到(只读共享一开,他那张卡的
            # `is_shared` 就是 True)。**详情**路径不分角色——`summary_notebook_row` 带这
            # 一列,reader 打开详情同样拿到并集,这与 `NotebookSummary.is_shared` 字段
            # 注释早就写明的「reader 看到的原库 is_shared 也为 True」一致。
            is_shared=(
                (bool(row["is_shared"]) if "is_shared" in keys else False)
                or ("_shared_to_groups" in keys and bool(row["_shared_to_groups"]))
            ),
            indexing_pipeline_id=desired_id or None,
            indexing_pipeline_version=selected_version,
            indexing_pipeline_available=bool(selected and selected.available),
            indexing_pipeline_missing=bool(desired_id and selected is None),
            indexing_pipeline_pending=bool(job_id) or stale,
            indexing_pipeline_stale=stale,
        )

    def _fill_viewer_relation(
        self,
        db: object,
        summary: NotebookSummary,
        row: Any,
        user_id: "str | None",
    ) -> None:
        """单库详情的 `access` / `shared_from` / `granted_via`(群组知识共享 P1-T4)。

        **修的是一个长期缺口**:详情投影从来没回填过 `access`,于是它永远是模型默认的
        `"owner"`——工作区顶栏那整段 reader 分支(只读徽章、退出共享、群组来源)对着
        详情响应从来没有为真过。列表投影一直是对的,所以这个缺口只在「打开一本别人
        共享给我的库」时暴露,而那正是本特性的主场景。

        判据与列表投影同口径,且刻意**不新增授权判定**:

        * `access` 只比 `created_by` —— 走到这里的请求已经过了路由上的读守卫
          (`require_notebook_read` = owner ∪ 只读成员 ∪ 有效授权边),所以「不是
          owner」就等于「有读权的 reader」。在这里重算一遍读权,等于把权限判定复制成
          两份,而两份迟早会不一致(真正的权威是那道守卫,不是这段投影)。
        * `shared_from` 取 `_owner_username` —— 由 `summary_notebook_row` 的 LEFT JOIN
          随行带回,零新增往返。
        * `granted_via` 的去重口径与列表**完全一致**:成员行优先——有只读成员行就把
          `granted_via` 留空。两条点查都是列表那两条查询本身,只是加了 notebook
          过滤——谓词只有一份。

          交叉态(既经分享链接加入、又在被授权的群组里)必须落在成员行那一支:他手上
          的「退出共享」删的正是那条成员行,是一个**真的有效**的动作;按群组来源把它
          藏起来等于拿走一个能用的出口。删掉成员行之后,群组授权接管,同一本库改带
          来源标注——列表与详情同时切换,不会一处一副面孔。

        * `can_manage_content`(P2-T2)由授权边行自己的 `_grant_role` 判定,与列表侧
          **同一个派生规则**(`notebook_grant_confers_admin`)。⚠ 它**必须**在成员行
          那一支之外单独算:管理权是权限,而「成员行优先」只是 `granted_via` 的展示
          去重。交叉态的组管理员一样能写,把这个布尔一起藏进那条 return 会让列表说
          「可管理」、详情说「只读」——同一本库两副面孔,正是上一段要防的东西。

        ⚠ 两条点查的**顺序被 P2-T2 掉了个个**:先 granted、后 joined。语义逐字不变
        (`granted_via` 仍是成员行优先)。查询次数**不是**逐形态不变——两个形态各挪了
        一次,是拿 `_grant_role` 必须付的代价(P2-T2 评审 P2-6 订正):
          * owner:+0(最常见的那条路,提前 return);
          * 只读共享(有成员行、无授权边):granted 点查为空 → 直接 return,+1
            (与改前相同——改前也是 joined 命中即 return);
          * **everyone 只读**(公共库,既无成员行也无授权边):granted 点查为空 →
            return,+1。**改前是 +2**(先 joined 落空、再 granted 落空),这里省了一次
            (**−1**)——granted 先跑、一空就走,不再白问一次 joined;
          * 群组共享(有授权边、无成员行):granted 非空 → 再 joined 一次(落空)→ +2
            (与改前相同);
          * **交叉态**(既有成员行、又有授权边):granted 非空 → joined 命中 → +2。
            **改前是 +1**(先 joined 命中即 return,根本不查 granted),这里**多付一次
            (+2)**——因为 `can_manage_content` 必须从 granted 行的 `_grant_role` 算,
            而交叉态恰恰要在成员行压掉 `granted_via` **之前**先拿到那个 role,躲不开。
        净效果是「everyone 只读 −1、交叉态 +1」的对调,不是零变化;交叉态那 +1 是硬成本
        (拿不到 `_grant_role` 就没法给交叉态的组管理员画写入口),everyone 那 −1 是顺带
        白赚的。原来的顺序(先 joined 再 granted)在交叉态上永远拿不到 `_grant_role`。
        """
        if not user_id:
            return
        keys = row.keys()
        owner_id = row["created_by"] if "created_by" in keys else None
        if owner_id == user_id:
            summary.access = "owner"
            summary.can_manage_content = True
            return
        summary.access = "reader"
        summary.shared_from = (
            row["_owner_username"] or "" if "_owner_username" in keys else ""
        )
        granted = self.queries.granted_notebook_rows(
            db, user_id, notebook_id=summary.id
        )
        if not granted:
            return
        summary.can_manage_content = any(
            notebook_grant_confers_admin(item) for item in granted
        )
        if self.queries.joined_notebook_rows(db, user_id, notebook_id=summary.id):
            return
        summary.granted_via = [
            GrantedGroupRef(**item)
            for item in fold_granted_notebook_groups(
                [
                    {
                        "notebook_id": item["id"],
                        "group_id": item["_group_id"],
                        "group_name": item["_group_name"],
                        "group_kind": item["_group_kind"],
                    }
                    for item in granted
                ]
            ).get(summary.id, [])
        ]

    def get(
        self,
        notebook_id: str,
        *,
        kg_building: bool = False,
        paper_meta_backfilling: bool = False,
        user_id: str | None = None,
    ) -> NotebookSummary:
        """status='copying' rows (copy_notebook's in-progress sentinel, P1-4)
        are treated as not-yet-existing: every catalog mutation guards with
        get(...) before acting, and a half-copied notebook must not be usable
        by any of them until the copy finishes."""
        indexing_options = self._indexing_option_map()
        with self.database.connect() as db:
            row = self.queries.summary_notebook_row(db, notebook_id)
            if row is None:
                raise KeyError(notebook_id)
            memory_counts = (
                self.queries.memory_counts_by_owner_notebook(db, user_id)
                if user_id is not None
                else {}
            )
            summary = self.from_row(
                db,
                row,
                memory_count=memory_counts.get((user_id, notebook_id), 0),
                indexing_options=indexing_options,
            )
            self._fill_viewer_relation(db, summary, row, user_id)
            # ask_available: 该库能否在任一模式下产出有据回答(见 NotebookSummary 字段注释)。
            # 判据对齐检索口径:KG 只算**可用状态**(USABLE_STATUSES,排除 deprecated),故不
            # 直接用 kg_ready/base_kg_available(含 deprecated),而用 usable 版查询。短路:
            # has_chunk 覆盖绝大多数库(可见来源 + knowhow 格子;可用活跃 KG 必有 chunk),
            # 放最前;usable-KG 查询用已算好的 kg_ready/base_kg_available 做便宜预过滤(为假
            # 则连查都免了);confirmed-memory 查询垫底。仅此单库路径回填;列表投影保持默认。
            #
            # 四个判据里**前三个是本地证据、第四个是参考库证据**,所以拆成两步求值:先短路
            # 求出本地那半(local_evidence_available),参考库那条只在本地为假时才查。
            # ask_available 的取值与语义**逐字不变**((a or b or d) or c ≡ a or b or c or d)。
            #
            # 零新增往返(效率是本仓库一等约束,这条路径就是「打开笔记本卡 5-6 秒」的现场):
            #   * 前三个分支本来就要为 ask_available 求值,结果直接复用,不重查。
            #   * confirmed-memory 那条新加了一个**已在手**的便宜预过滤 counts["memories"]
            #     —— 它是上面 memory_counts_by_owner_notebook 的结果(本函数无条件已查),
            #     按 (created_by, notebook_id) 分组的**全状态** COUNT(*),是 confirmed 的
            #     严格超集:为 0 即绝无 confirmed,直接免掉那次 EXISTS。形态与 kg_ready /
            #     base_kg_available 给 usable-KG 查询做预过滤完全一致。
            #   * 唯一被交换的是 C 与 D 的先后。逐形态对账(A=has_chunk, B=usable KG,
            #     C=usable base KG, D=confirmed memory):有 chunk 的库(绝大多数)恒 1 次查询,
            #     与改前一致;最坏情形仍是 4 次,与改前一致;「零 memory 行 + 需要走到 D」的
            #     库(如刚建的空库)由新预过滤**省下** 1 次;「c 真且 d 假」的库多付 1 次 D
            #     —— 那是为了如实回答 local_evidence_available 必须付的那一次(local 的取值
            #     由 D 决定,c 再真也代替不了它),且已被预过滤收窄到「本库对该用户有
            #     memory 行但一条都没确认」这一形态。
            local_evidence = bool(
                self.queries.notebook_has_chunk(db, notebook_id)
                or (
                    summary.kg_ready
                    and self.queries.notebook_has_usable_kg(db, notebook_id)
                )
                or (
                    int(summary.counts.get("memories", 0)) > 0
                    and user_id is not None
                    and self.queries.notebook_has_confirmed_memory(
                        db, notebook_id, user_id
                    )
                )
            )
            summary.local_evidence_available = local_evidence
            summary.ask_available = local_evidence or bool(
                summary.base_kg_available
                and self.queries.notebook_has_usable_base_kg(db, notebook_id)
            )
            # paper_meta_missing:「补全论文信息」按钮的显示门(见字段注释)。已在手的
            # 可见来源数是合规候选的严格超集(两者同排 memory/knowhow 合成源),为 0
            # 直接免掉 EXISTS 探针;仅此单库路径回填,列表投影保持默认 None(=未计算,
            # 前端按旧行为继续显示按钮)。
            summary.paper_meta_missing = bool(
                int(summary.counts.get("sources", 0)) > 0
                and self.queries.notebook_paper_meta_missing(db, notebook_id)
            )
            job_row = (
                self.kg_build_jobs.latest_on(db, notebook_id)
                if self.kg_build_jobs is not None
                else None
            )
        summary.kg_build = kg_build_status(job_row)
        summary.kg_building = kg_building or (
            summary.kg_build is not None
            and summary.kg_build.status == "running"
        )
        summary.paper_meta_backfilling = paper_meta_backfilling
        return summary

    def list_for_user(self, user_id: str) -> list[NotebookSummary]:
        """自有库(access=owner)∪ 经只读共享加入的库 ∪ 经**群组授权边**可读的库。

        后两者都是 `access="reader"`(已定裁决 7:P1 不给 `access` 加枚举值),区别
        由 `granted_via` 表达——非空即「来自群组《X》」。

        status='copying' 是 copy_notebook 分批写入期间的哨兵状态(P1-4),半拷贝
        的副本必须排除,不然用户能看到/点进一个字段还没写全的空壳 notebook。

        **去重按 id、成员行优先**:同一本库可以既在只读成员清单里、又被共享给我所在
        的组;三段按 owner → 成员 → 群组的顺序追加,已出现过的 id 直接跳过。判据放在
        这里而不是写进第三条 SQL 的 `NOT EXISTS`,是因为「已经产出了哪些库」这份事实
        本来就在手上,再用 SQL 算一遍就是同一判据的第二份拷贝。

        `can_manage_content`(P2-T2)零新增查询:owner 那段恒 True;另外两段查
        `admin_ids` —— 它由**去重之前**的全部授权边行折出来,所以交叉态(既有成员行、
        又持管理边)的库落在成员那一段时照样为真。⚠ 从 `out` 里逐条判而不是只在群组
        那一段赋值,正是为了这一条:被 `seen` 跳过的授权边行仍然携带权限事实,而它的
        `granted_via`(纯展示)才是该被成员行压掉的那一半。
        """
        out: List[NotebookSummary] = []
        seen: set[str] = set()
        indexing_options = self._indexing_option_map()
        with self.database.connect() as db:
            memory_counts = self.queries.memory_counts_by_owner_notebook(db, user_id)
            rows = self.queries.owned_notebook_rows(db, user_id)
            for row in rows:
                nb = self.from_row(
                    db,
                    row,
                    memory_count=memory_counts.get((user_id, row["id"]), 0),
                    indexing_options=indexing_options,
                )
                nb.access = "owner"
                nb.can_manage_content = True
                seen.add(nb.id)
                out.append(nb)
            joined = self.queries.joined_notebook_rows(db, user_id)
            for row in joined:
                nb = self.from_row(
                    db,
                    row,
                    memory_count=memory_counts.get((user_id, row["id"]), 0),
                    indexing_options=indexing_options,
                )
                nb.access = "reader"
                nb.shared_from = row["_owner_username"] or ""
                seen.add(nb.id)
                out.append(nb)
            granted = self.queries.granted_notebook_rows(db, user_id)
            admin_ids = {
                row["id"] for row in granted if notebook_grant_confers_admin(row)
            }
            groups_by_notebook = fold_granted_notebook_groups(
                [
                    {
                        "notebook_id": row["id"],
                        "group_id": row["_group_id"],
                        "group_name": row["_group_name"],
                        "group_kind": row["_group_kind"],
                    }
                    for row in granted
                ]
            )
            for row in granted:
                notebook_id = row["id"]
                if notebook_id in seen:
                    continue
                seen.add(notebook_id)
                nb = self.from_row(
                    db,
                    row,
                    memory_count=memory_counts.get((user_id, notebook_id), 0),
                    indexing_options=indexing_options,
                )
                nb.access = "reader"
                nb.shared_from = row["_owner_username"] or ""
                nb.granted_via = [
                    GrantedGroupRef(**item)
                    for item in groups_by_notebook.get(notebook_id, [])
                ]
                out.append(nb)
        for nb in out:
            if nb.id in admin_ids:
                nb.can_manage_content = True
        return out


class NotebookCatalogService:
    """Notebook catalog orchestration over the row store, the summary
    projection and the Task-7 query adapter.  Owns the in-process
    kg_building flag set (进程内; 重启后天然为空=未构建, 无需 reconcile) that
    get_notebook reflects into NotebookSummary.kg_building. Mirrors the same
    reflect-into-summary wiring for paper_meta_backfilling, sourced from the
    injected source_ingestion service's own in-process dict (see
    NotebookSummary.paper_meta_backfilling)."""

    def __init__(
        self,
        store: NotebookStorePort,
        summaries: NotebookSummaryQuery,
        queries: QueryStorePort,
        identity: IdentityStorePort,
        storage_dir: Callable[[], Path],
        analysis_artifacts: Any = None,
    ) -> None:
        """``storage_dir`` is a zero-arg callable resolving the LIVE storage
        root (knowhow-tables PR-2+3 Task 14) — a callable rather than a Path
        snapshot because the facade's ``storage_dir`` is a mutable property
        (tests monkeypatch it per instance), exactly the convention
        ``NotebookCopyService`` already uses for the same collaborator.
        ``delete_notebook`` reads it to sweep the notebook's pasted-image
        asset directory; injected at construction so EVERY caller gets the
        cleanup — routes reach this service directly via
        ``deps.notebook_catalog_repository()`` (``repo._runtime.catalog``),
        never through the facade delegate."""
        self._store = store
        self._summaries = summaries
        self._queries = queries
        self._identity = identity
        self._storage_dir = storage_dir
        self._analysis_artifacts = analysis_artifacts
        self.kg_building: set = set()
        # Injected post-construction by RepositoryRuntime.wire_source_ingestion()
        # once SourceIngestionService exists (mirrors memory_retriever below —
        # this class is constructed before source ingestion is wired). Held as
        # a WEAKREF, not a strong ref: SourceIngestionService closes over the
        # facade (`self._write`, `self.source_elements`, ...) for its own
        # wiring, so a strong ref here would let anything that holds `catalog`
        # (e.g. ScaleArtifactRuntime, which callbacks into
        # catalog.get_notebook) transitively keep the whole facade alive —
        # exactly what test_scale_artifact_runtime's retention tests guard
        # against. The service outlives every real request, so the weakref
        # is always live in practice; it only reads as dead in that same
        # deliberate GC/retention test.
        self.source_ingestion: "weakref.ReferenceType | None" = None
        self.memory_retriever = None

    def _paper_meta_backfilling(self, notebook_id: str) -> bool:
        service = self.source_ingestion() if self.source_ingestion is not None else None
        return False if service is None else service.paper_meta_backfilling(notebook_id)

    def list_notebooks(self) -> list[NotebookSummary]:
        return self._summaries.list_for_user(self._identity.current_user().id)

    def warm_open_path_caches(self, progress=None) -> int:
        """Prime the per-process open-path count caches (``knowledge_counts_cache``
        — the per-type GROUP BY, the pending-source correlated count and the chunk
        count) for every notebook so the first login after a restart is served
        warm instead of paying the cold recompute. Called by the startup-readiness
        warm-up; best-effort per notebook inside ``warm_all``. Returns the count."""
        return self._queries.warm_open_path_caches(progress)

    def _fill_document_limit(
        self, summary: NotebookSummary, notebook_id: str
    ) -> NotebookSummary:
        """回填 owner 的「每笔记本文档数量上限」有效值(前端来源面板据此 + 来源列表
        total_count 显示「文档 X / 上限」)。仅详情/创建路径回填;列表投影保持默认。"""
        owner_id, _owner_role = self._identity.notebook_owner(notebook_id)
        summary.document_limit = self._identity.effective_document_limit(owner_id)
        return summary

    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary:
        user_id = self._identity.current_user().id
        notebook_id = self._store.create_row(payload, user_id)
        return self._fill_document_limit(
            self._summaries.get(notebook_id, user_id=user_id), notebook_id
        )

    def get_notebook(self, notebook_id: str) -> NotebookSummary:
        return self._fill_document_limit(
            self._summaries.get(
                notebook_id,
                kg_building=notebook_id in self.kg_building,
                paper_meta_backfilling=self._paper_meta_backfilling(notebook_id),
                user_id=self._identity.current_user().id,
            ),
            notebook_id,
        )

    def update_notebook(
        self, notebook_id: str, payload: NotebookUpdate
    ) -> NotebookSummary:
        self.get_notebook(notebook_id)  # raises KeyError if missing
        self._store.update_row(notebook_id, payload)
        return self.get_notebook(notebook_id)

    def delete_notebook(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)  # raises KeyError if missing
        with diagnostics.diagnostic_phase("notebook_delete.db"):
            file_paths = self._store.delete_row_and_orphan_embeddings(notebook_id)
        # DB deletion is committed above; only then remove files on disk —
        # source files first, then the notebook's pasted-image asset
        # directory (knowhow-tables PR-2+3 Task 14; unconditional, from the
        # construction-injected live storage root, so the real HTTP delete
        # route — which reaches this service directly, not via the facade —
        # gets the cleanup too).
        with diagnostics.diagnostic_phase("notebook_delete.files"):
            analysis_artifacts = getattr(self, "_analysis_artifacts", None)
            if analysis_artifacts is not None:
                try:
                    analysis_artifacts.redact_notebook(
                        notebook_id, occurred_at=datetime.now(timezone.utc).isoformat()
                    )
                except Exception as exc:  # noqa: BLE001 - database deletion committed
                    _log.warning(
                        "analysis artifact redaction failed (%s)", type(exc).__name__
                    )
            for file_path in file_paths:
                _delete_source_file(file_path)
            _delete_notebook_asset_dir(self._storage_dir(), notebook_id)

    def mark_notebook_base(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)  # raises KeyError if missing
        self._store.set_tier(notebook_id, "base")

    def set_notebook_personal(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)  # raises KeyError if missing
        self._store.set_tier(notebook_id, "personal")

    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics:
        return self._queries.notebook_analytics(notebook_id)

    def search_notebook(
        self, notebook_id: str, query: str
    ) -> NotebookSearchResponse:
        # This search never tokenizes — it is already a whole-string substring
        # match, i.e. exactly what quoting asks for elsewhere. So the only thing
        # the markers can do here is make the pattern unmatchable: nothing in
        # the corpus contains the `"` the user typed. Dropping them keeps one
        # syntax across the product instead of a box where it silently finds
        # nothing.
        needle = strip_accepted_quote_markers(query)
        response = self._queries.search_notebook(notebook_id, needle)
        if self.memory_retriever is None:
            return response
        # Memory keeps the ORIGINAL query: its retriever is phrase-aware and
        # strips the markers itself for its own candidate probe. Handing it the
        # stripped needle would delete the constraint before the scorer sees it,
        # letting a memory that merely scatters those words qualify as though
        # the user had never quoted anything. (codex #410 round-2 P2)
        memories = self.memory_retriever.notebook_memory_hits(
            self._identity.current_user().id,
            notebook_id,
            query,
            _MEMORY_SEARCH_HIT_CAP,
        )
        memory_hits = [SearchHit(
            scope="Memory",
            notebook_id=notebook_id,
            label=item.title,
            text=item.text[:_SEARCH_HIT_EXCERPT_CHARS],
            source_id="",
            element_id="",
            memory_id=item.memory_id,
            provenance=dict(item.provenance),
        ) for item in memories]
        if not memory_hits:
            return response
        return NotebookSearchResponse(
            query=response.query,
            hits=[
                *response.hits[: max(0, SEARCH_HIT_CAP - len(memory_hits))],
                *memory_hits,
            ][:SEARCH_HIT_CAP],
        )
