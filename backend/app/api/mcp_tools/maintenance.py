"""Knowledge-graph / retrieval-index build MCP tool."""

from functools import partial
from typing import Any, Callable

import anyio
from mcp.server.fastmcp import Context, FastMCP

from app.services import background_jobs
from app.api.kg_routes import _kg_maintenance_busy
from app.repositories.ports import (
    KgBuildAlreadyRunning,
    KgMaintenanceAlreadyRunning,
)

from ._shared import (
    AgentToolError,
    _budget_response,
    _owner_request_context,
    _run_with_progress,
    _writable_notebook,
)


def _start_kg_build(repo: Any, notebook_id: str) -> dict[str, Any]:
    """``build(target="kg")``'s body: the precondition ORDER of
    ``kg_routes.build_kg`` -- the deployment-level "no chat model configured"
    refusal before the per-notebook single-flight job row is even touched."""
    # Deliberately does not name the deployment's env vars -- server
    # configuration is not an Agent's business.
    if not repo._runtime.models.configured("kg_extract"):
        raise AgentToolError(
            "unavailable",
            "这个部署没有为知识图谱分析配置对话模型，请联系管理员配置后重试",
        )
    try:
        job = repo.prepare_notebook_kg_job(
            notebook_id, "incremental", retry_partial=True
        )
    except KgBuildAlreadyRunning:
        # 409 语义是单飞,不是错误——路由同款中文句子,轮询 get_notebook 即可。
        raise AgentToolError(
            "busy", "当前笔记本已有知识图谱分析任务正在运行"
        ) from None
    except KgMaintenanceAlreadyRunning as exc:
        # 批 3·W2 §2.1:被维护动作闸住同样是单飞语义——复用路由侧按 holder
        # 点名的同款句子。
        raise AgentToolError("busy", _kg_maintenance_busy(exc).detail) from None
    # submit() 的参数形状逐字照抄 kg_routes.build_kg,包括提交失败时回滚成
    # failed(否则该行会永久卡在 running,拖死后续每次构建的单飞闸)。
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
        "target": "kg",
        "job_id": job["id"],
        "mode": "incremental",
        "status": "building",
    }


def register_maintenance_tools(
    server: FastMCP, repository_provider: Callable[[], Any]
) -> None:
    @server.tool(
        description=(
            "Start a background build for a notebook (notebook_id omitted = the "
            "token's default notebook) and return immediately; poll get_notebook "
            "for progress. target=\"kg\": an incremental knowledge-graph "
            "extraction (sources that already have knowledge objects are "
            "skipped; a previously partial source is retried) -- a "
            "model-call-heavy job whose LLM cost is proportional to the "
            "unextracted content; `when` does not apply. Refuses with busy "
            "(do not retry immediately) while a build is already running. "
            "target=\"index\": a retrieval-index rebuild; when=\"now\" "
            "(default) starts it now, when=\"idle\" queues it for the "
            "deployment's next low-traffic window; refuses if the notebook is "
            "too small to need one. Requires the manage permission and "
            "ownership of the notebook."
        ),
        tier="manage",
    )
    async def build(
        ctx: Context, target: str = "kg", when: str = "", notebook_id: str = "",
    ) -> dict[str, Any]:
        if target not in ("kg", "index"):
            raise AgentToolError("invalid_argument", "target 只能是 kg 或 index")
        if target == "kg" and when:
            raise AgentToolError(
                "invalid_argument", "target=kg 不接受 when，知识图谱分析总是立即开始"
            )
        if target == "index" and when not in ("", "now", "idle"):
            raise AgentToolError("invalid_argument", "when 只能是 now 或 idle")
        repo = repository_provider()
        # ``fenced`` only for the knowledge graph: a retrieval index is a
        # TARGET-LOCAL derived artifact outside the cross-environment sync
        # closure (docs/incremental-sync-design.md section 6), so a mirrored
        # notebook must be able to rebuild it -- rebuilding is the target
        # end's only repair for a stale index. The owner gate is unchanged.
        # The browser twin says the same through its own capability cell
        # ``scale_index:write``; the KG build keeps the fence because it
        # writes knowledge rows, which an import does replace.
        principal, notebook_id = await anyio.to_thread.run_sync(
            partial(
                _writable_notebook, repo, notebook_id, "maintenance:execute",
                fenced=target == "kg",
            )
        )

        def run() -> dict[str, Any]:
            with _owner_request_context(principal):
                if target == "kg":
                    return _start_kg_build(repo, notebook_id)
                # mode is fixed to "auto" (fold if a fresh index already
                # exists, else full) and never exposed as a tool argument --
                # the browser's own rebuild control defaults to it too.
                # The one ValueError the service raises here is the
                # eligibility rule (the HTTP twin answers 409): a notebook too
                # small to need an index is a refusal by rule -- retrying will
                # not change it -- so it is ``forbidden``, not ``busy``.
                try:
                    result = repo.trigger_scale_index_rebuild(
                        notebook_id, when=when or "now", mode="auto"
                    )
                except ValueError:
                    raise AgentToolError(
                        "forbidden",
                        "此操作不被允许：这个笔记本规模太小且不是基础库，不需要检索索引"
                        "（小笔记本不建索引也能正常检索）",
                    ) from None
                return {"target": "index", **dict(result)}

        return _budget_response(
            await _run_with_progress(ctx, run, label="build")
        )
