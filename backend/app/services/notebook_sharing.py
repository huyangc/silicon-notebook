from __future__ import annotations

import logging
import secrets
import shutil
from pathlib import Path
from typing import Callable, Iterable, TYPE_CHECKING

from app.models.notebooks import NotebookSummary
from app.domain.repository import RepositoryCompatibilitySeams
from app.repositories.ports import (
    AgentObservationStorePort,
    AgentProfileStorePort,
    AskStateStorePort,
    NotebookTooLargeToCopyError,
    RepositoryDatabasePort,
    SharingStorePort,
)
from app.services.knowhow.assets import ALLOWED_MIME_EXTENSIONS
from app.services.notebook_catalog import NotebookCatalogService, NotebookSummaryQuery
from app.services.notebook_copy_writer import NotebookCopyRun, NotebookCopyWriter, SnapshotRowSource

_log = logging.getLogger("silicon_notebook.sharing")


class NotebookCopyService:
    """Deep-copy orchestration: the copy run around ``NotebookCopyWriter``
    (``notebook_copy_writer``, which remaps and writes the rows through the
    store in chunked transactions), the filesystem copy and the compensation
    ordering.

    Compatibility seams are read during EVERY operation — ``seams.new_id()``
    (sqlite_repository._new_id), ``seams.copy_chunk_size()``
    (sqlite_repository._COPY_CHUNK) and ``seams.remap_json_ids`` — so patches
    applied after repository construction stay authoritative, and the per-row
    insert seat (facade ``_insert_row``) is honoured inside the store. The
    writer is built per copy from the CURRENT ``self._store``/``self._seams``,
    so the same holds for it.

    ``schedule_projection`` (PR-2+3 Task 13) is the facade's
    ``knowhow_api.get_scheduler(repo).schedule`` callable — copy_notebook
    calls it once per copied knowhow table, right after publish, so the
    structural KO/edge graph gets rebuilt (dynamic column-name types) without
    the caller ever blocking on it (same debounced background scheduler every
    editing endpoint already goes through — see ``ProjectionScheduler``).
    """

    def __init__(
        self,
        *,
        store: SharingStorePort,
        catalog: NotebookCatalogService,
        seams: "RepositoryCompatibilitySeams",
        storage_dir: Callable[[], Path],
        schedule_projection: Callable[[str], None],
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._seams = seams
        self._storage_dir = storage_dir
        self._schedule_projection = schedule_projection

    def sweep_stuck_copies(self, created_by: "str | None" = None) -> int:
        """批 3·W1 PR-4：收割过期拷贝哨兵行,并连带清掉每本的两棵磁盘目录。

        进程内的拷贝失败补偿（``copy_notebook`` 的 except 分支）本来就
        rmtree 目的地的 ``notebooks/<id>`` 与 ``assets/<id>``——但进程死在
        补偿之前的半拷贝只有本清扫收割其行,文件在 PR-4 之前无人清、按崩溃
        次数静默泄漏。store 现在返回实际收割的 id 列表,逐本 best-effort
        整目录删除（幂等,目录不存在即 no-op),对外仍返回计数。

        内评 P1(PR-4):``copy_notebook`` 开场的收割**必须**走本方法而不是
        直调 store——那是收割的唯一生产触发点,直调会把回传 ids 丢掉、只删
        行不清盘,恰好复刻上面说的泄漏。接线由 ``test_copy_notebook_
        entrypoint_reaps_stale_disk_too`` 钉住。

        codex #666 R7 P2:清盘走 ``on_reaped`` 逐本回调而不是等整表收尾的
        返回列表——后面某本的事务抛错时,已提交的行没了,目录若还没清就
        再也没有清扫路径轮得到它(行级清扫按行找目录)。回调在每本事务提交
        后立即执行,异常照旧上抛,但已提交的本决不漏盘。"""
        cleaned = {"n": 0}

        def _clean_disk(notebook_id: str) -> None:
            cleaned["n"] += 1
            shutil.rmtree(
                self._storage_dir() / "notebooks" / notebook_id,
                ignore_errors=True,
            )
            shutil.rmtree(
                self._storage_dir() / "assets" / notebook_id,
                ignore_errors=True,
            )

        self._store.sweep_stale_copies(created_by=created_by, on_reaped=_clean_disk)
        return cleaned["n"]

    def copy_notebook(
        self,
        source_notebook_id: str,
        *,
        new_owner_id: str,
        actor_label: "str | None" = None,
        new_name: "str | None" = None,
    ) -> NotebookSummary:
        """Deep-copy a notebook with remapped IDs and a hidden copy sentinel.

        Phase contract (frozen): sweep only stale own copies → copy source
        directory → insert copying sentinel → copy each table in configured
        chunks → validate counts and references → publish original status.
        Failure compensates ONLY the destination rows/files.

        This method only assembles the run: sweep → snapshot → row source →
        writer → validate/publish → projections. ``NotebookCopyWriter`` writes
        the tables in FK-safe order from a ``SnapshotRowSource``, which gives each
        table as one batch, so there is one ``insert_copy_rows`` per table; each
        phase fills the remap maps on ``NotebookCopyRun`` that the later phases
        read. Every file on disk (the source directory, the asset files, the
        compensating rmtree) stays here.
        """
        self.sweep_stuck_copies(new_owner_id)
        source_notebook = self._catalog.get_notebook(source_notebook_id)
        new_id = self._seams.new_id("nb")
        now = self._seams.now()
        chunk_size = self._seams.copy_chunk_size()
        source_dir = self._storage_dir() / "notebooks" / source_notebook_id
        run = NotebookCopyRun(
            source_notebook_id=source_notebook_id,
            source_notebook=source_notebook,
            new_owner_id=new_owner_id,
            new_id=new_id,
            now=now,
            chunk_size=chunk_size,
            destination_dir=self._storage_dir() / "notebooks" / new_id,
            assets_dest_dir=self._storage_dir() / "assets" / new_id,
        )
        try:
            if source_dir.exists():
                shutil.copytree(source_dir, run.destination_dir)
                run.copied_files = True

            # snapshot_copy_rows enforces the copyable-row bound ATOMICALLY inside
            # its own stable-snapshot read transaction (raises
            # NotebookTooLargeToCopyError before materialising any rows), so a
            # concurrent ingestion cannot slip an over-limit notebook past the
            # cached pre-checks into the fetchall (codex PR#353 r3). copytree
            # above is disk, not memory; the except below rmtree's it on a raise.
            snapshot = self._store.snapshot_copy_rows(source_notebook_id)

            NotebookCopyWriter(
                store=self._store,
                seams=self._seams,
                rows=SnapshotRowSource(snapshot),
                run=run,
                materialize_assets=lambda files: self._copy_asset_files(run, files),
            ).write(new_name=new_name, actor_label=actor_label)

            self._store.validate_copy(source_notebook_id, new_id)
            self._store.publish_copy(new_id, source_notebook.status)
        except Exception:
            self._compensate_failed_copy(run)
            raise
        # Success-only path — publish_copy above flipped the sentinel off
        # 'copying', and compensate_copy can no longer reap the notebooks row
        # (its DELETE is `WHERE status = 'copying'` only, sharing_store.py).
        # NOTHING fallible past publish may live inside the try/except: a
        # raise here used to run compensation that deleted the published
        # copy's chunks_fts/kg_objects_fts/knowledge_embeddings rows and
        # rmtree'd its files while the (already published) row SURVIVED — a
        # persistent, silently-corrupted copy sweep_stale_copies never reaps.
        self._schedule_copied_projections(run)
        return self._catalog.get_notebook(new_id)

    def _compensate_failed_copy(self, run: NotebookCopyRun) -> None:
        """Remove ONLY what this copy wrote: the destination rows, and the two
        destination directories if (and only if) this copy created them."""
        self._store.compensate_copy(run.new_id)
        if run.copied_files:
            shutil.rmtree(run.destination_dir, ignore_errors=True)
        if run.assets_copied:
            shutil.rmtree(run.assets_dest_dir, ignore_errors=True)

    def _copy_asset_files(
        self, run: NotebookCopyRun, asset_files: "list[tuple[str, str, str]]"
    ) -> None:
        """The files of one batch of copied ``notebook_assets`` rows
        (``(old_id, new_id, mime)``), from the source notebook's asset tree into
        the copy's — ``NotebookCopyWriter``'s ``materialize_assets``.

        ``run.assets_copied`` is set the moment the destination directory is
        created, before any file lands in it, so a failure mid-way through the
        files still has the directory removed by the compensation."""
        assets_src_dir = self._storage_dir() / "assets" / run.source_notebook_id
        run.assets_dest_dir.mkdir(parents=True, exist_ok=True)
        run.assets_copied = True
        for old_asset_id, new_asset_id, mime in asset_files:
            ext = ALLOWED_MIME_EXTENSIONS.get(mime, "bin")
            src_path = assets_src_dir / f"{old_asset_id}.{ext}"
            if src_path.is_file():
                shutil.copy2(src_path, run.assets_dest_dir / f"{new_asset_id}.{ext}")

    def _schedule_copied_projections(self, run: NotebookCopyRun) -> None:
        """PR-2+3 Task 13: schedule ONE structural reprojection per copied
        knowhow table, after publish.

        ``khtbl_map.values()`` are all NEW table ids (empty for a notebook with
        no knowhow tables, so this is a zero-cost no-op for the common case).
        The chunks/vectors already sitting in the copy are byte-identical to
        what project_table will independently recompute, so this rebuilds the
        KO/edge graph (dynamic column-name types) without a single additional
        embedder call — see element_id/cell_chunk_id in
        ``NotebookCopyWriter._copy_elements_and_chunks``. A scheduling failure (realistic:
        threading.Timer.start() RuntimeError under thread/fd exhaustion — this
        codebase has documented history there) is LOGGED, never compensated:
        the copy itself is complete and valid; the table's manual 重建投影
        button (or any later edit, which schedules the same full pass) covers
        a missed schedule.
        """
        for new_table_id in run.khtbl_map.values():
            try:
                self._schedule_projection(new_table_id)
            except Exception:  # noqa: BLE001 — published copy must survive a scheduling failure
                _log.warning(
                    "copy_notebook: 副本 %s 的 knowhow 表 %s 投影调度失败"
                    "（副本数据完整，可在表内手动重建投影）",
                    run.new_id,
                    new_table_id,
                    exc_info=True,
                )


class NotebookSharingService:
    """Sharing, membership and read-access orchestration over SharingStore.

    ``copy_stats`` is a late-bound callback to the facade's
    ``notebook_copy_stats`` (the cross-domain scale-profile memo) so instance
    patches and live Settings mutations keep being observed.  ``database`` +
    ``summaries`` hydrate join_shared's NotebookSummary over one connection,
    exactly like the former mixin's ``_notebook_from_row`` path.
    """

    def __init__(
        self,
        *,
        store: SharingStorePort,
        copies: NotebookCopyService,
        catalog: NotebookCatalogService,
        summaries: NotebookSummaryQuery,
        database: RepositoryDatabasePort,
        copy_stats: Callable[[str], dict],
        profiles: "AgentProfileStorePort | None" = None,
        observations: "AgentObservationStorePort | None" = None,
        ask_state: "AskStateStorePort | None" = None,
    ) -> None:
        self._store = store
        self._copies = copies
        self._catalog = catalog
        self._summaries = summaries
        self._database = database
        self._copy_stats = copy_stats
        # Agentic Memory P1 (T5): the member's private "understanding" overlay
        # for this notebook. Losing access is the one event that makes those
        # blocks orphaned data, so removal cleans them up here. ``None`` = not
        # wired (older composition roots / test doubles): membership removal
        # must never fail because of it.
        self._profiles = profiles
        # codex #535 R6 P2 (Agentic Memory P3): the member's Agent observation
        # queue for this notebook follows the same blank-slate contract as the
        # overlay blocks above — rows are derived entirely from that person's
        # own Agents' use of this library, so removal clears them too. ``None``
        # = not wired, same fail-open posture as ``profiles``.
        self._observations = observations
        # E7-4: the one read behind ``user_owns_answer`` (answer -> conversation
        # creator). ``None`` = not wired: the check then answers False, never True.
        self._ask_state = ask_state

    # ---------------------------------------------------------------- share
    def share_notebook(self, notebook_id: str) -> dict:
        self._catalog.get_notebook(notebook_id)  # raises KeyError if missing
        token = self._store.set_share_token(
            notebook_id, f"shr-{secrets.token_urlsafe(16)}"
        )
        stats = self._shown_copy_stats(notebook_id)
        return {
            "share_token": token,
            "copyable": stats["copyable"],
            "size": stats["size"],
        }

    def share_state(self, notebook_id: str) -> dict:
        """当前的分享链接状态(P1-T4)。**只读**:不铸 token、无 token 时不算规模。

        存在性检查刻意用 `notebook_row` 而不是 `self._catalog.get_notebook`:后者是
        一次完整的 summary 水合(逐类计数、挂载参考库、KG 探针……),而这里只需要
        「这本库在不在、它的 token 是什么」两个字段。路由上的 `notebook:manage` 守卫
        已经解析过这本库,这里再水合一遍纯属白付。
        """
        row = self._store.notebook_row(notebook_id)
        if row is None:
            raise KeyError(notebook_id)
        token = str(row["share_token"] or "") if row["is_shared"] else ""
        if not token:
            # 没有链接就没有「可不可拷贝 / 多大」这回事 —— 也不为它跑一次规模统计。
            return {"share_token": "", "copyable": False, "size": {}}
        stats = self._shown_copy_stats(notebook_id)
        return {
            "share_token": token,
            "copyable": stats["copyable"],
            "size": stats["size"],
        }

    def unshare_notebook(self, notebook_id: str) -> None:
        self._catalog.get_notebook(notebook_id)  # raises KeyError if missing
        # Unsharing drops every membership row too, but it withdraws an
        # authorisation; it is not the members leaving. Their Memory stays —
        # its owner can only read it with notebook read access, which they no
        # longer hold, and it comes back if they are let in again. Only a
        # member's own acknowledged exit (``MemoryService.leave_notebook``)
        # deletes Memory.
        #
        # Each removed member's private state goes exactly as in
        # ``remove_member`` (user ruling 2026-10-23: one semantics for the
        # single and the notebook-wide removal): the store returns who it
        # removed, in the same transaction that voided the token, and the
        # same per-member cleanup runs after the commit — membership first,
        # for the reason ``remove_member`` gives. A rejoin through a newly
        # minted link between the commit and the cleanup only resets that
        # person's just-started overlay, which is the rejoin contract anyway.
        # The cleanup takes the whole set at once: three write transactions
        # (job rows, blocks, observations) whatever the number of members.
        self._clear_member_profiles(notebook_id, self._store.clear_share(notebook_id))

    def find_notebook_by_share_token(self, token: str) -> "str | None":
        if not token:
            return None
        return self._store.find_by_token(token)

    def notebook_copy_stats(self, notebook_id: str) -> dict:
        # Share-routing copyability (copy vs read-only join — read by the
        # copy/join/preview routes and every share path below). ``self._copy_stats``
        # is the KG-version-cached bytes+chunks+nodes verdict; re-check the deep-copy
        # total-materialisation bound FRESH, because assets / sources / paper_meta
        # grow WITHOUT bumping that cache's version key. Otherwise a stale-copyable
        # notebook offers a copy that 409s at the guard while join rejects it as
        # small — a dead end (codex PR#354 r2 P2). Short-circuited to notebooks
        # already copyable by bytes+chunks+nodes (large libraries never pay the
        # count); retrieval reads scale_artifacts' cached stats directly and never
        # enters this path.
        stats = self._copy_stats(notebook_id)
        if stats.get("copyable") and not self._store.snapshot_copy_within_limits(notebook_id):
            stats = {**stats, "copyable": False}
        return stats

    def _shown_copy_stats(self, notebook_id: str) -> dict:
        """`notebook_copy_stats` for the surfaces that SHOW its size (the public
        preview, the share response, the owner's share dialog and overview).

        M2: those numbers exclude what a copy never carries — every member's Memory
        sources and the KG rows derived from them — so no field of the preview lets a
        link holder compute the Memory count by subtracting another (`source_count`
        already excludes them). Only these callers pay for it; the verdict-only
        callers (copy, join) and retrieval read the physical counts. A notebook
        without Memory pays one indexed probe."""
        stats = self.notebook_copy_stats(notebook_id)
        memory_sources, memory_nodes, memory_edges = self._store.memory_derived_kg_counts(
            notebook_id
        )
        size = stats.get("size")
        if (memory_sources or memory_nodes or memory_edges) and size:
            stats = {
                **stats,
                "size": {
                    **size,
                    "sources": max(0, size["sources"] - memory_sources),
                    "nodes": max(0, size["nodes"] - memory_nodes),
                    "edges": max(0, size["edges"] - memory_edges),
                },
            }
        return stats

    def shared_preview(self, notebook_id: str) -> dict:
        notebook = self._catalog.get_notebook(notebook_id)
        stats = self._shown_copy_stats(notebook_id)
        owner_display, titles = self._store.shared_preview_rows(notebook_id)
        return {
            "name": notebook.name,
            "owner_display": owner_display,
            "source_count": int(notebook.counts.get("sources", 0)),
            "node_count": stats["size"]["nodes"],
            "edge_count": stats["size"]["edges"],
            "source_titles": titles,
            "mode": "copy" if stats["copyable"] else "readonly",
            "size": stats["size"],
        }

    def shared_by_me(self, user_id: str) -> list:
        """owner 的「已分享」总览:只读共享 ∨ 共享给群组(P1-T4)。

        ⚠ **没有分享链接的行不算规模、不查成员**。`mode` / `size` / `members` 三个
        字段说的全是「这条**链接**是怎么回事」——纯群组共享的行没有链接,消费方也不
        渲染它们。而 `notebook_copy_stats` 每次都要新鲜复核一次深拷贝上限
        (`snapshot_copy_within_limits`,一次真实统计),`list_members` 又是一次查询:
        50 本只共享给群组的库会白付 100 次。所以这两笔只在真有 token 时才付。
        """
        out = []
        for row in self._store.list_shared_by_owner(user_id):
            token = row["share_token"] or ""
            if token:
                stats = self._shown_copy_stats(row["id"])
                readonly = not stats["copyable"]
                mode = "readonly" if readonly else "copy"
                size = stats["size"]
                members = self.list_members(row["id"]) if readonly else []
            else:
                # 中性值:没有链接就没有「可不可拷贝 / 多大 / 谁加入了」这回事。
                mode, size, members = "readonly", {}, []
            out.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "share_token": token,
                    "mode": mode,
                    "size": size,
                    "members": members,
                    # 共享给了几个**不同的**群组(P1-T4)。为 0 时这一行就是一条
                    # 纯只读共享,与改动前逐字一致;非 0 而 share_token 为空时,
                    # 这一行只因群组共享而存在——消费方据此不渲染分享链接。
                    "group_count": int(row["group_count"] or 0),
                }
            )
        return out

    # ----------------------------------------------------------------- copy
    def copy_notebook(
        self,
        source_notebook_id: str,
        *,
        new_owner_id: str,
        actor_label: "str | None" = None,
        new_name: "str | None" = None,
    ) -> NotebookSummary:
        # Fast early reject (defense-in-depth): the deep copy materialises every
        # table (objects / relations / chunks / all vector tables) into one
        # in-memory snapshot — 300GB+ at 8M-object scale. This cached copy_stats
        # check rejects an already-large source cheaply (before copytree) and
        # covers non-route callers. It does NOT need to be race-free: the
        # authoritative, race-proof bound is NotebookCopyService.copy_notebook's
        # within_copy_row_limit(), checked FRESH on the snapshot's own connection
        # immediately before the fetchall (OOM audit P2-7).
        if not self.notebook_copy_stats(source_notebook_id)["copyable"]:
            raise NotebookTooLargeToCopyError(
                f"notebook {source_notebook_id} is too large to deep-copy "
                f"(exceeds notebook_copy_max_bytes/rows); share read-only instead"
            )
        return self._copies.copy_notebook(
            source_notebook_id, new_owner_id=new_owner_id,
            actor_label=actor_label, new_name=new_name,
        )

    # -------------------------------------------------------- access guards
    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        """写权:仅 owner(安全边界,勿放宽)。"""
        return self._store.user_can_access_notebook(notebook_id, user_id)

    def user_owns_notebook_regardless_of_lifecycle(
        self, notebook_id: str, user_id: str
    ) -> bool:
        """`DELETE /api/notebooks/{id}` 依赖专属（codex #659 R6 P2）——见
        store 层同名方法的完整理由。消费点只有 `deps.require_notebook_
        delete` 与 `deps.require_notebook_cancel_delete`(删除窗口里主人撤销),
        其它任何调用点都不得复用它。"""
        return self._store.user_owns_notebook_regardless_of_lifecycle(
            notebook_id, user_id
        )

    def is_member(self, notebook_id: str, user_id: str) -> bool:
        return self._store.is_member(notebook_id, user_id)

    def user_can_admin_notebook(self, notebook_id: str, user_id: str) -> bool:
        """管理权:owner ∪ `role='admin'` 的有效授权边(P2 能力翻转,裁决 P2-1)。

        谓词的唯一定义点在两个后端的 `access_sql.NOTEBOOK_ADMIN_SQL`,这里一跳直接
        委托 store —— 与 `user_can_read_notebook` 同款,理由也一样(手写复刻不会跟随
        谓词扩展)。**不**取代 `user_can_access_notebook`:删库与 Agent/MCP 面仍恒 owner。
        """
        return self._store.user_can_admin_notebook(notebook_id, user_id)

    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool:
        """读权:owner ∪ 只读成员 ∪ 有效授权边(user/group/group_admins/everyone)。

        谓词的唯一定义点在两个后端的 `access_sql.NOTEBOOK_READ_SQL`,故这里一跳直接
        委托 store。曾经写成 `写权 or 成员`——语义相同但要发两次查询,而且是一份手写
        复刻;P1 群组授权扩展读权时,那份复刻不会跟随(正是这次委托要防的)。
        """
        return self._store.user_can_read_notebook(notebook_id, user_id)

    def notebook_sync_origin(self, notebook_id: str) -> str:
        """镜像来源标识(空 = 本地库)。目标端写入围栏的读侧,一跳直接委托 store。

        与上面三条访问守卫**并列而不同类**:那三条答「谁能写」,这一条答「这本库的
        内容还能不能被写」。围栏叠在能力守卫之上,所以这里不带任何权限判定。
        """
        return self._store.notebook_sync_origin(notebook_id)

    def user_can_read_source(self, source_id: str, user_id: str) -> bool:
        """Notebook read access AND, for a Memory source, being its creator.

        A Memory projection belongs to `memory_items.created_by`, not to the
        notebook: another member (the notebook owner included) must not read it
        through `/sources/{id}` or its elements. The owner gate is the SAME
        statement that resolves the notebook (`source_notebook_id(viewer_id=)`),
        so another member's Memory and a missing id both stop at that one read
        with ``None`` — nothing downstream can tell them apart.
        """
        notebook_id = self._store.source_notebook_id(source_id, viewer_id=user_id)
        return bool(notebook_id) and self.user_can_read_notebook(notebook_id, user_id)

    def user_owns_answer(self, answer_id: str, user_id: str) -> bool:
        """The answer belongs to ``user_id``: its conversation was created by
        them AND they can still read that notebook.

        An answer is its author's record, and its text may quote the author's
        private Memory, so reading the notebook is not enough to preview it,
        turn it into a Memory or rate it. An answer without a conversation (no
        creator to compare) is nobody's, so it is not owned either. Unknown ids,
        other members' answers and creatorless answers are indistinguishable
        (one statement, then ``False``). The NOTEBOOK owner does not own other
        members' answers. A deployment
        admin reading answers through the admin activity log is audit access
        and does not come through here.
        """
        if self._ask_state is None:
            return False
        notebook_id = self._ask_state.answer_notebook_id(answer_id, owned_by=user_id)
        return bool(notebook_id) and self.user_can_read_notebook(notebook_id, user_id)

    # ------------------------------------------------------------ membership
    # 测试观察点:直接写成员行的构造点(生产入会走分享令牌 join_shared),大量用例借它摆成员关系
    def add_member(self, notebook_id: str, user_id: str) -> None:
        return self._store.add_member(notebook_id, user_id)

    def remove_member(self, notebook_id: str, user_id: str) -> None:
        """Drop the membership row, then discard that member's private
        understanding overlay for this notebook.

        Agentic Memory P1 (T5). The blocks are derived ENTIRELY from that
        person's own use of this library, so the moment they lose access the
        rows are orphaned data about a notebook they can no longer open — and
        if access is ever granted again, starting from a blank slate is the
        correct behaviour, not a regression.

        This is defence in depth, not the access control: reading is already
        gated (injection takes an explicit owner, the API reads through the
        notebook read guard), so a row that outlived a removal would not be
        exposed. The physical delete is what makes "revoked" also mean "gone".

        Order matters — membership first. If the clear fails, the removal has
        still happened (an access change must never depend on a cleanup), and
        the read-side gate covers the leftover rows. The reverse order could
        delete a live member's notes and then leave them a member.

        The SHARED base is untouched by construction: ``clear_all`` scopes to
        one ``owner_id``, and ``user_id`` here is never the ``''`` sentinel.

        This method only removes the explicit per-user membership row — it
        does not, and cannot, revoke access a person still holds through a
        group grant (``notebook_grants`` with a group/group_admins/everyone
        principal). That authorization path does not run through here at
        all; whether the caller can still open this notebook after this call
        is decided entirely by the read-side gate
        (``user_can_read_notebook``/``NOTEBOOK_READ_SQL``), same as every
        other access decision in this service. If a group grant still gives
        them read access, this call still resets their overlay — that is a
        blank slate, not a leak, and is the same "start over" outcome as any
        other rejoin.

        Never deletes Memory. Ending someone's membership is an access change
        like revoking a grant: their Memory stays, readable by nobody while
        they cannot read the notebook, and returns if they are let back in.
        The one path that deletes a member's Memory is their own acknowledged
        exit, ``MemoryService.leave_notebook`` (``DELETE .../membership``).
        """
        self._store.remove_member(notebook_id, user_id)
        self._clear_member_profiles(notebook_id, [user_id])

    def forget_member_state(self, notebook_id: str, user_id: str) -> None:
        """The per-member cleanup ``remove_member`` runs after its row delete,
        for a caller that has already deleted the membership row itself in a
        transaction of its own (the self-exit does, atomically with its final
        Memory count). Deliberately does not touch ``notebook_members``: by
        the time this runs the user may have rejoined, and deleting that new
        row would silently undo their rejoin."""
        self._clear_member_profiles(notebook_id, [user_id])

    def _clear_member_profiles(self, notebook_id: str, user_ids: Iterable[str]) -> None:
        """Discard both halves of these members' overlays: the block rows
        (``clear_all``) AND the job/status rows (``clear_job_rows``), then
        their agent observations.

        One implementation for one member and for many (kick-all review
        2026-10-23): each table is cleared for the whole set in ONE write
        transaction, so removing every link member costs three transactions,
        not three per member; ``remove_member`` passes a one-element set.

        Agentic Memory P1 (T5 repair round). Clearing only the blocks left a
        stale ``agent_profile_jobs`` row behind — most visibly its
        ``pending_signal`` counter, which is derived entirely from this
        member's own asks/reports in THIS notebook. Without this, a member
        removed and then re-added would not start from a blank slate (the
        "rejoin" contract this method exists to guarantee): their counter
        would already be partway to the next consolidation run, or their row
        could still show a stale ``failed``/``running`` status from before
        they left. Both deletes are independently fail-open — a job-row
        failure must not re-raise past an already-committed access change,
        the same reasoning that already applied to the block-row delete.

        ⚠ Order matters (codex #520 R3 P1): the job row goes FIRST. The
        overlay worker's revocation guards are keyed off that row — it
        re-reads it before writing, and a ``settle`` that finds it gone
        triggers the post-write wipe. Blocks-first left a window where an
        in-flight worker passed its pre-write check, recreated the just-
        cleared blocks AND settled successfully, all before the job row
        vanished — every guard green, private data resurrected. Deleting the
        marker first means any worker still in flight either skips its writes
        (pre-check) or fails its settle and wipes what it wrote (post-guard);
        the block delete below then covers the fully-settled-before-removal
        case. The order holds for every member of the set at once: all job
        rows go before any block."""
        # The ``''`` owner is the shared base layer, never a member.
        owners = [user_id for user_id in user_ids if user_id]
        if not owners:
            return
        if self._profiles is not None:
            self._clear_profile_halves(notebook_id, owners)
        # codex #535 R6 P2:观察行同批清空——它们是这个人自己的 Agent 在本库
        # 留下的使用痕迹,移出后残留会经 GET .../agent-observations 立即复活,
        # 还会喂进重新加入后的第一次覆盖层巡固,破坏「空白起点」契约。座位与
        # profiles 各自独立判 None(旧组合根/替身可能只接了一半)。
        if self._observations is not None:
            try:
                self._observations.clear_observations(notebook_id, owners)
            except Exception:  # noqa: BLE001 — access change already committed
                _log.exception(
                    "failed to clear agent observations for notebook %s",
                    notebook_id,
                )

    def _clear_profile_halves(self, notebook_id: str, owners: list[str]) -> None:
        try:
            self._profiles.clear_job_rows(notebook_id, owners)
        except Exception:  # noqa: BLE001 — access change already committed
            _log.exception(
                "failed to clear agent profile job row for notebook %s",
                notebook_id,
            )
        try:
            self._profiles.clear_all(notebook_id, owners)
        except Exception:  # noqa: BLE001 — access change already committed
            _log.exception(
                "failed to clear agent profile overlay for notebook %s",
                notebook_id,
            )

    def list_members(self, notebook_id: str) -> list:
        return self._store.list_members(notebook_id)

    def join_shared(
        self, notebook_id: str, user_id: str, *, share_token: str
    ) -> NotebookSummary:
        # codex #659 R12 P1: R11 wrapped a database.write() (inside
        # add_member) INSIDE an outer database.connect() — under
        # POSTGRES_POOL_MAX_SIZE=1 that nested acquisition can never get a
        # second connection from a pool the outer connect() is still
        # holding, so every join deadlocks until the pool wait times out.
        # It also left the liveness read and the membership write as two
        # separate transactions, leaving a residual tombstone-race window
        # between them. Fix: fold both into ONE database.write() — exactly
        # one connection acquired for the whole call — and do the member
        # insert via insert_member_if_live's INSERT...SELECT...WHERE
        # EXISTS(NOTEBOOK_LIVE_SQL) (same idiom as ensure_conversation's
        # R6 fix: a read-side visibility technique, not a change to
        # write-side tombstone-sentinel discipline). add_member itself is
        # untouched — it has independent callers (repository_facade, the
        # service's own public add_member) and keeps its own contract.
        # notebook_row_on/from_row are still called exactly once each, on
        # the SAME connection, matching the pre-existing delegate test.
        with self._database.write() as db:
            row = self._store.notebook_row_on(db, notebook_id)
            if row is None:
                raise KeyError(notebook_id)
            inserted = self._store.insert_member_if_live(
                db, notebook_id, user_id, self._store.now(), share_token
            )
            # codex #659 R14 P2: rowcount==0 is ambiguous between "already a
            # member" (insert_member_if_live's OR IGNORE/DO NOTHING idempotent
            # no-op — a legitimate repeat join) and "the liveness guard
            # blocked the insert" (a concurrent delete job's tombstone
            # committed between notebook_row_on's read above and this INSERT,
            # same transaction — PostgreSQL's default READ COMMITTED lets a
            # LATER statement in the same transaction see an intervening
            # commit). Ignoring rowcount used to let the second case through
            # as a fabricated success: a summary handed back for a user who
            # was never actually added to notebook_members. Disambiguate with
            # a same-connection membership probe — no extra connection taken.
            #
            # codex #659 R16 P2: an existing membership is NOT proof the
            # notebook is still live — for a user already in
            # notebook_members the guarded insert also returns 0, so the
            # probe alone would fabricate a success for a notebook whose
            # tombstone committed between the read above and the INSERT.
            # Re-read the LIVE row (same connection) and hydrate from that
            # fresh read: gone → 404 regardless of membership; live + not a
            # member → the guard could only have blocked on liveness that
            # has since returned... which cannot happen for a monotonic
            # tombstone, so that combination also reads as not-joinable.
            #
            # The link itself is checked too (kick-all ruling 2026-10-23): the
            # route resolved the notebook from ``share_token`` before this
            # transaction, so an unshare can commit in between. The insert
            # requires the token to still be this notebook's live link; the
            # unshare removed every member in the transaction that voided it,
            # so a voided link lands here with no membership and is a 404.
            if inserted == 0:
                fresh = self._store.notebook_row_on(db, notebook_id)
                if fresh is None or not self._store.is_member_on(
                    db, notebook_id, user_id
                ):
                    raise KeyError(notebook_id)
                row = fresh
            notebook = self._summaries.from_row(db, row, user_id=user_id)
        notebook.access = "reader"
        return notebook

    # -------------------------------------------------------------- ownership
    def source_notebook_id(
        self,
        source_id: str,
        *,
        viewer_id: "str | None" = None,
        visible_only: bool = False,
    ) -> "str | None":
        return self._store.source_notebook_id(
            source_id, viewer_id=viewer_id, visible_only=visible_only
        )

    def conversation_owner(self, conversation_id: str) -> "str | None":
        return self._store.conversation_owner(conversation_id)

