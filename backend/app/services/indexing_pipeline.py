"""Notebook selection, sanitized projection and write admission."""
from __future__ import annotations

from app.domain.indexing_pipeline import (
    BUILTIN_INDEXING_PIPELINE_VERSION,
    IndexingPipelineHostPort,
    IndexingPipelineLargeLibraryError,
    IndexingPipelineOption,
    IndexingPipelineRebuildActiveError,
    IndexingPipelineRebuildFailedError,
    IndexingPipelineUnavailableError,
)


_BUILTIN = IndexingPipelineOption(
    pipeline_id="",
    label="内建管线",
    description="使用 silicon-notebook 内建分块与知识抽取策略。",
    version=BUILTIN_INDEXING_PIPELINE_VERSION,
    overrides_chunking=False,
    overrides_kg_extraction=False,
    available=True,
)


class IndexingPipelineService:
    def __init__(self, notebooks, chunks, host: IndexingPipelineHostPort | None,
                 large_library=None):
        self.notebooks = notebooks
        self.chunks = chunks
        self.host = host
        # 批 3·W3(D3):大库判据探针(None=不设限——只覆盖直接构造的
        # 测试;runtime 装配总是接线)。判据=活跃对象数阈值
        # (count_active_objects 的 seq-gated memo;摄取期 seq 常变时会
        # 冷载,但两个消费入口都是用户手动低频动作)。
        self._large_library = large_library

    def _option(self, pipeline_id: str) -> IndexingPipelineOption | None:
        if not pipeline_id:
            return _BUILTIN
        return self.host.option(pipeline_id) if self.host is not None else None

    def require_write_admission(self, notebook_id: str) -> None:
        state = self.notebooks.indexing_pipeline_state(notebook_id)
        pipeline_id = str(state["pipeline_id"] or "")
        desired_version = str(
            state["pipeline_version"] or BUILTIN_INDEXING_PIPELINE_VERSION
        )
        pipeline_job_id = str(state.get("pipeline_job_id") or "")
        option = self._option(pipeline_id)
        if option is None or not option.available:
            raise IndexingPipelineUnavailableError(pipeline_id)
        if (
            pipeline_job_id
            or
            pipeline_id != str(state["published_pipeline_id"] or "")
            or desired_version != option.version
            or option.version
            != str(
                state["published_pipeline_version"]
                or BUILTIN_INDEXING_PIPELINE_VERSION
            )
        ):
            raise IndexingPipelineUnavailableError(pipeline_id)

    def projection(self, notebook_id: str) -> dict:
        state = self.notebooks.indexing_pipeline_state(notebook_id)
        selected_id = str(state["pipeline_id"] or "")
        pipeline_job_id = str(state.get("pipeline_job_id") or "")
        pipeline_job_status = str(state.get("pipeline_job_status") or "")
        failed = bool(pipeline_job_id) and pipeline_job_status == "failed"
        active = bool(pipeline_job_id) and pipeline_job_status in {
            "queued",
            "running",
        }
        desired_version = str(
            state["pipeline_version"] or BUILTIN_INDEXING_PIPELINE_VERSION
        )
        published_id = str(state["published_pipeline_id"] or "")
        published_version = str(
            state["published_pipeline_version"]
            or BUILTIN_INDEXING_PIPELINE_VERSION
        )
        selected = self._option(selected_id)
        missing = bool(selected_id and selected is None)
        selected_version = (
            selected.version if selected is not None else desired_version
        )
        pending = (
            bool(pipeline_job_id)
            or selected_id != published_id
            or desired_version != selected_version
            or selected_version != published_version
        )
        retryable = pending and not active
        options = [_BUILTIN]
        if self.host is not None:
            options.extend(self.host.options())
        if missing:
            options.append(
                IndexingPipelineOption(
                    pipeline_id=selected_id,
                    label="已停用的索引管线",
                    description="该部署插件当前未加载；旧索引仍可读取。",
                    version=published_version,
                    overrides_chunking=True,
                    overrides_kg_extraction=False,
                    available=False,
                )
            )
        return {
            "pipeline_id": selected_id or None,
            "version": selected_version,
            "available": bool(selected and selected.available),
            "missing": missing,
            "pending": pending,
            # 批 3·W3(D3):服务端真值——大库锁定切换,前端据此禁用控件而
            # 不是等 409。判据 memo 化,冷一次热毫秒。
            "large_library_locked": bool(
                self._large_library is not None
                and self._large_library(notebook_id)
            ),
            "rebuild_status": (
                "pending" if active else "failed" if failed or retryable else "idle"
            ),
            "job_id": (
                pipeline_job_id
                if pipeline_job_id and not pipeline_job_id.startswith("pending:")
                else None
            ),
            "options": [
                {
                    "pipeline_id": item.pipeline_id or None,
                    "label": item.label,
                    "description": item.description,
                    "version": item.version,
                    "overrides_chunking": item.overrides_chunking,
                    "overrides_kg_extraction": item.overrides_kg_extraction,
                    "available": item.available,
                    "selected": item.pipeline_id == selected_id,
                }
                for item in options
            ],
        }

    def begin(self, notebook_id: str, pipeline_id: str | None) -> dict:
        """Persist desired intent and mint the sole authority for its worker."""
        desired = str(pipeline_id or "").strip()
        option = self._option(desired)
        if option is None or not option.available:
            raise IndexingPipelineUnavailableError(desired)
        before = self.projection(notebook_id)
        if (
            str(before["pipeline_id"] or "") == desired
            and before["version"] == option.version
            and not before["pending"]
        ):
            return {**before, "changed": False, "warning_count": 0}
        # 活跃 rebuild 期间拒绝在改 desired 之前：铸新 generation 会让正在跑的
        # worker 在花完整库模型/embedding 开销之后输掉 publish CAS(整轮作废),
        # 而提交者只读到一句无害的 409。失败态(retryable)不拦——那正是重试入口。
        if before["rebuild_status"] == "pending":
            raise IndexingPipelineRebuildActiveError(notebook_id)
        # 批 3·W3(D3):大库拒绝**非内建目标**的变更,在改 desired 之前——
        # 什么都没保存。放在 rebuild-active 之后:worker 在跑时给出更准的
        # 「重建进行中」文案而不是「规模较大」(内评 P2)。**切回内建
        # (desired == "")豁免**(内评双 P1):卡在缺席/失败自定义管线上的
        # 大库,require_write_admission 让全部写入 fail-closed,切回内建是
        # 唯一自助出口——闸死它=笔记本永久写锁死;恢复重建即便再失败也只
        # 回到 retryable,不会更糟。判定复用 before 里刚算过的锁定位
        # (拒绝与投影告诉前端的状态结构上必然一致,也省两次探针)。
        if desired and before.get("large_library_locked"):
            raise IndexingPipelineLargeLibraryError(notebook_id)
        generation = self.notebooks.set_indexing_pipeline_desired(
            notebook_id, desired, option.version
        )
        return {
            **self.projection(notebook_id),
            "changed": True,
            "warning_count": 0,
            "_pipeline_id": desired,
            "_pipeline_version": option.version,
            "_pipeline_generation": generation,
        }

    def rebuild(
        self,
        notebook_id: str,
        *,
        job_id: str,
        pipeline_id: str,
        pipeline_version: str,
        pipeline_generation: str,
    ) -> dict:
        """Run the authorized bounded chunk plan without publishing identity.

        The durable worker owns the later KG/finalize success tail.  Keeping
        publication out of this seam prevents a failed KG strategy from
        advertising the desired identity as complete.
        """
        try:
            outcome = self.chunks.rebuild_notebook_chunks(
                notebook_id,
                job_id=job_id,
                pipeline_id=pipeline_id,
                pipeline_version=pipeline_version,
                pipeline_generation=pipeline_generation,
            )
        except ValueError as exc:
            # Desired selection intentionally remains durable/pending. The
            # caller can retry or choose builtin; never silently republish old.
            raise IndexingPipelineRebuildFailedError() from exc
        return {
            **self.projection(notebook_id),
            "changed": True,
            "warning_count": int(outcome.get("warning_count", 0)),
        }


__all__ = ["IndexingPipelineService"]
