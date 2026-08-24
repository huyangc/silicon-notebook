from __future__ import annotations

import concurrent.futures
import contextvars
import hashlib
import json
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterable, List, Optional

from app.core.config import Settings
from app.core.event_logging import EventLogger
from app.core.llm import cap_kwargs
from app.domain.extensions import ParserProviderChainHostPort
from app.models.sources import (
    AddUrlSourcesResult,
    HIDDEN_SYNTHETIC_SOURCE_TYPES,
    KG_RUN_MESSAGE_OBJECTS_PREFIX,
    PDF_PYTHON_FALLBACK_WARNING_PREFIX,
    RejectedUrl,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UploadedSourceSummary,
)
from app.repositories.ports import (
    NotebookStorePort,
    SourceElementWrite,
    SourceScheduler,
    SourceStorePort,
    UploadedSourceFile,
)
from app.repositories.source_files import SourceFileStore, safe_filename
from app.services import kg_ingest, remote_sources
from app.services.extraction_profiles import PROFILES, get_profile
from app.services.kg.json_utils import safe_json
from app.services.kg.run_control import KgBuildAborted
from app.services.kg_mutation import KgMutationCoordinator
from app.services.knowledge_lifecycle import KnowledgeLifecycleService
from app.services.mineru_cloud_client import MinerUCloudNotConfigured
from app.services.paper_meta import (
    PAPER_META_SCHEMA_HINT,
    paper_meta_doc_type_eligible,
    paper_meta_prompt,
    verify_paper_meta,
)
from app.services.parser_chain_execution import (
    PARSER_FALLBACK_WARNING_CODE,
    ParserChainExecution,
)
from app.services.prompts import NOTEBOOK_META_SCHEMA_HINT, notebook_meta_prompt
from app.services.source_chunking import SourceChunkingService
from app.services.source_embedding import SourceEmbeddingService
from app.services.source_element_selection import (
    deduplicate_repeated_page_boundaries,
)


#: 「改了文档类型 → 只重抽 KG」失败时留给用户的说明。面向用户的文案，不带异常
#: 类型/堆栈（技术细节进 logger.exception 与事件日志）：它会原样出现在来源卡片
#: 的错误位上。指路的两条重试入口都是界面上真实存在的。
RETYPE_REEXTRACT_FAILED_MESSAGE = (
    "按新的文档类型重新分析时出错；文件已保留，可重新解析或重新上传。"
)

#: 「抽取跑到一半用户又改了类型」最多连锁补跑几次。正常路径是 1 次（改一次类型
#: → 补跑一次）；上限只为挡住病态循环——有人不停改类型时，这条源不能永远占着 KG
#: job 池的一个槽位。
_DOC_TYPE_RECONCILE_MAX_ROUNDS = 3


class PartialKgRetryIncomplete(RuntimeError):
    """A safe partial-KG retry left the previous graph untouched."""


@dataclass(frozen=True)
class SourcePipelineHooks:
    """Fresh per-call compatibility hooks into the (still facade-owned) KG /
    catalog / scale domains.  The facade builds one on EVERY orchestration
    call from its own bound seats, so post-construction per-instance
    monkeypatches (_run_extraction, _mark_unified_kg_dirty, ...) keep being
    observed.  Gate 5 (Task 15) replaces these callbacks with direct
    KnowledgeLifecycleService / KgMutationCoordinator dependencies."""

    should_extract_kg: Callable[[str], bool]
    extract_source: Callable[[str], None]
    mark_unified_dirty: Callable[[str], None]
    augment_notebook_metadata: Callable[[str, str], None]
    maybe_enqueue_scale_fold: Callable[[str], None]
    #: Agentic Memory P1 (T4):这本库的语料变了一次(新增/重解析/删除)。**必须
    #: 自身 fail-open**——它挂在上传/删除的成功路径末尾,一次后台整理排不上,不
    #: 是这次上传失败。三个来源生命周期口共用它(新增与重解析都走 process_source,
    #: 删除走 delete_source),不存在第四处。
    note_corpus_change: Callable[[str], None]


