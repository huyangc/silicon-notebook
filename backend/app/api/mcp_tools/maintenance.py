"""Fixed built-in MCP tool registrations for this capability bundle."""

from ._shared import *  # noqa: F403 - internal frozen helper surface


def register_maintenance_tools(
    server: FastMCP, repository_provider: Callable[[], Any]
) -> None:
    @server.tool(
        description=(
            "Trigger an incremental knowledge-graph extraction build for the "
            "selected notebook (sources that already have knowledge objects "
            "are skipped; any previously partial source is retried). This is "
            "a model-call-heavy analysis job -- expect it to run for a while "
            "and to consume LLM budget proportional to the notebook's "
            "unextracted content. It runs in the background; this call "
            "returns immediately with a job id, and get_build_status is how "
            "you check on it. Refuses (do not retry immediately) while a "
            "build is already running for this notebook -- poll "
            "get_build_status until it clears instead. Requires the "
            "maintenance:execute scope and ownership of the notebook."
        )
    )
    async def build_kg(ctx: Context) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _writable_notebook, ctx, repo, "maintenance:execute"
        )

        def run() -> dict[str, Any]:
            with _owner_request_context(principal):
                # Same precondition ORDER as kg_routes.build_kg: the
                # deployment-level "no chat model configured" refusal happens
                # before the per-notebook single-flight job row is even
                # touched. English, and deliberately does not name the
                # deployment's env vars -- server configuration is not an
                # Agent's business (mirrors add_source_url's
                # MinerUCloudNotConfigured wording above).
                if not repo._runtime.models.configured("kg_extract"):
                    raise ValueError(
                        "this deployment has no chat model configured for "
                        "knowledge-graph extraction; ask the operator to "
                        "configure one"
                    )
                try:
                    job = repo.prepare_notebook_kg_job(
                        notebook_id, "incremental", retry_partial=True
                    )
                except KgBuildAlreadyRunning:
                    # 409 语义是单飞,不是错误——路由同款中文句子,轮询
                    # get_build_status 即可,不必改写成英文重新措辞一遍。
                    raise ValueError("当前笔记本已有知识图谱分析任务正在运行")
                # submit() 的参数形状逐字照抄 kg_routes.build_kg,包括提交失败时
                # 回滚成 failed(否则该行会永久卡在 running,拖死后续每次构建的
                # 单飞闸)。
                try:
                    background_jobs.submit(
                        repo.execute_notebook_kg_job,
                        notebook_id,
                        job["id"],
                        "incremental",
                        retry_partial=True,
                        name=f"buildkg-{notebook_id}",
                        notify_pending=True,
                    )
                except Exception:
                    repo.fail_notebook_kg_job_submission(job["id"])
                    raise
                return {
                    "job_id": job["id"],
                    "mode": "incremental",
                    "status": "building",
                }

        return _budget_response(
            await _run_with_progress(ctx, run, label="build_kg")
        )

    @server.tool(
        description=(
            "Trigger a retrieval-index rebuild for the selected notebook. "
            "when='now' (the default) starts it in the background "
            "immediately; when='idle' queues it for this deployment's next "
            "low-traffic window instead of running now. Refuses if the "
            "notebook is too small to need a retrieval index (small "
            "notebooks are served without one). Requires the "
            "maintenance:execute scope and ownership of the notebook."
        )
    )
    async def build_retrieval_index(
        ctx: Context, when: str = "now"
    ) -> dict[str, Any]:
        if when not in ("now", "idle"):
            raise ValueError("when must be one of: now, idle")
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _writable_notebook, ctx, repo, "maintenance:execute"
        )

        def run() -> dict[str, Any]:
            with _owner_request_context(principal):
                # mode is fixed to "auto" (fold if a fresh index already
                # exists, else full) and never exposed as a tool argument --
                # the browser's own rebuild control defaults to it too, and
                # fold-vs-full is an implementation detail an Agent has no
                # basis to pick between. Eligibility failures come back as
                # ValueError from the service layer with an already-readable
                # message ("notebook too small and not base-tier...") and are
                # let through as-is, exactly like add_source_url lets
                # add_url_sources' own rejection wording through.
                return repo.trigger_scale_index_rebuild(
                    notebook_id, when=when, mode="auto"
                )

        return _budget_response(
            await _run_with_progress(ctx, run, label="build_retrieval_index")
        )

    @server.tool(
        description=(
            "Read the selected notebook's combined build status: "
            "knowledge-graph extraction (ready/building, pending source "
            "count, and the current or most recent build job's stage and "
            "progress) plus retrieval-index state (exists/building/queued, "
            "including queue position and the next low-traffic window when "
            "queued). The one read behind both build_kg and "
            "build_retrieval_index -- poll this after triggering either. "
            "Read-only: any member of the notebook may call it, not only "
            "the owner."
        )
    )
    async def get_build_status(ctx: Context) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, ctx, repo, "knowledge:read"
        )

        def load() -> dict[str, Any]:
            with _owner_request_context(principal):
                # `index_status` is a pure aggregation of already-user-facing
                # fields (stable status/stage enums, counters, timestamps) --
                # unlike a source's `error_message`, its KgBuildJobStatus job
                # sub-object only ever carries a derived `user_message`, never
                # a raw exception. Nothing to strip before handing it back.
                return repo.index_status(notebook_id)

        return _budget_response(
            await _run_with_progress(ctx, load, label="get_build_status"),
            field_limits={
                "status": 40, "stage": 40, "mode": 40, "error_code": 100,
                "user_message": 500, "state": 40,
            },
        )

    # --- Agentic Memory P3 (T3): notebook understanding + observation log --
    # Registered right after get_build_status, the last of the pre-existing
    # 20 tools. Both reuse `_selected_notebook` exactly like every read-only
    # tool above -- neither goes through `_writable_notebook`'s owner-only
    # gate; see that function's own docstring (updated by this feature) for
    # why `add_observation` specifically does not.


