from __future__ import annotations

import concurrent.futures
import contextvars
import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterable, List, Optional

from app.core.config import Settings
from app.core.event_logging import EventLogger
from app.core.llm import cap_kwargs
from app.models.schemas import (
    AddUrlSourcesResult,
    RejectedUrl,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
)
from app.repositories.ports import SourceScheduler, UploadedSourceFile
from app.repositories.source_files import SourceFileStore, safe_filename
from app.repositories.sqlite.notebook_store import NotebookStore
from app.repositories.sqlite.source_store import SourceElementWrite, SourceStore
from app.services import kg_ingest, remote_sources
from app.services.extraction_profiles import PROFILES, get_profile
from app.services.kg.client import safe_json
from app.services.kg_mutation import KgMutationCoordinator
from app.services.knowledge_lifecycle import KnowledgeLifecycleService
from app.services.mineru_cloud_client import MinerUCloudNotConfigured
from app.services.paper_meta import (
    PAPER_META_SCHEMA_HINT,
    paper_meta_prompt,
    verify_paper_meta,
)
from app.services.parsers import mineru_content_list_to_elements
from app.services.prompts import NOTEBOOK_META_SCHEMA_HINT, notebook_meta_prompt
from app.services.source_chunking import SourceChunkingService
from app.services.source_embedding import SourceEmbeddingService


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
        notebooks: NotebookStore,
        sources: SourceStore,
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
        parse_file: Callable[..., List[SourceElement]],
        make_persist_image: Callable[[str, str, str], Any],
        delete_source_images: Callable[[str], None],
        mineru_client: Callable[[], Any],
        mineru_cloud_client: Callable[[], Any],
        llm: Callable[[], Any],
        kg_llm: Callable[[], Any],
        normalize_doc_type: Callable[[str], str],
        default_notebook_names: Iterable[str],
        # --- TEMPORARY KG/catalog callbacks (Task 13/15 targets) ----------
        clear_source_extraction_state: Callable[..., None],
        begin_extraction_run: Callable[[str, str, str, str], None],
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
        self.parse_file = parse_file
        self.make_persist_image = make_persist_image
        self.delete_source_images = delete_source_images
        self.mineru_client = mineru_client
        self.mineru_cloud_client = mineru_cloud_client
        self.llm = llm
        self.kg_llm = kg_llm
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
        # 论文元数据 backfill 进程内状态镜像 kg_building（重启即清）
        # nb_id → {"total": N, "done": k, "_gen": G}
        self._paper_meta_backfilling: dict[str, dict] = {}
        self._paper_meta_backfilling_lock = threading.Lock()
        # 同一 nb 并发 backfill 的世代守卫：后来者覆盖先来者的 entry 后，
        # 先来者的 finally 不能把后来者的 entry 弹掉；_one 里的 done 更新也
        # 只能改属于自己 gen 的 entry，避免"最新一次"语义下的 done 串扰。
        self._paper_meta_generation = 0

    def pipeline_hooks(self) -> SourcePipelineHooks:
        return SourcePipelineHooks(
            should_extract_kg=self.should_extract_kg,
            extract_source=self.run_extraction,
            mark_unified_dirty=self.kg_mutations.mark_unified_kg_dirty,
            augment_notebook_metadata=self.augment_notebook_metadata,
            maybe_enqueue_scale_fold=self.maybe_enqueue_scale_fold,
        )

    def import_sources_compat(
        self, notebook_id: str, payload: SourceImportRequest
    ) -> List[SourceSummary]:
        return self.import_sources(notebook_id, payload, self.pipeline_hooks())

    def add_url_sources_compat(
        self, notebook_id: str, urls: Iterable[str], scheduler=None
    ) -> AddUrlSourcesResult:
        return self.add_url_sources(
            notebook_id, urls, scheduler, self.pipeline_hooks()
        )

    def upload_sources_compat(
        self, notebook_id: str, files: Iterable[UploadedSourceFile], scheduler=None
    ) -> List[SourceSummary]:
        return self.upload_sources(
            notebook_id, files, scheduler, self.pipeline_hooks()
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
        if lower_name.endswith(".docx"):
            return "docx"
        if lower_name.endswith(".pptx"):
            return "pptx"
        return "other"

    def summarize(self, title: str, elements: List[SourceElement]) -> str:
        text = "\n".join(element.text for element in elements[:12])
        llm = self.llm()
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
    ) -> AddUrlSourcesResult:
        """逐 URL 初筛(非 PDF/不可达/超限→rejected,不建来源);通过的建 source_url
        来源并交由现有 process_source(有 scheduler 则后台,否则同步)。未配置 token→报错。"""
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
        for raw in urls:
            url = (raw or "").strip()
            if not url:
                continue
            probe = remote_sources.probe_pdf(url)
            if not probe.ok:
                rejected.append(RejectedUrl(url=url, reason=probe.reason))
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
            )
            if scheduler is not None:
                scheduler(source_id)
            else:
                self.process_source(source_id, hooks)
            created.append(self.sources.get_source(source_id))
        return AddUrlSourcesResult(created=created, rejected=rejected)

    def upload_sources(
        self,
        notebook_id: str,
        files: Iterable[UploadedSourceFile],
        scheduler: "SourceScheduler | None",
        hooks: SourcePipelineHooks,
    ) -> List[SourceSummary]:
        """Register uploaded files and kick off processing.

        With a ``scheduler`` (e.g. ``BackgroundTasks.add_task``) the heavy
        parse/embed/extract pipeline runs out of band and each source is
        returned in the ``queued`` state. Without one (tests, scripts) the
        pipeline runs synchronously before returning.
        """
        self.notebooks.get_row(notebook_id)  # KeyError if missing
        imported: List[SourceSummary] = []
        for file in files:
            source_id = self.new_id("src")
            file_name = safe_filename(file.file_name)
            digest = hashlib.sha256(file.content).hexdigest()
            stored_path = self.source_files.write_upload(
                notebook_id, source_id, file_name, file.content
            )
            self.sources.insert_source(
                source_id=source_id,
                notebook_id=notebook_id,
                title=file_name,
                source_type=self.source_type_from_name(file_name),
                status="queued",
                parse_status="queued",
                file_name=file_name,
                file_path=str(stored_path),
                file_size=len(file.content),
                file_hash=digest,
                summary="Uploaded; parsing is queued.",
                doc_type=self.normalize_doc_type(file.doc_type),
            )
            if scheduler is not None:
                scheduler(source_id)
            else:
                self.process_source(source_id, hooks)
            imported.append(self.sources.get_source(source_id))
        return imported

    # ------------------------------------------------------------ pipeline
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
        self.event_log.emit(
            {
                "kind": "status",
                "source_id": source_id,
                "status": status,
                "error": error_message or "",
            }
        )

    def should_extract_kg(self, notebook_id: str) -> bool:
        """摄取期是否抽 KG:全局开关开,或该 notebook 已有 KG(续抽保持完整)。"""
        return self.settings.kg_auto_extract or self.notebook_has_kg(notebook_id)

    def parse_url_via_local(
        self, source_id: str, url: str, file_name: str, persist_image: Any = None
    ) -> List[SourceElement]:
        """下载 URL 到临时文件，走本地 MinerU(http/cli)/pypdf 解析（数据不出网）。

        复用 parse_source_file 的「本地 MinerU 失败→pypdf 兜底」路径，与文件上传一致；
        全程不触达 mineru.net 云端。解析后无论成败都清理临时文件。`persist_image`
        (Task 8) 与文件上传路径同款透传给 parse_source_file/parse_pdf。
        """
        fd, tmp = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        tmp_path = Path(tmp)
        try:
            remote_sources.download_pdf(url, tmp_path)
            return self.parse_file(
                source_id, str(tmp_path), file_name or "source.pdf", self.mineru_client(),
                persist_image=persist_image,
            )
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

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

        self.set_source_status(source_id, "parsing")
        try:
            t = time.perf_counter()
            stage("parse", "start", t)
            # Task 9: 清掉这个源之前遗留的 MinerU 图片资产(行+盘)——必须在下面
            # 任何新图片持久化之前执行,否则会把刚写的新图也一并删掉。首次解析时
            # 这是无害的空操作(还没有图片);重新解析时清出一个干净的起点,避免
            # 每次重解析都在 notebook_assets 里累积孤儿行/文件。
            self.delete_source_images(source_id)
            # Task 8: per-source 图片持久化闭包(张数/尺寸/mime 护栏由工厂内置，
            # mineru_return_images 关时为 None——三条解析路径都能安全接受 None)。
            persist_image = self.make_persist_image(
                source.notebook_id, source_id, getattr(source, "created_by", "") or ""
            )
            # URL 来源：本地 MinerU 已配置则优先本地（下载到临时文件，数据不出网），
            # 否则走 mineru.net 云端；本地文件来源走 MinerU(http/cli)/pypdf。
            # 本地优先时绝不静默回落云端——内网部署不能把内部 PDF 外发。
            if source.source_url:
                mineru_client = self.mineru_client()
                if mineru_client.configured:
                    elements = self.parse_url_via_local(
                        source_id, source.source_url, source.file_name, persist_image
                    )
                    mineru_error = str(getattr(mineru_client, "last_error", "") or "")
                    parser_mode = f"mineru_local({mineru_client.mode})"
                else:
                    cloud_client = self.mineru_cloud_client()
                    content_list, images = cloud_client.parse_url_with_images(
                        source.source_url, data_id=source_id
                    )
                    elements = mineru_content_list_to_elements(
                        source_id, content_list, images=images, persist_image=persist_image
                    )
                    mineru_error = str(getattr(cloud_client, "last_error", "") or "")
                    parser_mode = "mineru_cloud"
            else:
                mineru_client = self.mineru_client()
                cloud_client = self.mineru_cloud_client()
                if not mineru_client.configured and cloud_client.configured:
                    # 本地 http/cli 未配置 + 云端已配 → 上传文件走云端(对称 URL 分支)；
                    # 云端任一步失败 → 回落 pypdf，摄取不中断。
                    try:
                        content_list, images = cloud_client.parse_file_with_images(
                            source.file_path, data_id=source_id
                        )
                        elements = mineru_content_list_to_elements(
                            source_id, content_list, images=images, persist_image=persist_image
                        )
                        mineru_error = str(getattr(cloud_client, "last_error", "") or "")
                        parser_mode = "mineru_cloud"
                    except Exception as exc:
                        mineru_error = str(getattr(cloud_client, "last_error", "") or exc)
                        elements = self.parse_file(
                            source_id, source.file_path, source.file_name, mineru_client,
                            persist_image=persist_image,
                        )
                        parser_mode = "pypdf_fallback_after_cloud_error"
                else:
                    # 本地已配(http/cli) 或 两者都没配 → 现状：本地 MinerU / pypdf。
                    elements = self.parse_file(
                        source_id, source.file_path, source.file_name, mineru_client,
                        persist_image=persist_image,
                    )
                    mineru_error = str(getattr(mineru_client, "last_error", "") or "")
                    parser_mode = str(getattr(mineru_client, "mode", ""))
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
            # 摘要(best-effort LLM)挪到 elements 落地之后:放在写库前会让 LLM 超时/失败/
            # hang 把 elements 一起拖没——几万源集体丢 elements、KG 无从接地的根子。
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
                from app.repositories.sqlite import knowledge_counts_cache
                knowledge_counts_cache.invalidate(notebook_id)

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

            embed_ctx = contextvars.copy_context()
            embed_thread = threading.Thread(
                target=lambda: embed_ctx.run(_embed_bg),
                name=f"embed-{source_id}", daemon=True
            )
            embed_thread.start()

            if hooks.should_extract_kg(notebook_id):
                self.set_source_status(source_id, "extracting")
                t = time.perf_counter()
                stage("extract", "start", t)
                hooks.extract_source(source_id)
                stage("extract", "done", t)
                try:
                    hooks.mark_unified_dirty(source.notebook_id)
                except Exception:
                    self.event_log.logger.exception(
                        "unified-KG dirty mark failed for source %s", source_id
                    )
            # Surface "parsed to empty" (e.g. scanned/image PDF with no text layer)
            # instead of a silent success that looks like a real result.
            empty_hint = ""
            if not elements and source.file_name.lower().endswith(".pdf"):
                empty_hint = (
                    "No extractable text — likely a scanned/image PDF. "
                    "Enable MinerU (MINERU_MODE) or add OCR to parse it."
                )
            fallback_hint = ""
            if (
                source.file_name.lower().endswith(".pdf")
                and self.mineru_client().configured
                and elements
                and "mineru" not in element_parsers
            ):
                fallback_hint = (
                    "MinerU did not produce usable elements; fell back to pypdf text extraction. "
                    "Check MinerU settings/logs if layout, formula, or table fidelity is expected."
                )
                if mineru_error:
                    fallback_hint = f"{fallback_hint} Last MinerU error: {mineru_error[:500]}"
            # Auto-fill notebook name/description from sources (only while name is a
            # default placeholder / purpose is still auto). Persist BEFORE marking the
            # source 'extracted' so the frontend's extracted-triggered refetch shows
            # the fresh name/description live. Best-effort: never fail the pipeline.
            try:
                hooks.augment_notebook_metadata(source.notebook_id, source_id)
            except Exception:
                self.event_log.logger.exception(
                    "notebook meta augmentation failed for %s", source_id
                )
            self.set_source_status(
                source_id,
                "extracted",
                error_message=empty_hint or fallback_hint,
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
        # Content-add settle point: if this notebook already has a scale index,
        # enqueue an idle incremental fold so the new (post-watermark) source
        # becomes semantically searchable. Idle queue coalesces batch runs (many
        # process_source calls) into a single fold. Never builds a fresh index;
        # helper is fail-safe (never raises).
        hooks.maybe_enqueue_scale_fold(source.notebook_id)
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
        llm_client = self.llm()
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

    def run_extraction(self, source_id: str) -> None:
        source: SourceDetail = self.sources.get_source(source_id)
        elements = self.source_elements(source_id)
        # 历史源 catch-up:补论文元数据(幂等,有行即跳)。ensure_paper_metadata 的
        # try/except 包住幂等读之后的整个方法体(不止 LLM 调用),保证它绝不向外
        # 抛异常——失败真正不影响 KG 抽取(不会中断下面紧接着的抽取流程),而不只
        # 是注释里的一句希望。
        self.ensure_paper_metadata(source, elements=elements, force=False)
        now = self.now()
        run_id = self.new_id("run")
        doc_type_id = (
            self.normalize_doc_type(getattr(source, "doc_type", "") or "")
            or "academic_paper"
        )
        kg_doc_type = kg_ingest.DOC_TYPE_MAP.get(doc_type_id, "academic")
        # One write transaction: reset the source's prior KG artefacts and open
        # its extraction_runs row (temporary facade callback — Task 13/15 target).
        self.begin_extraction_run(source_id, source.notebook_id, run_id, now)
        # begin_extraction_run committed a DELETE of this source's prior KG objects
        # in its own transaction; the kg_mutation_seq bump only lands later in
        # store_kg (success path). Invalidate the count cache here so the no-llm
        # early-return and the exception path can't keep serving pre-delete counts.
        from app.repositories.sqlite import knowledge_counts_cache
        knowledge_counts_cache.invalidate(source.notebook_id)
        try:
            kg_llm_client = self.kg_llm()
            if not getattr(kg_llm_client, "configured", False):
                self.finish_extraction_run(run_id, "completed", "no-llm")
                return
            raw_text = self.source_files.read_source_text(
                getattr(source, "file_path", "") or "", elements
            )
            n_chars = kg_ingest.plan_window_size(
                len(raw_text), self.settings.kg_extract_workers,
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
            n_obj, n_rel = self.knowledge_lifecycle.store_kg(
                source.notebook_id, source.id, objects, relations
            )
            try:
                self.knowledge_lifecycle.incremental_fuse_source(
                    source.notebook_id, source.id
                )
            except Exception:
                self.event_log.logger.exception(
                    "incremental_fuse_source failed for %s", source_id
                )
            try:
                self.maybe_auto_index(source.notebook_id)
            except Exception:
                self.event_log.logger.exception(
                    "maybe_auto_index failed for %s", source.notebook_id
                )
            fw, tw = graph.failed_windows, graph.total_windows
            self.finish_extraction_run(
                run_id,
                "completed",
                f"kg objects={n_obj} relations={n_rel} doc_type={kg_doc_type} "
                f"windows_failed={fw}/{tw} windows_skipped={graph.windows_skipped} "
                f"concepts_dropped={graph.concepts_dropped} claims_dropped={graph.claims_dropped}",
            )
        except Exception as exc:
            self.finish_extraction_run(run_id, "failed", str(exc))
            raise

    # ---------------------------------------------------- paper metadata
    def ensure_paper_metadata(
        self,
        source: "SourceSummary | SourceDetail",
        elements: Optional[List[SourceElement]] = None,
        force: bool = False,
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
        doc_type = (
            self.normalize_doc_type(getattr(source, "doc_type", "") or "")
            or "academic_paper"
        )
        if doc_type != "academic_paper":
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
            client = self.kg_llm()
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
        (≤8,受 kg_extract_workers 约束),任务级 copy_context 传播 per-user
        模型配置。返回 {"total": N, "<status>": n, ...} 计数。成功收尾
        (stored>0)经 pending_bus 广播 paper_meta_done 铃铛事件,见
        _notify_paper_meta_done。"""
        nb_row = self.notebooks.get_row(notebook_id)  # KeyError if missing
        targets = self.sources.sources_missing_paper_meta(
            notebook_id, include_existing=force
        )
        counts: dict = {"total": len(targets)}
        if not targets:
            return counts
        workers = max(1, min(8, int(getattr(self.settings, "kg_extract_workers", 4))))
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
        try:
            def _one(source_id: str) -> None:
                nonlocal done
                try:
                    row = self.sources.get_source(source_id)
                    status = self.ensure_paper_metadata(row, force=force)
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
                with self._paper_meta_backfilling_lock:
                    entry = self._paper_meta_backfilling.get(notebook_id)
                    if entry is not None and entry.get("_gen") == my_gen:
                        entry["done"] = current
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
            if stored > 0:
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
                    self._notify_paper_meta_done(notebook_id, nb_row, stored)
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

    def _notify_paper_meta_done(
        self, notebook_id: str, nb_row: Any, stored: int
    ) -> None:
        """backfill 成功收尾的铃铛通知,完全对照
        scale_artifact_runtime.notify_index_done 的模板:owner 优先取当前
        请求用户 ContextVar,退化到 notebook 的 created_by(nb_row 复用
        backfill_paper_metadata 开头已取的行,免二次查库);fail-open——通知
        本身出错绝不能让已经跑完的 backfill 抛异常/丢 counts。"""
        try:
            from app.core.request_context import request_user_id
            from app.services.pending_bus import pending_bus

            uid = request_user_id() or nb_row["created_by"]
            if not uid:
                return
            pending_bus.emit(uid, {
                "event": "paper_meta_done",
                "notebook_id": notebook_id,
                "notebook_name": nb_row["name"],
                "stored": stored,
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
