from __future__ import annotations

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
from app.services.kg_mutation import KgMutationCoordinator
from app.services.knowledge_lifecycle import KnowledgeLifecycleService
from app.services.mineru_cloud_client import MinerUCloudNotConfigured
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
        self, source_id: str, url: str, file_name: str
    ) -> List[SourceElement]:
        """下载 URL 到临时文件，走本地 MinerU(http/cli)/pypdf 解析（数据不出网）。

        复用 parse_source_file 的「本地 MinerU 失败→pypdf 兜底」路径，与文件上传一致；
        全程不触达 mineru.net 云端。解析后无论成败都清理临时文件。
        """
        fd, tmp = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        tmp_path = Path(tmp)
        try:
            remote_sources.download_pdf(url, tmp_path)
            return self.parse_file(
                source_id, str(tmp_path), file_name or "source.pdf", self.mineru_client()
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
            # URL 来源：本地 MinerU 已配置则优先本地（下载到临时文件，数据不出网），
            # 否则走 mineru.net 云端；本地文件来源走 MinerU(http/cli)/pypdf。
            # 本地优先时绝不静默回落云端——内网部署不能把内部 PDF 外发。
            if source.source_url:
                mineru_client = self.mineru_client()
                if mineru_client.configured:
                    elements = self.parse_url_via_local(
                        source_id, source.source_url, source.file_name
                    )
                    mineru_error = str(getattr(mineru_client, "last_error", "") or "")
                    parser_mode = f"mineru_local({mineru_client.mode})"
                else:
                    cloud_client = self.mineru_cloud_client()
                    content_list = cloud_client.parse_url(
                        source.source_url, data_id=source_id
                    )
                    elements = mineru_content_list_to_elements(source_id, content_list)
                    mineru_error = str(getattr(cloud_client, "last_error", "") or "")
                    parser_mode = "mineru_cloud"
            else:
                mineru_client = self.mineru_client()
                elements = self.parse_file(
                    source_id, source.file_path, source.file_name, mineru_client
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
            summary = self.summarize_source(source.title, elements)
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
            self.set_source_status(source_id, "parsed", summary=summary)

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
        self.kg_mutations.invalidate_unified_cache(source.notebook_id)
        hooks.mark_unified_dirty(source.notebook_id)

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