class SourceIngestionService:
    """Source ingestion orchestration: import / URL add / upload dispatch,
    the parse → chunk → background-embed → extract status machine, source
    deletion and per-source KG extraction (Task 12).

    Composition rules (Gate 4): no SQL lives here, no facade import, no
    direct ``_connect``/``_write`` — persistence goes through the injected
    stores, the ``write`` seat (the facade's ``_write`` compatibility seam,
    resolved at call time so transaction-counting/failure-injection
    monkeypatches keep observing every commit boundary), the DIRECT
    KnowledgeLifecycleService / KgMutationCoordinator dependencies (Task 15
    replaced the Gate-4 store_kg / incremental_fuse_source /
    invalidate_unified_cache callbacks) and the remaining TEMPORARY
    facade-owned KG/catalog callbacks that Task 16+ move with their domains.

    Behavior invariants preserved verbatim from the facade:
    - status machine queued→parsing→parsed→extracting→extracted (failed on
      pipeline error); 'extracted' gates on KG extraction ONLY;
    - element replacement commits before the best-effort chunk build; the
      background element-embed daemon thread overlaps foreground extraction
      and is joined before the final pipeline event;
    - the embed thread runs under ``contextvars.copy_context()`` so the
      per-user request context propagates;
    - URL sources parse local-first and NEVER silently fall back to cloud;
    - single-window extraction failures stay isolated inside kg_ingest —
      a pipeline exception records the failed source state instead of
      propagating (record_and_continue).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        notebooks: NotebookStorePort,
        sources: SourceStorePort,
        source_files: SourceFileStore,
        chunking: SourceChunkingService,
        embedding: SourceEmbeddingService,
        event_log: EventLogger,
        new_id: Callable[[str], str],
        now: Callable[[], str],
        write: Callable[[], ContextManager[Any]],
        # --- facade-bound late seams (call-time resolution keeps frozen
        # module/instance patch targets effective) -------------------------
        source_elements: Callable[[str], List[SourceElement]],
        summarize_source: Callable[[str, List[SourceElement]], str],
        source_type_from_name: Callable[[str], str],
        parser_provider_chain: ParserProviderChainHostPort,
        parser_connection_probe: Any,
        make_persist_image: Callable[[str, str, str], Any],
        delete_source_images: Callable[[str], None],
        mineru_client: Callable[[], Any],
        mineru_cloud_client: Callable[[], Any],
        model_clients: Any,
        normalize_doc_type: Callable[[str], str],
        default_notebook_names: Iterable[str],
        # --- TEMPORARY KG/catalog callbacks (Task 13/15 targets) ----------
        clear_source_extraction_state: Callable[..., None],
        begin_extraction_run: Callable[..., None],
        finish_extraction_run: Callable[[str, str, str], None],
        notebook_tier: Callable[[str], str],
        concept_whitelist_terms: Callable[[], set],
        notebook_has_kg: Callable[[str], bool],
        # --- direct service dependencies (Task 15: Gate-4 hooks replaced) ---
        knowledge_lifecycle: KnowledgeLifecycleService,
        kg_mutations: KgMutationCoordinator,
        maybe_auto_index: Callable[[str], None],
        notebook_meta_row: Callable[[str], Optional[dict]],
        notebook_meta_sources: Callable[[str, str], List[dict]],
        apply_notebook_meta: Callable[..., None],
        maybe_enqueue_scale_fold: Callable[[str], None],
        invalidate_knowledge_counts: Callable[[str], None] = lambda _notebook_id: None,
        # Agentic Memory P1 (T4). Defaulted to a no-op for the same reason
        # ``invalidate_knowledge_counts`` above is: narrow test doubles and any
        # runtime that never wires the understanding feature must keep
        # composing this service unchanged, and "no consolidation chain" is a
        # complete, correct behaviour (it is exactly the kill-switch-off path).
        note_corpus_change: Callable[[str], None] = lambda _notebook_id: None,
    ) -> None:
        self.settings = settings
        self.notebooks = notebooks
        self.sources = sources
        self.source_files = source_files
        self.chunking = chunking
        self.embedding = embedding
        self.event_log = event_log
        self.new_id = new_id
        self.now = now
        self.write = write
        self.source_elements = source_elements
        self.summarize_source = summarize_source
        self.source_type_from_name = source_type_from_name
        self.parser_provider_chain = parser_provider_chain
        self.parser_connection_probe = parser_connection_probe
        self.make_persist_image = make_persist_image
        self.delete_source_images = delete_source_images
        self.mineru_client = mineru_client
        self.mineru_cloud_client = mineru_cloud_client
        self.model_clients = model_clients
        self.normalize_doc_type = normalize_doc_type
        self.default_notebook_names = default_notebook_names
        self.clear_source_extraction_state = clear_source_extraction_state
        self.begin_extraction_run = begin_extraction_run
        self.finish_extraction_run = finish_extraction_run
        self.notebook_tier = notebook_tier
        self.concept_whitelist_terms = concept_whitelist_terms
        self.notebook_has_kg = notebook_has_kg
        self.knowledge_lifecycle = knowledge_lifecycle
        self.kg_mutations = kg_mutations
        self.maybe_auto_index = maybe_auto_index
        self.notebook_meta_row = notebook_meta_row
        self.notebook_meta_sources = notebook_meta_sources
        self.apply_notebook_meta = apply_notebook_meta
        self.maybe_enqueue_scale_fold = maybe_enqueue_scale_fold
        self.invalidate_knowledge_counts = invalidate_knowledge_counts
        self.note_corpus_change = note_corpus_change
        # 论文元数据 backfill 进程内状态镜像 kg_building（重启即清）
        # nb_id → {"total": N, "done": k, "_gen": G}
        self._paper_meta_backfilling: dict[str, dict] = {}
        self._paper_meta_backfilling_lock = threading.Lock()
        # 同一 nb 并发 backfill 的世代守卫：后来者覆盖先来者的 entry 后，
        # 先来者的 finally 不能把后来者的 entry 弹掉；_one 里的 done 更新也
        # 只能改属于自己 gen 的 entry，避免"最新一次"语义下的 done 串扰。
        self._paper_meta_generation = 0
        # 源级活跃租约(进程内;重启即空=没人在处理,恰是崩溃后的正确答案,故不参与
        # 启动清算)。镜像 kg_building / _paper_meta_backfilling 的进程内单飞惯例:
        # process_source 进入时给计数加一、finally 减一。职责是「在途误报抑制」——体检
        # endpoint 与 process_source 同进程可并发,在途源瞬时没 elements/没 chunks,
        # 纯产物判据会误报损坏,租约声明「有活线程在弄,别报」。崩溃检测归 parse_status
        # + 启动清算,不靠这个内存集。值是**引用计数**而非时间戳:同一源可被并发处理
        # (上传后台 job 未完时 owner 又点 parse),两个 invocation 各加一次,先完成者
        # 减到 1(仍活)、后完成者减到 0 才撤租——用时间戳会被后来者覆盖、令先完成者的
        # finally 撤错人(镜像上面 _paper_meta_backfilling 的世代守卫,这里用计数更简)。
        # source_id → 活跃 invocation 数;P2 体检把 keys() 当 active 集做减法。
        self._active_sources: dict[str, int] = {}
        self._active_sources_lock = threading.Lock()
        # source_id → 「后台嵌入进行中」的 worker 数(codex 第5轮 P2)。process_source 可能在其后台
        # 嵌入 worker 写完向量**之前**就返回、撤掉自己那份活跃租约;体检 H4/H5 用活跃集排除在途源,
        # 若不单独记这段「在嵌入」,in-flight 向量会被误报缺失+诱发重复 backfill。刻意与 _active_sources
        # 分开(那是 process_source 生命周期 + 分块锁的引用计数,嵌入 worker 晚于它结束);两者的并集
        # 由 _active_source_ids_snapshot 出给体检。同一 _active_sources_lock 守护(都是小 dict)。
        self._embedding_sources: dict[str, int] = {}
        # Relation-completion pages are durable; this set is only a process-local
        # single-flight guard for their resumable drain jobs. Startup repopulates
        # work from the persisted pending rows after a crash/restart.
        self._relation_completion_scheduled: set[tuple[str, str, str]] = set()
        self._relation_completion_schedule_lock = threading.Lock()
        # 源级分块串行锁(per-source):同一源的并发 reparse 必须在「换 elements →
        # 建 chunks + 置 marker」这段串行,否则一次 invocation 可能读到 A 代 elements、
        # 另一次把它换成 B 代、第一次再把基于 A 代的 chunks 连同 marker 写回,留下
        # 「B 代 elements + A 代 chunks + marker 已置」的假完成(codex 第 2 轮 P2)。
        # 只锁 build_chunks 不够——换 elements 若不持同一把锁,仍能插进「读→写」之间;
        # 故锁必须连续覆盖 replace_elements 到 build_chunks。懒创建、与上面租约同生命
        # 周期:refcount 归零(无活跃 invocation)时一并清除,免得每见一个源就永久留锁。
        # get-or-create 在 _active_sources_lock 下做,但**获取锁本身**在该 meta 锁之外
        # (否则持 meta 锁等 per-source 锁会死锁)。
        self._source_chunk_locks: dict[str, threading.Lock] = {}

    def _source_chunk_lock(self, source_id: str) -> threading.Lock:
        """取(或懒创建)某源的分块串行锁。仅在 _active_sources_lock 下 get-or-create
        锁对象(快),**不**在这里 acquire——调用方拿到后自行 ``with`` 获取,以免持 meta
        锁阻塞在 per-source 锁上造成死锁。清除在 process_source 的 finally 里,与租约
        refcount 归零同步(那一刻无活跃 invocation 持锁,可安全 pop)。"""
        with self._active_sources_lock:
            lock = self._source_chunk_locks.get(source_id)
            if lock is None:
                lock = threading.Lock()
                self._source_chunk_locks[source_id] = lock
            return lock

    def _release_source_lease(self, source_id: str) -> None:
        """租约计数减一;归零(无活跃 invocation)时连该源的分块锁一并从注册表清除。
        process_source 的 finally 与 ``hold_source_chunk_lock`` 守卫**共用**这一套 refcount
        生命周期,保证「计数归零才 pop 锁」只有一处实现。调用方须已释放它持有的分块锁
        (pop 假定此刻无人持该锁)。"""
        with self._active_sources_lock:
            remaining = self._active_sources.get(source_id, 0) - 1
            if remaining > 0:
                self._active_sources[source_id] = remaining
            else:
                self._active_sources.pop(source_id, None)
                # 计数归零=无 invocation 持该源分块锁,顺手清锁(有界化)。见 __init__ 说明。
                self._source_chunk_locks.pop(source_id, None)

    def _release_embedding_source(self, source_id: str) -> None:
        """「后台嵌入进行中」计数减一,归零即从 _embedding_sources 移除(codex 第5轮 P2)。与
        _release_source_lease 分开:嵌入 worker 不碰 _active_sources / 分块锁,只标记「在嵌入」。"""
        with self._active_sources_lock:
            remaining = self._embedding_sources.get(source_id, 0) - 1
            if remaining > 0:
                self._embedding_sources[source_id] = remaining
            else:
                self._embedding_sources.pop(source_id, None)

    @contextmanager
    def _held_source_chunk_lock(self, source_id: str, timeout: float | None):
        """``hold_source_chunk_lock`` / ``try_hold_source_chunk_lock`` 的共同实现:
        登记租约 → 取锁(可带超时)→ yield「是否真的持到锁」→ 释放锁 → 减租约。

        超时失败时**照样**走 finally 减租约,且此时 pop 仍然安全:取锁失败意味着锁正被
        别人持有,而持锁方(process_source 在管线入口登记、``_held_source_chunk_lock``
        自己在取锁前登记)的租约必 ≥1,所以本方这一减不可能把计数减到 0、不会 pop 掉别人
        正持有的锁对象。这与下面 ``hold_source_chunk_lock`` 文档里那条不变式是同一条。
        """
        with self._active_sources_lock:
            self._active_sources[source_id] = self._active_sources.get(source_id, 0) + 1
        try:
            # 租约已≥1,注册表项在本方持有期不会被 pop,get-or-create 与 pop 无竞争。
            lock = self._source_chunk_lock(source_id)
            acquired = lock.acquire() if timeout is None else lock.acquire(timeout=timeout)
            try:
                yield acquired
            finally:
                if acquired:
                    lock.release()
        finally:
            self._release_source_lease(source_id)

    @contextmanager
    def hold_source_chunk_lock(self, source_id: str):
        """给**非 process_source 的持锁方**(体检 backfill)用的守卫:持有该源分块锁的同时,
        把它登记进活跃租约(``_active_sources``),使 process_source 的 finally **不会在本方仍
        持锁时把锁从注册表 pop 掉**(codex 第2轮 P1:否则 process_source 释放锁→finally 见租约归零
        →pop 锁,而本方仍持旧锁;随后新的 reparse 会另建一把**新锁**、与本方互斥失效→给复用的
        element/chunk id 挂上永久陈旧向量,正是这把锁要防的;且 backfill-only 的源没有任何租约触发
        pop→**锁泄漏**)。先登记租约(计数≥1 → 锁在本方持有期不会被 pop),再取锁 acquire;退出时
        先释放锁、再经 ``_release_source_lease`` 减租约(归零则本方作为最后持有者连锁清除)。

        与 process_source 的正确串行由**同一把锁 + 同一套租约**保证:本方登记租约后取到的锁,与
        process_source 在 `_source_chunk_lock` 取到的是**同一对象**(租约≥1 保证注册表项不被中途 pop
        重建);任一方持锁,另一方 acquire 阻塞。process_source 持锁时其租约必≥1(在管线入口登记、
        finally 才减),故本方减租约到 0 时必无 process_source 持锁,pop 安全。"""
        with self._held_source_chunk_lock(source_id, None):
            yield

    @contextmanager
    def try_hold_source_chunk_lock(self, source_id: str, *, timeout: float):
        """``hold_source_chunk_lock`` 的**有界等待**版:yield 的是「是否持到锁」。

        语义与 ``threading.Lock.acquire(timeout=...)`` 逐字一致——yield ``True`` 表示锁在本
        ``with`` 体内被持有,yield ``False`` 表示等待超时、**本方没有任何锁**。调用方必须先
        判断再动手(见 ``CommandCatalogService._source_write_barrier``:False 直接抬手回一个
        「来源正在重新解析」的 409,body 里不做任何写)。

        为什么需要有界版:这把锁被 ``process_source`` 从 ``replace_elements`` 一路持到
        ``build_chunks``,中间夹着 summarize 与 paper_meta 两次 LLM 调用,最坏可达分钟级。让一个
        同步 HTTP 请求线程无限期等在上面,换来的答案还是同一个 409,不划算;有界等待把它变成
        「几秒内如实告知正在解析」。刻意**不**把它做成异常:异常类型会逼调用方 import 本模块
        (它拖着解析/嵌入/KG 的整条依赖),而 ``catalog_job`` 是刻意的后端中性模块,只吃端口与
        可调用对象。返回布尔让那条边界保持只有一个鸭子类型方法名。
        """
        with self._held_source_chunk_lock(source_id, timeout) as acquired:
            yield acquired

    def pipeline_hooks(self) -> SourcePipelineHooks:
        return SourcePipelineHooks(
            should_extract_kg=self.should_extract_kg,
            extract_source=self.run_extraction,
            mark_unified_dirty=self.kg_mutations.mark_unified_kg_dirty,
            augment_notebook_metadata=self.augment_notebook_metadata,
            maybe_enqueue_scale_fold=self.maybe_enqueue_scale_fold,
            note_corpus_change=self.note_corpus_change,
        )

    def import_sources_compat(
        self, notebook_id: str, payload: SourceImportRequest
    ) -> List[SourceSummary]:
        return self.import_sources(notebook_id, payload, self.pipeline_hooks())

    def add_url_sources_compat(
        self,
        notebook_id: str,
        urls: Iterable[str],
        scheduler=None,
        capacity=None,
        agent_profile_id: str = "",
    ) -> AddUrlSourcesResult:
        return self.add_url_sources(
            notebook_id,
            urls,
            scheduler,
            self.pipeline_hooks(),
            capacity=capacity,
            agent_profile_id=agent_profile_id,
        )

    def upload_sources_compat(
        self,
        notebook_id: str,
        files: Iterable[UploadedSourceFile],
        scheduler=None,
        agent_profile_id: str = "",
    ) -> List[UploadedSourceSummary]:
        return self.upload_sources(
            notebook_id,
            files,
            scheduler,
            self.pipeline_hooks(),
            agent_profile_id=agent_profile_id,
        )

    def process_source_compat(self, source_id: str) -> SourceSummary:
        return self.process_source(source_id, self.pipeline_hooks())

    def parse_source_compat(self, source_id: str) -> SourceSummary:
        return self.parse_source(source_id, self.pipeline_hooks())

    def delete_source_compat(self, source_id: str) -> None:
        return self.delete_source(source_id, self.pipeline_hooks())

    def list_sources(self, notebook_id: str) -> List[SourceSummary]:
        self.notebooks.get_row(notebook_id)
        return self.sources.list_sources(notebook_id)

    def list_sources_page(
        self, notebook_id: str, offset: int = 0, limit: int = 50, q: str = ""
    ):
        self.notebooks.get_row(notebook_id)
        return self.sources.list_sources_page(
            notebook_id, offset=offset, limit=limit, q=q
        )

    @staticmethod
    def source_type(file_name: str) -> str:
        lower_name = file_name.lower()
        if lower_name.endswith(".pdf"):
            return "pdf"
        if lower_name.endswith(".md") or lower_name.endswith(".markdown"):
            return "markdown"
        if lower_name.endswith(".zip"):
            return "markdown_bundle"
        if lower_name.endswith(".docx"):
            return "docx"
        if lower_name.endswith(".pptx"):
            return "pptx"
        return "other"

    def summarize(self, title: str, elements: List[SourceElement]) -> str:
        text = "\n".join(element.text for element in elements[:12])
        llm = self.model_clients.chat("source_summary")
        if llm.configured and text.strip():
            try:
                raw = llm.chat_json(
                    [{"role": "user", "content": (
                        "Summarize this semiconductor notebook source in one concise sentence.\n"
                        f"Title: {title}\n\n{text[:6000]}"
                    )}],
                    '{"summary": "one concise sentence"}',
                )
                summary = str(json.loads(raw).get("summary", "")).strip()
                if summary:
                    return summary
            except Exception:
                pass
        if not text.strip():
            return "Parsed source contains no extractable text elements."
        return f"{len(elements)} parsed text element(s). {' '.join(text.split())[:260]}"

    # ------------------------------------------------------------- intake
    def import_sources(
        self,
        notebook_id: str,
        payload: SourceImportRequest,
        hooks: SourcePipelineHooks,
    ) -> List[SourceSummary]:
        """登记一批「只有元数据」的来源行（文件本身尚未上传）。

        刻意不接出处参数：这条路径只服务浏览器的元数据导入，落下的行因而一律是
        用户添加的（`agent_profile_id` 留 NULL）。Agent 侧入口若将来要走这里，
        必须先补出处穿透——否则 Agent 建出的行会被记成人添加的，既拿不回删除
        权限，也让「这份来源是谁加的」在同一张表里分叉。
        """
        self.notebooks.get_row(notebook_id)  # KeyError if missing
        source_ids: List[str] = []
        with self.write() as db:
            # One shared transaction for the whole batch — all-or-nothing,
            # exactly as the former inline loop behaved.
            for file in payload.files:
                source_id = self.new_id("src")
                self.sources.insert_source(
                    source_id=source_id,
                    notebook_id=notebook_id,
                    title=file.file_name,
                    source_type=self.source_type_from_name(file.file_name),
                    status="imported",
                    parse_status="metadata-only",
                    file_name=file.file_name,
                    file_path="",
                    file_size=file.file_size,
                    file_hash="",
                    summary="File metadata imported. Upload the file to parse source elements.",
                    doc_type=self.normalize_doc_type(file.doc_type),
                    connection=db,
                )
                source_ids.append(source_id)
        return [self.sources.get_source(source_id) for source_id in source_ids]

    def add_url_sources(
        self,
        notebook_id: str,
        urls: Iterable[str],
        scheduler: "SourceScheduler | None",
        hooks: SourcePipelineHooks,
        capacity: "int | None" = None,
        agent_profile_id: str = "",
    ) -> AddUrlSourcesResult:
        """逐 URL 初筛(空白跳过;非 PDF/不可达→rejected,不建来源);通过的建 source_url
        来源并交由现有 process_source(有 scheduler 则后台,否则同步)。未配置 token→报错。

        capacity 是「每笔记本文档数量上限」的剩余额度(None=不限,如 admin 笔记本):容量
        按**成功探测逐条**扣减——探测通过但额度已用尽的 URL 进 rejected(超限原因),不消耗
        配额。故一个无效链接不会拖累整批,接近上限时仍能建成额度内的有效来源。

        agent_profile_id 非空时把这批来源标成「Agent 添加」(v48 出处列),空串(所有既有
        调用方的默认值)落 NULL = 人添加的,行为逐位不变。"""
        self.notebooks.get_row(notebook_id)  # KeyError if missing
        # 本地 MinerU 或云端任一可用即可；本地优先（内网场景数据不出网）。
        if not (
            self.mineru_client().configured or self.mineru_cloud_client().configured
        ):
            raise MinerUCloudNotConfigured(
                "未配置 PDF 解析服务（本地 MINERU_MODE=http/cli 或云端 MINERU_API_TOKEN）"
            )
        created: List[SourceSummary] = []
        rejected: List[RejectedUrl] = []
        budget = capacity  # None=不限;int=剩余可建数,每建成一个 -1
        for raw in urls:
            url = (raw or "").strip()
            if not url:
                continue
            probe = remote_sources.probe_pdf(url)
            if not probe.ok:
                rejected.append(RejectedUrl(url=url, reason=probe.reason))
                continue
            if budget is not None and budget <= 0:
                rejected.append(
                    RejectedUrl(url=url, reason="已达该笔记本的文档数量上限，未添加")
                )
                continue
            source_id = self.new_id("src")
            self.sources.insert_source(
                source_id=source_id,
                notebook_id=notebook_id,
                title=probe.display_name,
                source_type="pdf",
                status="queued",
                parse_status="queued",
                file_name=probe.display_name,
                file_path="",
                source_url=url,
                file_size=probe.content_length,
                file_hash="",
                summary="链接已添加，解析排队中。",
                doc_type="",
                agent_profile_id=agent_profile_id,
            )
            if scheduler is not None:
                scheduler(source_id)
            else:
                self.process_source(source_id, hooks)
            created.append(self.sources.get_source(source_id))
            if budget is not None:
                budget -= 1
        return AddUrlSourcesResult(created=created, rejected=rejected)

    def reuse_uploaded_source(
        self,
        source_id: str,
        scheduler: "SourceScheduler | None",
        hooks: SourcePipelineHooks,
        *,
        doc_type: str = "",
        doc_type_explicit: bool = False,
    ) -> UploadedSourceSummary:
        """同 notebook 内已有相同内容的源 → 复用那一行，绝不新建第二条。

        复用 ≠ 撒手不管：停在失败终态（parse_status='failed'）的行就地重跑
        流水线，其余原样返回。理由是 file_hash 的语义：它在
        insert_source 时（解析之前）就写进去了，只说明「这份内容进过库」，
        不说明「这份内容成功摄取过」——解析失败（本项目里 MinerU/网络抖动是
        常态）后指纹照样留着。若无条件短路，用户最自然的重试动作（把同一个
        文件再传一次）会静默变成 no-op 还弹「已上传」，坏源永远修不好。

        修法刻意不是「失败时清掉 file_hash」（Memory 派生源那条路径的做法）：
        那会让指纹重新落空，UI 与离线 batch_ingest 的 already_ingested 双双
        重新建行，把重复源又请回来——正是本特性要治的东西。Memory 那边可以清，
        是因为它另有 memory_id 唯一键兜住行的身份，上传路径没有。

        崩溃遗留在 queued/parsing 的行由启动期的 recover_interrupted_jobs 统一
        判死（那里才有「本进程刚起来，不可能有流水线在跑」这个前提），落到
        'failed' 后同样能被这里重试。反过来，正在跑的行绝不在这里重入：同一行
        并发跑两条流水线是重复的 LLM/解析开销。

        ``doc_type``（本次上传为这个文件选的文档类型，调用方原样传入、这里才
        归一化）是复用路径上第二件不能撒手的事：内容一样不代表用户的意图一样，
        「类型判错了，我改成教材再传一遍」是最自然的纠正动作，而 doc_type 决定
        抽取 profile 并进抽取 prompt（因而也进 LLM 缓存键），静默丢掉等于这条源
        永远按错的类型入图。但「改」必须以用户**显式**表态为前提——由 per-file 的
        ``doc_type_explicit`` 门控（前端只在用户手动动过这一项的类型下拉框时才置位；
        auto-detect 的建议值、以及不发此信号的调用方/batch_ingest 一律非显式）：
        - 显式 + 具体类型 → 改成该类型并按新 profile 重抽；
        - 显式 + 空（用户把下拉框选回「自动检测」）→ 把已定型的源**重置回自动**（''）
          并按自动 profile 重抽——否则 UI 上根本无法把类型改回自动；
        - **非显式**（重传时没动下拉框，哪怕发来的是具体值/与旧值不同）→ 绝不改，
          保留旧类型、不重抽。这条是关键：重传同一文件是内容去重的核心场景，不能
          因为 auto-detect 顺手填了个类型就把既有源的类型静默抹掉重抽。
        判据只看 ``doc_type_explicit`` 与归一化后的新旧值是否不同，不再看「值空不空」。

        还在跑的行（'queued'/'parsing'/'extracting'）只记类型、不调度任何东西，
        由那条正在跑的任务自己收尾：'queued'/'parsing' 的抽取还没开始，
        run_extraction 到时会读到新类型；'extracting' 的已经读走了旧类型，靠
        _extract_reconciling_doc_type 跑完比对一次再按新类型补跑。绝不在这里
        另起一条任务——KG job 池不按 source 串行，两条任务并发抽同一条源会互相
        清掉对方的 KG 产物（见 _extract_reconciling_doc_type）。

        换后缀（解析器）**不再**触发重解析：同一份内容以不同后缀重传（如先 .csv
        后 .md）复用既有源、保留原解析（原 file_name/file_path/elements 不变），不
        重跑整条流水线。要换解析器请删除该源再重传。
        """
        summary = self.sources.get_source(source_id)
        incoming_doc_type = self.normalize_doc_type(doc_type or "")
        stored_doc_type = self.normalize_doc_type(
            getattr(summary, "doc_type", "") or ""
        )
        # 只有用户在 UI 里**显式**设过这一项的类型下拉框（doc_type_explicit=True）才允许
        # 改/重置既有源的类型；非显式（重传没动下拉框、auto-detect 建议、非 UI 调用方）
        # 一律保留旧值、不重抽。显式 + 空 = 用户选回「自动检测」→ 重置回 ''。见 docstring。
        retyped = doc_type_explicit and incoming_doc_type != stored_doc_type
        if retyped:
            self.sources.set_doc_type(source_id, incoming_doc_type)
            # 新类型不再合格抽论文元数据(如 academic→textbook)→ 清掉旧论文元数据。
            # 否则元数据抽取 gate 会跳过这条源、不再刷新,而 SourceStore 仍拿旧标题/
            # 作者把它当论文展示/搜索/计数。合格→合格(如 auto→academic)不动;
            # 判定复用抽取侧同一 predicate(paper_meta_doc_type_eligible),两处不会漂移。
            # incoming_doc_type 已 normalize,直接喂 predicate。幂等:无元数据行则 no-op。
            if not paper_meta_doc_type_eligible(incoming_doc_type):
                self.sources.clear_paper_meta(source_id)

        if summary.parse_status == "failed":
            # 失败源重试：解析产物本来就不可信（失败），重跑整条流水线。
            # ⚠ 认领必须原子：两个并发上传同一失败源都在任一方写 'queued' 之前读到
            # summary.parse_status=='failed'，若都无条件 set 'queued'+调度，就会给同一行
            # 起两条流水线（互相 replace_elements / 清对方的抽取状态与 KG 产物，还白烧
            # 一份解析/模型开销）。claim_failed_for_retry 是一条 WHERE parse_status='failed'
            # 的守卫 UPDATE，进程全局写锁让两个并发恰有一个 rowcount==1：
            # ① 抢到（翻出 'failed'→'queued'，兼作「已入队」标记与 error_message 清理）→
            #    调度重试；② 没抢到（另一次已认领）→ 绝不重入，返回既有行。
            if self.sources.claim_failed_for_retry(source_id):
                self._emit_status(source_id, "queued", "")
                if scheduler is not None:
                    scheduler(source_id)
                else:
                    self.process_source(source_id, hooks)
        elif retyped and hooks.should_extract_kg(summary.notebook_id):
            # 内容没变、只有类型变了 → 只重抽 KG，不重解析（见 _reextract_retyped）。
            # ⚠ 认领必须原子：「读 summary.parse_status 再决定调度」是 TOCTOU——
            # 方法开头读到的 'extracting'/'extracted' 可能在这几行里已被在跑的流水线
            # 落成 'extracted'。若据旧读到的 'extracting' 就不调度，而那条流水线又
            # （开头就读走了旧 doc_type）落了 'extracted'，新类型就没人补跑。
            # claim_reextract_if_extracted 用「WHERE parse_status='extracted'」的守卫
            # UPDATE 原子表决：抢到（本就已定型）→ 本次拿到重抽权、翻 'extracting'；
            # 没抢到（还在 queued/parsing/extracting）→ 不调度，交给那条在跑的流水线
            # 的终态 doc_type 收口（mark_extracted_if_doc_type）按新类型补跑。
            # ⚠ 刻意**不**把 'parsed' 纳入这条守卫：重抽走 _reextract_retyped（只重抽、
            # 不 parse/embed），对 idle 'parsed'（可能由启动恢复而来、embedding 陈旧/缺失）
            # 会落个 extracted-but-unembedded 的源。idle 'parsed'+纯 retype 不是回归
            # （doc_type 仍落库，将来任何整条重跑都会用上），故留作已知边界。
            if self.sources.claim_reextract_if_extracted(source_id):
                self._emit_status(source_id, "extracting", "")
                if scheduler is not None:
                    # 与 build_notebook_kg 同一个 job 池：文档级抽取的并发上限是
                    # 进程全局的，不能在这儿另起线程绕过它。
                    from app.services.kg import scheduler as kg_scheduler

                    kg_scheduler.submit_job(
                        self._reextract_retyped, source_id, summary.notebook_id, hooks
                    )
                else:
                    self._reextract_retyped(source_id, summary.notebook_id, hooks)
        if retyped or summary.parse_status == "failed":
            # 失败重试认领会翻动行；重读让返回值如实反映现状。
            summary = self.sources.get_source(source_id)
        return UploadedSourceSummary.model_validate(
            {**summary.model_dump(), "reused": True}
        )

    def _reextract_retyped(
        self, source_id: str, notebook_id: str, hooks: SourcePipelineHooks
    ) -> None:
        """重跑一条「内容没变、只是 doc_type 改了」的源的 KG 抽取。

        刻意不走 process_source：同一份内容重新解析是纯白烧（MinerU 一趟几十秒
        到几分钟），而且有真实的破坏性——解析在写 elements 之前就先清了图片资产，
        网络抖一下就能把一条本来好好的源打成 failed。doc_type 只喂抽取侧，所以
        只重跑抽取就够。

        状态由调用方先翻成 'extracting'；成功的终态 'extracted' 现由
        _extract_reconciling_doc_type 原子落下（与 doc_type 最终比对合一，见其
        docstring），这里不再单独 set 'extracted'。失败仍在这里落 'failed' 且留一句
        用户看得懂的原因。刻意不再退回 'parsed'：'parsed' 在前端是非终态（轮询判据
        只认 extracted/failed 两个终态），退回去会让界面一直转到超时、报一次假的
        「处理超时」；而且那之后重传同一个文件也救不回来——doc_type 早已落库，
        retyped 为 false，整条路径变成静默 no-op。落 'failed' 则让前端立刻停轮询并
        显示失败，用户可以用既有的「重新解析」入口，或者干脆重传（失败源走整条流水线
        重跑）来重试。（build_notebook_kg 的单源失败确实退 'parsed'，但那是后台批量、
        没有人在等这一条；这里有一个正在等结果的上传请求，语境不同。）

        绝不把异常抛回上传请求：用户的其它文件已经建好了。"""
        try:
            self._extract_reconciling_doc_type(source_id, hooks.extract_source)
        except Exception:  # noqa: BLE001 — 一个源的抽取失败不该炸掉整次上传
            self.set_source_status(
                source_id, "failed", error_message=RETYPE_REEXTRACT_FAILED_MESSAGE
            )
            self.event_log.logger.exception(
                "doc-type re-extraction failed for %s", source_id
            )
            return
        # reconcile 已原子落 'extracted'（DB）；补发 status 事件（与 process_source 同）。
        self._emit_status(source_id, "extracted", "")
        try:
            hooks.mark_unified_dirty(notebook_id)
        except Exception:
            self.event_log.logger.exception(
                "unified-KG dirty mark failed for source %s", source_id
            )
        hooks.maybe_enqueue_scale_fold(notebook_id)

    def _extract_reconciling_doc_type(
        self, source_id: str, extract: Callable[[str], None],
        *, terminal_error_message: str = "", emit_terminal_status: bool = False,
    ) -> None:
        """抽取，并把「标记 extracted」原子地与 doc_type 的最终比对合成一步。

        doc_type 是在 run_extraction 的**开头**就读走的（它选抽取 profile、进抽取
        prompt，因而也进 LLM 缓存键）。「抽取正在跑」这段窗口里改类型
        （reuse_uploaded_source 对 parse_status='extracting' 的行只记类型、不调度
        任何东西），若跑完无条件落 'extracted'，存库的 doc_type 会与真正用来抽取的
        profile 不一致，而且没有任何东西回来纠正——这条源就永远按错的类型入了图。

        每一轮：先读本轮抽取要用的 doc_type，抽取，再用
        mark_extracted_if_doc_type 以「WHERE doc_type=本轮值」为守卫**原子**落终态：
          · rowcount 1 → doc_type 没在窗口里被改，落 'extracted'，一致，返回；
          · rowcount 0 → 窗口里被并发 retype 改了 → 不落终态，带新类型再跑一轮。
        比对与终态是**同一条 UPDATE**，中间不再有任何窗口——这正是「读 doc_type →
        抽取 → 落 extracted」三步一致性的最终收口（Codex 第 4 轮 P2）。此前的写法
        「先读到 doc_type 旧值、再由调用方稍后无条件落 extracted」在这两步之间留有
        窗口：并发 retype 在此改了类型且因行是 'extracting' 而不调度，就永久失配。

        这个窗口只能由正在跑的这一条自己收尾，不能由上传请求另起一条任务：
        KG job 池（app/services/kg/scheduler.py）不按 source 串行，两条任务并发抽
        同一条源会互相清对方的 KG 产物（begin_extraction_run 先删既有对象），比丢
        一次 retype 坏得多。

        轮数上限挡住病态循环（有人不停改类型）：耗尽后无条件落终态 + 告警，别让这
        条源永远卡在 'extracting'。异常照旧向外抛（调用方各自决定落什么终态）；每轮
        只多一次主键读 + 一条守卫 UPDATE，没有任何模型/网络开销。终态的
        error_message 由调用方给（process_source 传 empty_hint/fallback_hint；
        _reextract_retyped 不给）。

        默认只落库、**不发** status 事件：process_source / _reextract_retyped 在自己的
        stage 仪器之后统一发 _emit_status('extracted', ...)，好让事件顺序（extract:done
        先于 status:extracted）与重构前一字不差——DB 落终态的原子性对可观测顺序不可见。
        ``emit_terminal_status=True`` 时（notebook KG 构建路径 _extract_one 复用本收口，
        那条路径没有 extract:done 这类前置 stage 事件、也就没有顺序约束）在守卫落终态
        后**原子**补发 'extracted' 事件：调用方不能改用 set_source_status 自行补发——那是
        一次无条件 DB 写，会在守卫落终态与补发之间的窗口里把并发 retype 刚翻成
        'extracting' 的行又冲回 'extracted'（配旧 profile），正是本收口要堵的洞。"""
        for _ in range(_DOC_TYPE_RECONCILE_MAX_ROUNDS):
            used = self.normalize_doc_type(
                getattr(self.sources.get_source(source_id), "doc_type", "") or ""
            )
            extract(source_id)
            if self.sources.mark_extracted_if_doc_type(
                source_id, used, error_message=terminal_error_message
            ):
                if emit_terminal_status:
                    self._emit_status(source_id, "extracted", terminal_error_message)
                return
        # 轮数耗尽：无条件落终态 + 告警，别把源卡在 'extracting'。
        self.sources.set_status(
            source_id, "extracted", error_message=terminal_error_message
        )
        if emit_terminal_status:
            self._emit_status(source_id, "extracted", terminal_error_message)
        self.event_log.logger.warning(
            "doc_type kept changing during extraction of %s; stopped after %s rounds",
            source_id, _DOC_TYPE_RECONCILE_MAX_ROUNDS,
        )

    def upload_sources(
        self,
        notebook_id: str,
        files: Iterable[UploadedSourceFile],
        scheduler: "SourceScheduler | None",
        hooks: SourcePipelineHooks,
        agent_profile_id: str = "",
    ) -> List[UploadedSourceSummary]:
        """Register uploaded files and kick off processing.

        With a ``scheduler`` (e.g. ``BackgroundTasks.add_task``) the heavy
        parse/embed/extract pipeline runs out of band and each source is
        returned in the ``queued`` state. Without one (tests, scripts) the
        pipeline runs synchronously before returning.

        Each returned row carries ``reused``: False for a source this call
        created, True for an existing same-content source in this notebook
        that was handed back instead (see ``reuse_uploaded_source``). Callers
        that report "N sources added" must count the False ones only.

        A reused row is not necessarily untouched: a non-empty ``doc_type``
        that differs from the stored one is applied to the existing row (and
        its KG re-extracted), so the returned ``doc_type`` is the value that
        now holds — callers that describe the outcome should read it rather
        than assume "nothing happened".

        ``agent_profile_id`` (non-empty only on the Agent surface) stamps v48
        provenance on rows this call CREATES. A reused row keeps whatever
        provenance it already had: an Agent re-uploading bytes a person already
        added must not turn that person's source into an Agent-deletable one.

        ``UploadedSourceFile.title`` likewise applies to CREATED rows only, and
        is what a caller whose display name is NOT its file name (MCP's
        ``add_source_text``: the user supplies a title, the file name is derived
        from it) uses to keep the two apart. Empty falls back to ``file_name``,
        which is every browser/CLI caller.
        """
        self.notebooks.get_row(notebook_id)  # KeyError if missing
        imported: List[UploadedSourceSummary] = []
        for file in files:
            source_id = self.new_id("src")
            file_name = safe_filename(file.file_name)
            digest = hashlib.sha256(file.content).hexdigest()
            # 同 notebook 内相同内容复用既有源，与 batch_ingest 的 already_ingested
            # 行为一致（此前 UI 路径会建出重复源）。跨 notebook 刻意不去重。
            # doc_type 一并递进去：内容判重不代表用户这次的类型选择也该丢
            # （见 reuse_uploaded_source）。
            #
            # 快路径：常见的重传命中既有行 → 直接复用，不落盘、不开写事务。
            existing_id = self.sources.source_id_by_hash(notebook_id, digest)
            if existing_id:
                imported.append(
                    self.reuse_uploaded_source(
                        existing_id, scheduler, hooks,
                        doc_type=file.doc_type,
                        doc_type_explicit=file.doc_type_explicit,
                    )
                )
                continue
            # 首见内容：落盘 + 插入必须原子。此前「source_id_by_hash(读) 再 insert(写)」
            # 是两步，两个并发首次上传相同字节都查不到 → 各插一行（migration 24/25 的
            # 索引非唯一，拦不住）。insert_source_if_absent 在一个写事务里 BEGIN
            # IMMEDIATE + 重查 + 插入，全局写锁（同进程）与 RESERVED 锁（跨进程，
            # batch_ingest 共库）让并发恰有一个真正插入。文件先落盘（I/O 不进写事务、
            # 不占写锁），被复用时清掉这条刚落的孤儿文件——它按本次的新 source_id 命名，
            # 与被复用行的文件不同名、同目录，delete 只删这一个文件（目录非空不连删）。
            stored_path = self.source_files.write_upload(
                notebook_id, source_id, file_name, file.content
            )
            reused_id = self.sources.insert_source_if_absent(
                source_id=source_id,
                notebook_id=notebook_id,
                digest=digest,
                # 显示标题优先用调用方给的那个;空串(浏览器上传、batch_ingest、eval
                # ——那里用户给的本来就是文件名)落回 file_name,行为逐位不变。
                # ⚠ 只在**新建**分支:上面两条 reuse 路径刻意不碰既有行的 title,
                # 与 doc_type 的复用语义一致(复用不是重命名)。
                title=file.title or file_name,
                source_type=self.source_type_from_name(file_name),
                status="queued",
                parse_status="queued",
                file_name=file_name,
                file_path=str(stored_path),
                file_size=len(file.content),
                summary="Uploaded; parsing is queued.",
                doc_type=self.normalize_doc_type(file.doc_type),
                agent_profile_id=agent_profile_id,
            )
            if reused_id is not None:
                # 输给了并发的另一次首传（它先插入并提交）→ 复用那一行，清掉孤儿文件。
                self.source_files.delete(str(stored_path))
                imported.append(
                    self.reuse_uploaded_source(
                        reused_id, scheduler, hooks,
                        doc_type=file.doc_type,
                        doc_type_explicit=file.doc_type_explicit,
                    )
                )
                continue
            if scheduler is not None:
                scheduler(source_id)
            else:
                self.process_source(source_id, hooks)
            imported.append(
                UploadedSourceSummary.model_validate(
                    {**self.sources.get_source(source_id).model_dump(), "reused": False}
                )
            )
        return imported

    # ------------------------------------------------------------ pipeline
    def _emit_status(
        self, source_id: str, status: str, error_message: str = ""
    ) -> None:
        """Emit a status-machine transition to the event log. Split out of
        set_source_status so the atomic-claim paths (claim_failed_for_retry /
        claim_reextract_if_extracted), whose conditional UPDATE already wrote the
        row, can surface the SAME visible transition without a second,
        unconditional DB write that would defeat the guard."""
        self.event_log.emit(
            {
                "kind": "status",
                "source_id": source_id,
                "status": status,
                "error": error_message or "",
            }
        )

    def set_source_status(
        self,
        source_id: str,
        status: str,
        *,
        summary: Optional[str] = None,
        error_message: str = "",
    ) -> None:
        self.sources.set_status(
            source_id, status, summary=summary, error_message=error_message
        )
        # Emit every status-machine transition so it is visible in the event log.
        self._emit_status(source_id, status, error_message)

    def should_extract_kg(self, notebook_id: str) -> bool:
        """摄取期是否抽 KG:全局开关开,或该 notebook 已有 KG(续抽保持完整)。"""
        return self.settings.kg_auto_extract or self.notebook_has_kg(notebook_id)

    def process_source(
        self, source_id: str, hooks: SourcePipelineHooks
    ) -> SourceSummary:
        """Run the full parse -> embed -> extract pipeline with a status machine.

        States: queued -> parsing -> parsed -> extracting -> extracted (or failed).

        Each stage is timed and logged to the `events` channel so a "stuck"
        upload can be traced to the exact step (parse / embed / extract) and how
        long it has been running.
        """
        source = self.sources.get_source(source_id)
        notebook_id = source.notebook_id
        now = self.now()
        pipeline_started = time.perf_counter()

        def stage(name: str, status: str, started: float, **extra) -> None:
            self.event_log.emit(
                {
                    "kind": "pipeline",
                    "source_id": source_id,
                    "notebook_id": notebook_id,
                    "file_name": source.file_name,
                    "stage": name,
                    "status": status,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    **extra,
                }
            )

        # 进入即取内存租约(在置 parsing 之前)。见 __init__ 处说明:职责是在途误报
        # 抑制,不是崩溃检测。租约是**引用计数**——同一源可被并发处理(上传后台 job
        # 未完时 owner 又点 POST /sources/{id}/parse),每个 invocation 各加一次、
        # finally 各减一次,计数归零才真正撤租;否则先完成的 invocation 会撤掉仍在跑
        # 的另一个的租约,令其在途缺 elements/chunks 被体检误报损坏。下面 try 的
        # finally 覆盖所有出口做减。
        with self._active_sources_lock:
            self._active_sources[source_id] = self._active_sources.get(source_id, 0) + 1
        source_parse_lock: threading.Lock | None = None
        source_parse_lock_acquired = False
        parser_execution: ParserChainExecution | None = None
        parsed_assets_pending = False
        try:
            # 置 'parsing' 放在 try 内首行(不在 try 外):否则这句 DB 写若因磁盘满/
            # 写锁异常/库损坏抛出,会在进 try 前传出、绕过下面 finally 的租约释放,
            # 令该源被体检永久误抑制(假阴性)直到重启。放进来后其失败由 except 兜住
            # 落 'failed'、finally 照常 pop。stamp(上一行,锁内 dict 赋值)不抛,留在
            # try 外无妨。
            self.set_source_status(source_id, "parsing")
            # The accepted parser materializer deletes/replaces image assets.
            # Serialize that boundary with element/chunk replacement for the
            # same source so concurrent reparses cannot delete each other's
            # final asset generation. Different sources remain fully parallel.
            source_parse_lock = self._source_chunk_lock(source_id)
            source_parse_lock.acquire()
            source_parse_lock_acquired = True
            t = time.perf_counter()
            stage("parse", "start", t)
            parser_execution = ParserChainExecution(
                host=self.parser_provider_chain,
                source_id=source_id,
                source_kind="url" if source.source_url else "file",
                file_path=source.file_path,
                file_name=source.file_name,
                source_url=source.source_url,
                mineru_client=self.mineru_client(),
                cloud_client=self.mineru_cloud_client(),
                connection=self.parser_connection_probe,
                make_persist_image=lambda: self.make_persist_image(
                    source.notebook_id,
                    source_id,
                    getattr(source, "created_by", "") or "",
                ),
                delete_source_images=lambda: self.delete_source_images(source_id),
                event_sink=self.event_log.emit,
            )
            parsed = parser_execution.run()
            parsed_assets_pending = parser_execution.materialized
            elements, _ = deduplicate_repeated_page_boundaries(parsed.elements)
            mineru_error, parser_mode = parsed.mineru_error, parsed.parser_mode
            parser_warning_code = parsed.warning_code
            element_parsers = sorted(
                {
                    str(element.metadata.get("parser", ""))
                    for element in elements
                    if element.metadata.get("parser")
                }
            )
            stage(
                "parse",
                "done",
                t,
                elements=len(elements),
                parser_mode=parser_mode,
                actual_parsers=element_parsers,
                mineru_error=mineru_error[:500],
            )
            # per-source 分块串行锁:把「换 elements → 建 chunks + 置 marker」整段串起来
            # (见 __init__ 说明)。锁必须从 replace_elements 一直持到 build_chunks 之后,
            # 否则并发同源 reparse 会交错出「B 代 elements + A 代 chunks + marker 已置」的
            # 假完成。中间的 summarize/paper_meta(LLM)也在锁内——对**罕见**的并发同源
            # reparse 是正确的串行;单写(常见)路径下锁无竞争、零额外开销。
            # The same non-reentrant lock was acquired before provider I/O so
            # asset replacement, elements and chunks are one source generation.
            # Keep the existing block indentation without reacquiring it.
            with nullcontext():
                # elements 先落地(parse 的核心产物,不依赖 LLM):先清旧态再写 elements。
                with self.write() as db:
                    self.clear_source_extraction_state(
                        db,
                        source_id,
                        source.notebook_id,
                        clear_embeddings=True,
                    )
                    self.sources.replace_elements(
                        db,
                        source_id,
                        [
                            SourceElementWrite(
                                id=f"el-{source_id}-{index:04d}",
                                element_type=element.element_type,
                                location_label=element.location_label,
                                text=element.text,
                                metadata=element.metadata,
                            )
                            for index, element in enumerate(elements, start=1)
                        ],
                        created_at=now,
                    )
                    # 新代 elements 落库即令旧分块完成标记失效——同一写事务内归零
                    # chunked_at,无崩溃窗口。分块会在下面成功后重新置位;若分块
                    # 失败则留 NULL,正是 H3 的损坏信号。刻意就地一条而非折进
                    # clear_source_extraction_state(后者也被 KG 抽取复用、发生在分块之后)。
                    self.sources.clear_chunked_at(db, source_id)
                parsed_assets_pending = False
                parser_execution.mark_assets_committed()
                # 摘要(best-effort LLM)挪到 elements 落地之后:放在写库前会让 LLM 超时/
                # 失败/hang 把 elements 一起拖没——几万源集体丢 elements、KG 无从接地的根子。
                summary = self.summarize_source(source.title, elements)
                self.set_source_status(source_id, "parsed", summary=summary)

                # 论文元数据(best-effort):初次上传即抽,re-parse 时 force 刷新;
                # 失败不阻塞流水线。落库在终态转换前,前端轮询随状态变化带到。
                self.ensure_paper_metadata(source, elements=elements, force=True)

                # chunk-native 基础: 合并 element 成检索 chunk(纯写库无网络, query 立即可用)。
                # best-effort: 失败不阻塞既有 parse->extract 流水线。
                try:
                    self.chunking.build_chunks_for_source(source_id)
                except Exception:
                    self.event_log.logger.exception("chunk build failed for %s", source_id)
                    # Chunk build may have committed chunks (source now parsed =>
                    # pending KG) then failed before its kg_mutation_seq bump; with
                    # auto-extract off no later write bumps it. Drop the seq-gated
                    # chunk/pending memos so the next open recomputes, not serves stale.
                    self.invalidate_knowledge_counts(notebook_id)

            source_parse_lock.release()
            source_parse_lock_acquired = False
            source_parse_lock = None

            # Hints + notebook-meta augmentation do NOT depend on KG extraction
            # output; compute/persist them up front so the terminal 'extracted'
            # mark can be the LAST write. For the KG path that terminal mark is now
            # issued atomically-with-the-final-doc_type-recheck INSIDE
            # _extract_reconciling_doc_type (closing the retype window); computing
            # these after extraction would reopen a gap between "doc_type confirmed"
            # and "extracted".
            #
            # Surface "parsed to empty" (e.g. scanned/image PDF with no text layer)
            # instead of a silent success that looks like a real result.
            # file_name 在库里可为空(URL 来源/历史行),两处判据共用同一防御形态。
            suffix = ".pdf" if source.source_url else Path(source.file_name or "").suffix.lower()
            empty_hint = ""
            if not elements and suffix == ".pdf":
                empty_hint = (
                    "No extractable text — likely a scanned/image PDF. "
                    "Enable MinerU (MINERU_MODE) or add OCR to parse it."
                )
            fallback_hint = ""
            # 判据按「降级确实有损的后缀」而非只认 .pdf:docx/pptx 同样有 MinerU
            # 优先分支,它们的降级此前是静默的。xlsx/xlsm 刻意不在其中——openpyxl
            # 兜底对单元格值全保真,见 MINERU_FALLBACK_WARNING_SUFFIXES。其余两个
            # 条件不变——它们已经保证「MinerU 根本没配置时的正常兜底」不打警告。
            used_python_fallback_after_mineru_error = (
                parser_warning_code == PARSER_FALLBACK_WARNING_CODE
            )
            if used_python_fallback_after_mineru_error:
                # 前缀常量的名字与取值都不可改:四个存储层、shadow parity 与既有
                # 已入库行都靠它做前缀匹配。只有正文措辞去 PDF 化。
                fallback_hint = (
                    f"{PDF_PYTHON_FALLBACK_WARNING_PREFIX} "
                    "MinerU did not produce usable elements after retries; used a local "
                    "Python parser for this document. Layout, formulas, tables, or OCR may "
                    "still differ from MinerU output; reparse this source when MinerU is "
                    "available."
                )
                if mineru_error:
                    fallback_hint = f"{fallback_hint} Last MinerU error: {mineru_error[:500]}"
            # Keep the stable fallback prefix first even when the local parser
            # also finds zero text (common for scans/blank pages). Otherwise
            # `empty_hint or fallback_hint` hides the fallback fact and clients
            # lose parse_quality_warning + the reparse/delete recovery actions.
            terminal_msg = " ".join(
                message for message in (fallback_hint, empty_hint) if message
            )
            # Auto-fill notebook name/description from sources (only while name is a
            # default placeholder / purpose is still auto). Persist BEFORE the
            # terminal 'extracted' mark so the frontend's extracted-triggered
            # refetch shows the fresh name/description live. It reads source
            # metadata (title/summary/doc_type), never KG objects, and this source
            # is counted via pending_source_id whether it is 'parsed' or
            # 'extracting' — so running it before extraction gives the same input.
            # Best-effort: never fail the pipeline.
            try:
                hooks.augment_notebook_metadata(source.notebook_id, source_id)
            except Exception:
                self.event_log.logger.exception(
                    "notebook meta augmentation failed for %s", source_id
                )

            # parse -> extract hand-off: flip 'parsed' -> 'extracting'. Emitted before
            # the background embed thread starts, so the event order stays
            # status:extracting -> embed:start.
            self.set_source_status(source_id, "extracting")

            # Element embedding (best-effort semantic recall) runs in the BACKGROUND,
            # concurrent with KG extraction, so a large doc's slow embed never blocks
            # the KG result. 'extracted'/green below is gated on EXTRACTION only.
            embed_started = time.perf_counter()
            stage("embed", "start", embed_started)

            def _embed_bg() -> None:
                try:
                    self.embedding.embed_source(source_id)
                    # chunk 向量后台补, 不阻塞流水线
                    self.embedding.embed_chunks_for_source(source_id)
                    stage("embed", "done", embed_started)
                except Exception as exc:  # noqa: BLE001 — best-effort; never fail the pipeline
                    stage("embed", "error", embed_started,
                          error=f"{type(exc).__name__}: {exc}")
                    self.event_log.logger.exception(
                        "background embed failed for %s", source_id
                    )
                finally:
                    # 嵌入 worker 完成才撤它这份「在嵌入」标记(见下面 spawn 前的 stamp)。
                    self._release_embedding_source(source_id)

            # 嵌入 worker 单独记「在嵌入」(codex 第5轮 P2):process_source 的活跃租约在它自己 finally
            # 里就撤了,而**后台嵌入可能还没写完向量**——H4/H5 用活跃集(见 _active_source_ids_snapshot,
            # 并入 _embedding_sources)排除在途源,不给这个 worker 单记一份的话,这段 in-flight 会被误报
            # 缺 chunk/element 向量(假告警)+诱发重复的、可能昂贵的 backfill。刻意用**独立的**
            # _embedding_sources 而非 _active_sources:后者是 process_source 生命周期 + 分块锁的引用计数,
            # 嵌入 worker 会**晚于** process_source 结束,混进去会打乱那套租约/锁语义。stamp 必须在 start
            # 之前(否则线程先跑到 embed、标记还没记上有窗口)。
            with self._active_sources_lock:
                self._embedding_sources[source_id] = (
                    self._embedding_sources.get(source_id, 0) + 1
                )
            embed_ctx = contextvars.copy_context()
            embed_thread = threading.Thread(
                target=lambda: embed_ctx.run(_embed_bg),
                name=f"embed-{source_id}", daemon=True
            )
            try:
                embed_thread.start()
            except BaseException:  # 起线程失败:_embed_bg 不会跑、其 finally 撤不了 → 这里补撤,免泄漏
                self._release_embedding_source(source_id)
                raise

            if hooks.should_extract_kg(notebook_id):
                # Status already flipped to 'extracting' above.
                t = time.perf_counter()
                stage("extract", "start", t)
                # 同一条源第一次上传时也存在「抽取跑到一半用户改类型」的窗口（他嫌慢，
                # 用正确的类型把同一个文件又传了一次）：reuse_uploaded_source 对
                # 'extracting' 的行只记类型不调度，由这里跑完自校验并按新类型补跑；
                # 成功的终态 'extracted' 由 _extract_reconciling_doc_type 与 doc_type
                # 最终比对**原子**落下（收口 retype 窗口，见其 docstring）。
                self._extract_reconciling_doc_type(
                    source_id, hooks.extract_source,
                    terminal_error_message=terminal_msg,
                )
                stage("extract", "done", t)
                # reconcile 已原子落 'extracted'（DB）；这里补发 status 事件，放在
                # stage extract:done 之后以保持既有事件顺序（见其 docstring）。
                self._emit_status(source_id, "extracted", terminal_msg)
                try:
                    hooks.mark_unified_dirty(source.notebook_id)
                except Exception:
                    self.event_log.logger.exception(
                        "unified-KG dirty mark failed for source %s", source_id
                    )
            else:
                # 不抽 KG：没有 doc_type/profile 一致性可谈，直接落终态。
                self.set_source_status(
                    source_id, "extracted", error_message=terminal_msg
                )
            # KG ('extracted'/green) set above; wait for the background element
            # embedding to finish before declaring the whole pipeline done.
            embed_thread.join()
            stage("pipeline", "done", pipeline_started, elements=len(elements))
        except Exception as exc:
            stage("pipeline", "error", pipeline_started, error=f"{type(exc).__name__}: {exc}")
            self.event_log.logger.exception("process_source failed for %s", source_id)
            self.set_source_status(
                source_id,
                "failed",
                summary="Parsing failed; see source error.",
                error_message=str(exc),
            )
        finally:
            # 覆盖 try 的所有出口——成功 return、上面的 except 落 'failed'(KgBuildAborted
            # 等 Exception 子类都被它兜住)、以及未被 except 捕获而向上传出的
            # BaseException:一律给租约计数减一。置 'parsing' 也在 try 内(见上),故进入
            # try 后再无未覆盖的泄漏边。计数归零才真正撤租(见 stamp 处:并发处理同一源
            # 时先完成者不能撤掉仍在跑者的租约),并连该源分块锁一并清除(有界化)。刻意早于
            # 下面的 maybe_enqueue_scale_fold,后者是独立的空闲收尾、不需要持租约。此处的
            # 分块锁在本方 with 块内已释放,减租约到 0 时可安全 pop(与 backfill 守卫共用
            # _release_source_lease:backfill 也登记租约,故本方持锁的窗口里锁不会被它 pop)。
            # An accepted materializer may have persisted images before a
            # downstream Exception *or BaseException* interrupts the element
            # transaction. Never leave that uncommitted asset generation
            # behind. Once replace_elements commits, the flag is cleared and
            # the generation becomes authoritative.
            try:
                if parsed_assets_pending or (
                    parser_execution is not None
                    and parser_execution.assets_pending
                ):
                    try:
                        self.delete_source_images(source_id)
                    except BaseException:
                        self.event_log.logger.exception(
                            "uncommitted parser assets cleanup failed for %s", source_id
                        )
            finally:
                try:
                    if source_parse_lock is not None and source_parse_lock_acquired:
                        source_parse_lock.release()
                finally:
                    self._release_source_lease(source_id)
        # Content-add settle point: if this notebook already has a scale index,
        # enqueue an idle incremental fold so the new (post-watermark) source
        # becomes semantically searchable. Idle queue coalesces batch runs (many
        # process_source calls) into a single fold. Never builds a fresh index;
        # helper is fail-safe (never raises).
        hooks.maybe_enqueue_scale_fold(source.notebook_id)
        # Agentic Memory P1 (T4): one source-lifecycle event for this notebook.
        # Placed alongside the fold enqueue and AFTER the lease release for the
        # same reason: it is an independent idle-time settle point that needs
        # no lease, and it is itself fail-open (a background understanding
        # refresh that cannot be scheduled must never turn a successful
        # (re)parse into a failure). New sources and reparses BOTH land here —
        # `parse_source` delegates to this method.
        #
        # Reached on EVERY terminal path of the pipeline, including the `except`
        # branch above that lands `failed` — deliberately: a document that
        # yielded nothing IS a corpus-shape signal (it is exactly what
        # `corpus_gaps` reports), and the counter is a "something changed here"
        # accumulator, not a success counter.
        #
        # Hidden synthetic rows (memory/knowhow projections) are NOT corpus
        # changes: the shared understanding is built from user-visible documents
        # only, and a confirmed Memory belongs to one member — letting one
        # member's Memory advance a notebook-shared counter would spend the
        # whole notebook's model call on an event the other members cannot see.
        # Same predicate the statistics use (`VISIBLE_SOURCE_TYPES_PREDICATE`
        # via this constant), evaluated on the row already in hand.
        if source.type not in HIDDEN_SYNTHETIC_SOURCE_TYPES:
            hooks.note_corpus_change(source.notebook_id)
        return self.sources.get_source(source_id)

    def parse_source(
        self, source_id: str, hooks: SourcePipelineHooks
    ) -> SourceSummary:
        # Manual (re)parse is always synchronous so the response reflects the result.
        return self.process_source(source_id, hooks)

    def augment_notebook_metadata(
        self, notebook_id: str, pending_source_id: str = ""
    ) -> None:
        """Auto-fill the notebook name (while it is still a default placeholder)
        and/or description (while purpose_auto=1) from its processed sources.
        No-op for fields the user has set. `pending_source_id` is the source whose
        pipeline is finishing (still 'extracting'); it is counted so the FIRST
        source already produces a name/description."""
        meta = self.notebook_meta_row(notebook_id)
        if meta is None:
            return
        cur_name = (meta["name"] or "").strip()
        need_name = cur_name in self.default_notebook_names
        need_desc = meta["purpose_auto"]
        if not (need_name or need_desc):
            return
        rows = self.notebook_meta_sources(notebook_id, pending_source_id)
        if not rows:
            return
        titles = [r["title"] for r in rows]
        labels = []
        for r in rows:
            profile = PROFILES.get(self.normalize_doc_type(r["doc_type"]))
            label = profile.label if profile else "自动检测"
            if label not in labels:
                labels.append(label)

        name_val, desc_val = "", ""
        llm_client = self.model_clients.chat("notebook_metadata")
        if llm_client.configured:
            block = "\n".join(
                f"- {r['title']} "
                f"[{(PROFILES.get(self.normalize_doc_type(r['doc_type'])) or get_profile('academic_paper')).label}] "
                f"{(r['summary'] or '')[:200]}"
                for r in rows[:20]
            )
            try:
                raw = llm_client.chat_json(
                    [{"role": "user", "content": notebook_meta_prompt(block)}],
                    NOTEBOOK_META_SCHEMA_HINT,
                )
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    name_val = str(parsed.get("name", "")).strip()
                    desc_val = str(parsed.get("description", "")).strip()
            except Exception:
                name_val, desc_val = "", ""

        # Deterministic fallbacks (LLM off or failed).
        if need_desc and not desc_val:
            shown = "、".join(titles[:5]) + ("等" if len(titles) > 5 else "")
            desc_val = f"本笔记本收录了 {len(titles)} 个来源：{shown}。"
            if labels:
                desc_val += f"文档类型涵盖 {'、'.join(labels)}。"
        if need_name and not name_val:
            name_val = (titles[0] or "").strip()[:40]

        # Optimistic guard rides guard_name: only overwrite while the name is
        # still the placeholder we read (no clobber of a concurrent rename).
        self.apply_notebook_meta(
            notebook_id,
            guard_name=meta["name"],
            name=(name_val[:120] if need_name and name_val else ""),
            purpose=(desc_val[:1000] if need_desc and desc_val else ""),
        )

    def delete_source(self, source_id: str, hooks: SourcePipelineHooks) -> None:
        source = self.sources.get_source(source_id)
        with self.write() as db:
            # Extraction's terminal write holds a KEY SHARE lock on this row.
            # Take the conflicting aggregate lock before scanning/deleting any
            # projections so extraction cannot publish KG after our cursor.
            if self.sources.source_exists_for_update_tx(db, source_id):
                self.clear_source_extraction_state(
                    db,
                    source_id,
                    source.notebook_id,
                    clear_embeddings=True,
                )
                self.sources.delete_source_row(db, source_id)
        self.source_files.delete(source.file_path)
        self.delete_source_images(source_id)  # Task 9: cascade-clean MinerU image assets
        self.kg_mutations.invalidate_unified_cache(source.notebook_id)
        hooks.mark_unified_dirty(source.notebook_id)
        # A deletion changes the corpus exactly as much as an addition does —
        # and it is the event that most often invalidates an existing
        # understanding block (the document a claim cited is gone). Last step,
        # after the deletion itself has fully committed, so a failure here can
        # only lose a background refresh, never the delete.
        #
        # Hidden synthetic rows are excluded here for a sharper reason than on
        # the ingest side: deleting a Memory-derived source is how a member
        # REVOKES a private Memory, and letting that advance the notebook-shared
        # counter would let one member's private housekeeping trigger (and pay
        # for) everyone's shared refresh. See the ingest call site for the
        # single-predicate argument.
        if source.type not in HIDDEN_SYNTHETIC_SOURCE_TYPES:
            hooks.note_corpus_change(source.notebook_id)

    # ------------------------------------------------------ memory-derived
    def memory_kg_eligible(self, notebook_id: str) -> bool:
        """Memory 确认后是否自动抽 KG：与上传同门（should_extract_kg）+ base 库
        排除（进 base 走晋升人审，从不自动抽取）。"""
        return self.should_extract_kg(notebook_id) and self.notebook_tier(notebook_id) != "base"

    def memory_source_id(self, memory_id: str) -> Optional[str]:
        return self.sources.source_id_for_memory(memory_id)

    def ingest_memory_source(
        self, notebook_id: str, memory_id: str, title: str, content_md: str
    ) -> Optional[str]:
        """Memory→隐藏合成源→真实抽取管线（对象/关系/证据/增量融合，与上传逐字
        同一条 run_extraction）。在调用方的 job 线程内同步运行。

        失败语义：notebook 不存在直接 KeyError（调用方兜——那是调用方 bug，不该
        留下孤儿 failed 源）；其余从 insert 到抽取的**任何**失败都落
        parse_status='failed'+error_message 并清空 file_hash（指纹只代表
        「成功摄取过的内容」——不清掉，指纹仍匹配会把同内容重调短路进坏源、
        永不重试），从不向外抛（Memory 本体不受影响）。

        不建 chunk——Memory 文本已经由 MemoryRetriever 直接注入 prompt，建 chunk
        会造成双份注入。只建 elements + element embedding。

        指纹 = sha256(title + "\\n" + content_md)：内容未变（如仅改 tags）零代价
        跳过，不重抽；变化时清掉该源之前的抽取产物（KG 对象/关系/embeddings/
        extraction_runs）后重新解析+重抽（title 在指纹内，重抽时一并刷新），
        从不追加。
        """
        # KeyError guard stays OUTSIDE the failure boundary below: same intake
        # contract as import_sources/upload_sources, and there is no source
        # row to mark 'failed' yet.
        self.notebooks.get_row(notebook_id)
        fingerprint = hashlib.sha256(
            f"{title}\n{content_md}".encode("utf-8")
        ).hexdigest()
        source_id: Optional[str] = None
        try:
            existing_id = self.sources.source_id_for_memory(memory_id)
            if existing_id is not None:
                source_id = existing_id
                if self.sources.get_source(existing_id).file_hash == fingerprint:
                    return existing_id  # content unchanged (e.g. only tags edited)
            else:
                source_id = self.new_id("src")
                self.sources.insert_source(
                    source_id=source_id,
                    notebook_id=notebook_id,
                    title=title,
                    source_type="memory",
                    status="active",
                    parse_status="parsed",
                    file_name="",
                    file_path="",
                    file_size=0,
                    file_hash=fingerprint,
                    summary="",
                    doc_type="",
                    memory_id=memory_id,
                )

            from app.services.parsers import parse_markdown_text

            elements = parse_markdown_text(source_id, content_md)
            now = self.now()
            with self.write() as db:
                if existing_id is not None:
                    # Reparse semantics: drop this source's prior extraction
                    # derivatives (KG objects/relations/embeddings/extraction
                    # runs) in the SAME transaction as the element swap below —
                    # mirrors process_source's parse-stage invariant exactly.
                    self.clear_source_extraction_state(
                        db,
                        source_id,
                        notebook_id,
                        clear_embeddings=True,
                    )
                    # title rides the fingerprint (sha256(title+content)), so
                    # a title change re-lands here: refresh it together with
                    # the fingerprint in the same UPDATE.
                    self.sources.update_file_hash(
                        source_id, fingerprint, title=title, connection=db
                    )
                self.sources.replace_elements(
                    db,
                    source_id,
                    [
                        SourceElementWrite(
                            id=f"el-{source_id}-{index:04d}",
                            element_type=element.element_type,
                            location_label=element.location_label,
                            text=element.text,
                            metadata=element.metadata,
                        )
                        for index, element in enumerate(elements, start=1)
                    ],
                    created_at=now,
                )

            try:
                # Deliberately embed_source only — NEVER embed_chunks_for_source,
                # since no chunks are ever built for a memory-derived source.
                # Best-effort, like the upload pipeline's background embed.
                self.embedding.embed_source(source_id)
            except Exception:
                self.event_log.logger.exception(
                    "memory source embed failed for %s", source_id
                )

            self.sources.set_status(source_id, "extracting")
            self.run_extraction(source_id)
            self.sources.set_status(source_id, "extracted")
            # Mirror process_source: the dirty bump is a real DB write that
            # runs AFTER 'extracted' is recorded (the KG objects are already
            # stored) — its failure is log-only and must never flip an
            # actually-extracted source back to 'failed'.
            try:
                self.kg_mutations.mark_unified_kg_dirty(notebook_id)
            except Exception:
                self.event_log.logger.exception(
                    "unified-KG dirty mark failed for source %s", source_id
                )
            self.event_log.emit({
                "kind": "memory_kg",
                "source_id": source_id,
                "notebook_id": notebook_id,
                "memory_id": memory_id,
                "status": "extracted",
            })
        except Exception as exc:
            self.event_log.logger.exception(
                "memory source ingestion failed for %s", source_id or memory_id
            )
            if source_id is not None:
                try:
                    # The fingerprint means "successfully ingested content":
                    # clear it FIRST so an identical retry re-runs instead of
                    # fingerprint-skipping into the broken row forever —
                    # retryability outranks status labeling if the second
                    # UPDATE below were itself to fail.
                    self.sources.update_file_hash(source_id, "")
                    self.sources.set_status(
                        source_id, "failed",
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    # The failure fallback itself must never raise.
                    self.event_log.logger.exception(
                        "failed-state bookkeeping failed for %s", source_id
                    )
            self.event_log.emit({
                "kind": "memory_kg",
                "source_id": source_id or "",
                "notebook_id": notebook_id,
                "memory_id": memory_id,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
        return source_id

    def remove_memory_source(self, memory_id: str) -> None:
        """弃用 Memory 派生源：无派生源（从未抽取过，或已被移除）时幂等 no-op。"""
        source_id = self.sources.source_id_for_memory(memory_id)
        if source_id is not None:
            self.delete_source(source_id, self.pipeline_hooks())

    # ----------------------------------------------------------- extraction
    @staticmethod
    def _relation_completion_needs_resume(stats: dict) -> bool:
        return bool(
            stats.get("mode") in {"shadow", "write"}
            and not stats.get("exhausted")
            and not stats.get("generation_conflict")
            and not stats.get("skip_reason")
        )

    def _schedule_relation_completion_resume(
        self, notebook_id: str, source_id: str, source_title: str, run_id: str,
        expected_mode: str,
    ) -> bool:
        """Single-flight one bounded resume invocation; re-enqueue if pending."""
        from app.services.kg import scheduler as kg_scheduler

        if expected_mode not in {"shadow", "write"}:
            return False
        key = (source_id, run_id, expected_mode)
        with self._relation_completion_schedule_lock:
            if key in self._relation_completion_scheduled:
                return False
            self._relation_completion_scheduled.add(key)

        def _run() -> None:
            stats: dict | None = None
            try:
                stats = self.knowledge_lifecycle.complete_relations_for_source(
                    notebook_id, source_id, source_title, run_id, [], [], [],
                    expected_mode=expected_mode,
                )
                if stats.get("inserted"):
                    try:
                        self.maybe_auto_index(notebook_id)
                    except Exception:
                        self.event_log.logger.exception(
                            "relation completion resume indexing failed for %s",
                            source_id,
                        )
            except Exception:
                # Keep the durable cursor pending. A later startup or explicit
                # source run can retry without replaying completed pages.
                self.event_log.logger.exception(
                    "relation completion resume failed for %s", source_id
                )
            finally:
                with self._relation_completion_schedule_lock:
                    self._relation_completion_scheduled.discard(key)
            if stats is not None and self._relation_completion_needs_resume(stats):
                self._schedule_relation_completion_resume(
                    notebook_id, source_id, source_title, run_id, expected_mode
                )
            elif stats is not None and stats.get("skip_reason") == "mode_changed":
                current_mode = str(stats.get("current_mode") or "off")
                if current_mode in {"shadow", "write"}:
                    self._schedule_relation_completion_resume(
                        notebook_id, source_id, source_title, run_id, current_mode
                    )

        try:
            kg_scheduler.submit_job(_run)
        except Exception:
            with self._relation_completion_schedule_lock:
                self._relation_completion_scheduled.discard(key)
            self.event_log.logger.exception(
                "relation completion resume submission failed for %s", source_id
            )
            return False
        return True

    def resume_pending_relation_completions(self, page_size: int = 100) -> int:
        """Schedule every current pending source state in bounded metadata pages."""
        from app.services.kg.relation_completion import completion_mode_for_notebook

        cursor_source = ""
        cursor_mode = ""
        scheduled = 0
        page_size = max(1, int(page_size))
        while True:
            rows = self.knowledge_lifecycle.pending_relation_completions(
                cursor_source, cursor_mode, page_size
            )
            if not rows:
                break
            for row in rows:
                notebook_id = str(row["notebook_id"])
                source_id = str(row["source_id"])
                source_title = str(row.get("title") or "")
                run_id = str(row["source_generation"])
                persisted_mode = str(row["mode"])
                current_mode = completion_mode_for_notebook(
                    self.settings, notebook_id
                )
                if persisted_mode != current_mode:
                    if current_mode in {"shadow", "write"}:
                        transitioned = (
                            self.knowledge_lifecycle.transition_relation_completion_mode(
                                notebook_id, source_id, run_id,
                                persisted_mode, current_mode,
                            )
                        )
                        if transitioned:
                            scheduled += int(self._schedule_relation_completion_resume(
                                notebook_id, source_id, source_title, run_id, current_mode
                            ))
                    else:
                        self.knowledge_lifecycle.mark_relation_completion_stale(
                            notebook_id, source_id, run_id, persisted_mode
                        )
                else:
                    scheduled += int(self._schedule_relation_completion_resume(
                        notebook_id, source_id, source_title, run_id, persisted_mode
                    ))
            cursor_source = str(rows[-1]["source_id"])
            cursor_mode = str(rows[-1]["mode"])
            if len(rows) < page_size:
                break
        return scheduled

    def relink_extra_relations(
        self, objects: List[dict], relations: List[dict], source_id: str
    ) -> List[dict]:
        """PURE: propose deterministic relink edges for degree-0 nodes within ONE
        source's freshly-extracted (objects, relations), returning them in the same
        shape build_records emits so store_kg can remap local→DB ids and persist
        them with the SAME review_status/source_id as LLM edges.

        Adapts the (objects, relations) extraction shape to the relink core's node
        dict, then maps each new edge back to a relation dict. No DB / IO."""
        from app.services.kg.relink import complete_isolated_edges

        nodes = [
            {
                "id": o["local_id"],
                "object_type": o["object_type"],
                "name": (o.get("payload") or {}).get("name", ""),
                "source_id": source_id,
                "element_ids": {
                    ev.get("element_id")
                    for ev in o.get("evidence", [])
                    if ev.get("element_id")
                },
            }
            for o in objects
        ]
        edges = [(r["source_local_id"], r["target_local_id"]) for r in relations]
        extra = complete_isolated_edges(nodes, edges)
        return [
            {
                "source_local_id": e["source_object_id"],
                "target_local_id": e["target_object_id"],
                "edge_type": e["edge_type"],
                "evidence": [{"basis": e["basis"], "quote": ""}],
            }
            for e in extra
        ]

    def run_extraction(
        self,
        source_id: str,
        *,
        kg_client: Any | None = None,
        preserve_existing_until_complete: bool = False,
    ) -> None:
        control = getattr(kg_client, "control", None)
        if control is not None:
            control.raise_if_aborted()
        source: SourceDetail = self.sources.get_source(source_id)
        elements = self.source_elements(source_id)
        # 历史源 catch-up:补论文元数据(幂等,有行即跳)。ensure_paper_metadata 的
        # try/except 包住幂等读之后的整个方法体(不止 LLM 调用),保证它绝不向外
        # 抛异常——失败真正不影响 KG 抽取(不会中断下面紧接着的抽取流程),而不只
        # 是注释里的一句希望。
        self.ensure_paper_metadata(source, elements=elements, force=False)
        if control is not None:
            control.raise_if_aborted()
        now = self.now()
        run_id = self.new_id("run")
        doc_type_id = (
            self.normalize_doc_type(getattr(source, "doc_type", "") or "")
            or "academic_paper"
        )
        kg_doc_type = kg_ingest.DOC_TYPE_MAP.get(doc_type_id, "academic")
        # Normal extraction resets the old source graph up front. A partial-KG
        # repair instead opens a run while retaining the old graph; only a
        # zero-failed-window replacement may swap it later.
        self.begin_extraction_run(
            source_id,
            source.notebook_id,
            run_id,
            now,
            preserve_existing=preserve_existing_until_complete,
        )
        self.invalidate_knowledge_counts(source.notebook_id)
        try:
            kg_llm_client = kg_client if kg_client is not None else self.model_clients
            if not (
                kg_llm_client.configured("kg_extract")
                if callable(getattr(kg_llm_client, "configured", None))
                else getattr(kg_llm_client, "configured", False)
            ):
                if preserve_existing_until_complete:
                    message = (
                        "partial KG retry incomplete; existing KG preserved "
                        "retry_incomplete=1 no-llm"
                    )
                    self.finish_extraction_run(run_id, "completed", message)
                    raise PartialKgRetryIncomplete(message)
                self.finish_extraction_run(run_id, "completed", "no-llm")
                return
            raw_text = self.source_files.read_source_text(
                getattr(source, "file_path", "") or "", elements
            )
            model_parallelism = (
                kg_llm_client.parallelism("kg_extract")
                if callable(getattr(kg_llm_client, "parallelism", None))
                else 1
            )
            n_chars = kg_ingest.plan_window_size(
                len(raw_text), model_parallelism,
                self.settings.kg_window_min_chars, self.settings.kg_window_max_chars,
                override=self.settings.kg_window_target_chars,
            )
            whitelist = self.concept_whitelist_terms()
            base_filter = self.notebook_tier(source.notebook_id) == "base"
            graph = kg_ingest.extract_graph(
                kg_llm_client, raw_text, source.file_name or "source.md", kg_doc_type,
                n=n_chars,
                m=self.settings.kg_window_overlap_chars,
                whitelist=whitelist,
                refine=self.settings.kg_refine_enabled,
                gleaning_rounds=(
                    self.settings.kg_gleaning_rounds
                    if self.settings.kg_gleaning_enabled else 0
                ),
                base_filter=base_filter,
            )
            warn = self.settings.kg_window_warn_threshold
            if graph.total_windows > warn:
                self.event_log.logger.warning(
                    "KG windows %s exceed warn threshold %s for source %s (%s) — "
                    "extracting in full, no truncation",
                    graph.total_windows, warn, source_id, source.file_name,
                )
            objects, relations = kg_ingest.build_records(
                graph, source.id, source.title, elements
            )
            # Reconnect degree-0 nodes BEFORE store_kg, so the relink edges go
            # through the same local→DB remap + review_status/source_id as LLM
            # edges (esp. gleaning, which emits edgeless nodes). Intra-source only.
            if getattr(self.settings, "kg_relink_enabled", True):
                relations = relations + self.relink_extra_relations(
                    objects, relations, source.id
                )
            fw, tw = graph.failed_windows, graph.total_windows
            if preserve_existing_until_complete and (fw > 0 or not objects):
                message = (
                    "partial KG retry incomplete; existing KG preserved "
                    "retry_incomplete=1 "
                    f"windows_failed={fw}/{tw} "
                    f"empty_result={int(not objects)} "
                    f"candidate_objects={len(objects)} "
                    f"candidate_relations={len(relations)}"
                )
                self.finish_extraction_run(run_id, "completed", message)
                raise PartialKgRetryIncomplete(message)
            if control is not None:
                control.raise_if_aborted()
            n_obj, n_rel = self.knowledge_lifecycle.store_kg(
                source.notebook_id,
                source.id,
                objects,
                relations,
                replace_source=preserve_existing_until_complete,
                source_generation=run_id,
            )
            try:
                self.knowledge_lifecycle.incremental_fuse_source(
                    source.notebook_id, source.id
                )
            except Exception:
                self.event_log.logger.exception(
                    "incremental_fuse_source failed for %s", source_id
                )
            completion_stats = {"mode": "off", "inserted": 0}
            try:
                completion_stats = self.knowledge_lifecycle.complete_relations_for_source(
                    source.notebook_id,
                    source.id,
                    source.title,
                    run_id,
                    [],
                    [],
                    [],
                    cancel_check=(
                        control.raise_if_aborted if control is not None else (lambda: None)
                    ),
                )
                if self._relation_completion_needs_resume(completion_stats):
                    self._schedule_relation_completion_resume(
                        source.notebook_id, source.id, source.title, run_id,
                        str(completion_stats["mode"]),
                    )
            except KgBuildAborted:
                raise
            except Exception:
                # Completion is an opt-in best-effort stage.  A model/provider
                # failure must not roll back the already committed source KG.
                self.event_log.logger.exception(
                    "relation completion failed for %s", source_id
                )
            try:
                self.maybe_auto_index(source.notebook_id)
            except Exception:
                self.event_log.logger.exception(
                    "maybe_auto_index failed for %s", source.notebook_id
                )
            self.finish_extraction_run(
                run_id,
                "completed",
                f"{KG_RUN_MESSAGE_OBJECTS_PREFIX}{n_obj} relations={n_rel} "
                f"doc_type={kg_doc_type} "
                f"windows_failed={fw}/{tw} windows_skipped={graph.windows_skipped} "
                f"concepts_dropped={graph.concepts_dropped} claims_dropped={graph.claims_dropped} "
                f"completion_mode={completion_stats.get('mode', 'off')} "
                f"completion_inserted={completion_stats.get('inserted', 0)}",
            )
        except KgBuildAborted as exc:
            message = f"{exc.failure.code}: {exc.failure.user_message}"
            status = "failed"
            if preserve_existing_until_complete:
                status = "completed"
                message = (
                    "partial KG retry incomplete; existing KG preserved "
                    f"retry_incomplete=1 {message}"
                )
            self.finish_extraction_run(
                run_id,
                status,
                message,
            )
            raise
        except PartialKgRetryIncomplete:
            raise
        except Exception as exc:
            message = str(exc)
            status = "failed"
            if preserve_existing_until_complete:
                status = "completed"
                message = (
                    "partial KG retry incomplete; existing KG preserved "
                    f"retry_incomplete=1 {message}"
                )
            self.finish_extraction_run(run_id, status, message)
            raise

    # ---------------------------------------------------- paper metadata
    def ensure_paper_metadata(
        self,
        source: "SourceSummary | SourceDetail",
        elements: Optional[List[SourceElement]] = None,
        force: bool = False,
        *,
        client: Any | None = None,
    ) -> str:
        """单源论文元数据抽取(best-effort,幂等)。返回状态串 stored/not_paper/
        skipped/disabled/no_llm/no_text/failed,仅供调用方统计,不进状态机。
        挂载点:process_source(force=True,re-parse 即刷新)与 run_extraction 开头
        (force=False,历史源 catch-up);批量走 backfill_paper_metadata。
        成本:每源一次 chat_json(头部 ~paper_meta_head_chars 字符,输出~300 token);
        gate=paper_meta_enabled ∧ doc_type∈{'',academic_paper} ∧ 非合成源 ∧ LLM
        已配 ∧ 有文本 ∧ (force ∨ 无行)。失败不写行(下次可重试)、不碰
        extraction_runs、不阻断流水线(摄取侧惯例,不用 note_model_error)。"""
        if not getattr(self.settings, "paper_meta_enabled", True):
            return "disabled"
        if source.type in ("memory", "knowhow"):
            return "skipped"
        # 抽取合格判定与「retype 到不合格类型清旧元数据」共用同一 predicate
        # (paper_meta_doc_type_eligible),单一定义点、不会各写一半再漂移。传入归一化
        # 后的值('' 表自动/默认,predicate 视为合格)。
        doc_type = self.normalize_doc_type(getattr(source, "doc_type", "") or "")
        if not paper_meta_doc_type_eligible(doc_type):
            return "skipped"
        # From here on EVERYTHING is inside the try: the idempotency read,
        # the LLM-configured probe, element/text hydration and the LLM call
        # itself. Any of these can raise (a store read, a config resolution
        # bug, a file-read error) and must degrade to "failed" the same as an
        # LLM error — this method must NEVER escape an exception, because its
        # two mount points (process_source, run_extraction's historical-source
        # catch-up) both run KG extraction immediately afterward and an
        # uncaught exception here would abort that call chain before KG
        # extraction ever runs.
        try:
            if not force and self.sources.get_paper_meta(source.id) is not None:
                return "skipped"
            client = client or self.model_clients.chat("paper_metadata")
            if not getattr(client, "configured", False):
                return "no_llm"
            if elements is None:
                elements = self.source_elements(source.id)
            head_chars = int(getattr(self.settings, "paper_meta_head_chars", 4000))
            head_text = self.source_files.read_source_text(
                getattr(source, "file_path", "") or "", elements
            )[:head_chars]
            if not head_text.strip():
                return "no_text"
            raw = client.chat_json(
                [{"role": "user", "content": paper_meta_prompt(head_text)}],
                PAPER_META_SCHEMA_HINT,
                temperature=0.0,
                **cap_kwargs(client, "openai_compat_max_tokens"),
            )
            # safe_json() always coerces to a dict (its contract), silently
            # swallowing whatever the true top-level JSON shape was — an
            # empty-dict result is then indistinguishable from a genuine
            # LLM {} response. That matters here specifically: verify_
            # paper_meta({}, ...) yields a *valid* is_paper=False marker,
            # which upsert_paper_meta persists as "already tried, not a
            # paper" and permanently suppresses retries. A model that
            # emits array-wrapped/scalar garbage (e.g. "[]") must NOT get
            # that treatment — it should fail this attempt and stay
            # retryable. So probe the raw JSON's real top-level shape
            # first (json.loads, no cleanup) and only fall back to
            # safe_json's think-tag/code-fence stripping when the direct
            # parse fails outright.
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = safe_json(raw)
            if (
                isinstance(parsed, list)
                and len(parsed) == 1
                and isinstance(parsed[0], dict)
            ):
                parsed = parsed[0]  # single-object-in-array: unwrap, don't discard
            if not isinstance(parsed, dict):
                raise ValueError("paper-meta LLM returned non-object JSON")
            meta = verify_paper_meta(
                parsed, head_text,
                model=str(getattr(client, "model", "") or ""),
            )
            self.sources.upsert_paper_meta(source.id, source.notebook_id, meta)
            self.event_log.emit({
                "kind": "paper_meta",
                "source_id": source.id,
                "notebook_id": source.notebook_id,
                "is_paper": bool(meta["is_paper"]),
                "authors": len(meta["authors"]),
                "dropped": meta["dropped"],
            })
            return "stored" if meta["is_paper"] else "not_paper"
        except Exception:
            self.event_log.logger.exception(
                "paper metadata extraction failed for %s", source.id
            )
            return "failed"

    def backfill_paper_metadata(
        self,
        notebook_id: str,
        force: bool = False,
        progress: Optional[Callable[[int, int, str, str], None]] = None,
    ) -> dict:
        """批量补抽缺论文元数据的源(CLI phase=metadata 与应用内端点共用)。
        幂等键=meta 行存在;失败源不落行,重跑自动重试(断点续跑)。有界并发
        取自 ``paper_metadata`` 所绑定模型服务的并行度，并在原始 worker
        启动前解析一次 workload client。返回
        {"total": N, "<status>": n, ...} 计数。成功收尾
        (stored>0)经 pending_bus 广播 paper_meta_done 铃铛事件,见
        _notify_paper_meta_done。"""
        nb_row = self.notebooks.get_row(notebook_id)  # KeyError if missing
        targets = self.sources.sources_missing_paper_meta(
            notebook_id, include_existing=force
        )
        counts: dict = {"total": len(targets)}
        if not targets:
            return counts
        # Resolve the workload-bound adapter before raw worker threads. The
        # scheduler remains the authoritative global cap; matching the pool to
        # this service avoids manufacturing excess blocked worker calls.
        paper_client = self.model_clients.chat("paper_metadata")
        workers = max(
            1,
            min(
                self.model_clients.parallelism("paper_metadata"),
                len(targets),
            ),
        )
        lock = threading.Lock()
        done = 0

        # 注册状态（重复 backfill 同一 nb 会覆盖，符合"最新一次"语义；
        # 生成 gen token 供 _one/finally 世代校验，避免先来者的 finally 弹
        # 掉后来者的 entry / 先来者的晚 worker 串写后来者的 done）
        with self._paper_meta_backfilling_lock:
            self._paper_meta_generation += 1
            my_gen = self._paper_meta_generation
            self._paper_meta_backfilling[notebook_id] = {
                "total": len(targets), "done": 0, "_gen": my_gen,
            }
        # entry 已登记,此刻发布快照,「论文信息补全中」项才会在已连接的铃铛里
        # 立刻出现——job 完成时的 notify_pending 只兜底刷新终态。必须在登记之后
        # 发:list_for_user 读的正是上面这个 dict,提前发只会推出一个不含本项的
        # 快照。
        self._publish_pending(nb_row)
        try:
            def _one(source_id: str) -> None:
                nonlocal done
                try:
                    row = self.sources.get_source(source_id)
                    status = self.ensure_paper_metadata(
                        row, force=force, client=paper_client
                    )
                except Exception:
                    status = "failed"
                    self.event_log.logger.exception(
                        "paper metadata backfill failed for %s", source_id
                    )
                with lock:
                    done += 1
                    counts[status] = counts.get(status, 0) + 1
                    current = done
                # 同步进度到 backfilling dict（供 pending-actions 读）；只
                # 改属于本次调用 gen 的 entry，避免"最新一次"覆盖后先来者
                # 的晚 worker 污染新一批的 done
                owns = False
                with self._paper_meta_backfilling_lock:
                    entry = self._paper_meta_backfilling.get(notebook_id)
                    if entry is not None and entry.get("_gen") == my_gen:
                        entry["done"] = current
                        owns = True
                # 推进度给铃铛。限频(默认 2s)——recompute 是 job 线程里的 DB
                # 计算,每源一发在大批量下就是查询风暴;无人连接时更是零开销。
                # 被后来者覆盖(owns=False)就别再推,那已经不是当前这批的进度了。
                if owns:
                    self._publish_pending(nb_row, throttled=True)
                if progress is not None:
                    progress(current, len(targets), source_id, status)

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="paper-meta"
            ) as pool:
                futures = [
                    pool.submit(contextvars.copy_context().run, _one, sid)
                    for sid in targets
                ]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
            self.event_log.emit(
                {"kind": "paper_meta", "notebook_id": notebook_id, "backfill": counts}
            )
            stored = int(counts.get("stored", 0))
            # 「判定为非论文」也是成功收尾：ensure_paper_metadata 落了标记行
            # (is_paper=0)、返回 "not_paper"，于是 stored 保持 0。只看 stored
            # 会让「整批都不是论文」这种完全成功的 job 不发 done——而前端刻意
            # 不自己弹完成 toast(交给铃铛)，用户就只能看着按钮悄悄复位。
            # 全 failed 仍然不报完成(stored 与 not_paper 皆为 0)。
            not_paper = int(counts.get("not_paper", 0))
            if stored > 0 or not_paper > 0:
                # 先 pop 本次 building entry 再通知，对照
                # scale_artifact_runtime.notify_index_done 的模板（状态先清
                # 后通知）；否则 _notify_paper_meta_done 里的 mark_dirty 会
                # 用仍含 building 项的旧快照重算，铃铛在 done toast 后瞬间又
                # 闪回"补全中"，直到端点侧 notify_pending=True 的 mark_dirty
                # 才补救。
                #
                # 只有「本世代仍持有 entry」才通知：若已被后来的 backfill 覆盖，
                # 说明同 notebook 还有一批在跑，此时报完成会让用户在新一批仍在
                # 进行时看到 done toast（多标签页/重复提交下还会重复发事件）。
                if self._pop_backfilling(notebook_id, my_gen):
                    self._notify_paper_meta_done(
                        notebook_id, nb_row, stored, not_paper=not_paper
                    )
            return counts
        finally:
            # 异常路径兜底清理。成功路径已在通知前提前 pop 过，这里对同一
            # gen 是幂等 no-op（entry 已不在，guard 直接跳过）。
            self._pop_backfilling(notebook_id, my_gen)

    def _pop_backfilling(self, notebook_id: str, my_gen: int) -> bool:
        """只 pop 属于 my_gen 世代的 entry；后来者已覆盖时（entry 的 _gen
        不匹配）不动手，与旧 finally 内联逻辑等价。成功路径与 finally 共用
        本方法：成功路径在通知前调用一次清空 building 状态，finally 收尾时
        对同一 gen 变成幂等 no-op；异常路径下只有 finally 调用，照常兜底。

        返回本次调用是否真的持有并弹出了 entry —— 调用方据此决定要不要发
        完成通知（被后来者覆盖的旧世代不该报完成，见调用处注释）。"""
        with self._paper_meta_backfilling_lock:
            entry = self._paper_meta_backfilling.get(notebook_id)
            if entry is not None and entry.get("_gen") == my_gen:
                self._paper_meta_backfilling.pop(notebook_id, None)
                return True
            return False

    @staticmethod
    def _pending_user_id(nb_row: Any) -> Optional[str]:
        """待办通知的归属 user:优先当前请求用户 ContextVar(background_jobs
        已 copy_context 传播),退化到 notebook 的 created_by。nb_row 复用
        backfill_paper_metadata 开头已取的行,免二次查库。"""
        from app.core.request_context import request_user_id

        return request_user_id() or nb_row["created_by"]

    def _publish_pending(self, nb_row: Any, *, throttled: bool = False) -> None:
        """把当前待办快照推给发起用户(job 线程内调用)。

        throttled=True 用于进度点(限频);起始/完成走不节流的路径。发布与
        fail-open 语义都在 pending_bus.publish_snapshot 里,与 KG 构建/索引
        构建两条路径共用同一入口。"""
        try:
            from app.services.pending_bus import publish_snapshot

            publish_snapshot(self._pending_user_id(nb_row), throttled=throttled)
        except Exception:  # noqa: BLE001 - notification is fail-open
            pass

    def _notify_paper_meta_done(
        self, notebook_id: str, nb_row: Any, stored: int, *, not_paper: int = 0
    ) -> None:
        """backfill 成功收尾的铃铛通知,完全对照
        scale_artifact_runtime.notify_index_done 的模板;fail-open——通知
        本身出错绝不能让已经跑完的 backfill 抛异常/丢 counts。

        事件同时带 stored 与 not_paper:前端据此区分「补全了 N 篇」与「N 篇
        判定为非论文」,后者 stored=0 但同样是成功完成(见调用处注释)。"""
        try:
            from app.services.pending_bus import pending_bus

            uid = self._pending_user_id(nb_row)
            if not uid:
                return
            pending_bus.emit(uid, {
                "event": "paper_meta_done",
                "notebook_id": notebook_id,
                "notebook_name": nb_row["name"],
                "stored": stored,
                "not_paper": not_paper,
            })
            pending_bus.mark_dirty(uid)
        except Exception:  # noqa: BLE001 - notification is fail-open
            try:
                self.event_log.logger.exception(
                    "paper_meta_done notify failed for %s", notebook_id
                )
            except Exception:
                pass

    def paper_meta_backfilling(self, notebook_id: str) -> bool:
        """O(1) 内存 membership；重启后天然为 False（未在跑）。"""
        return notebook_id in self._paper_meta_backfilling

    def paper_meta_backfill_progress(self, notebook_id: str) -> Optional[dict]:
        """返回 {"total","done"} 的浅拷贝或 None（未在跑）。锁内取快照，
        并剥掉下划线内部字段（如 _gen 世代 token）不外泄给消费者。"""
        with self._paper_meta_backfilling_lock:
            prog = self._paper_meta_backfilling.get(notebook_id)
            if not prog:
                return None
            return {k: v for k, v in prog.items() if not k.startswith("_")}
