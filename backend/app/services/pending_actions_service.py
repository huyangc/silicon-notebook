from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("silicon_notebook.pending_actions")


class PendingActionsService:
    """Read-only aggregation for the user action centre.

    ``source_ingestion`` starts out ``None``: this service is constructed in
    ``RepositoryRuntime.wire_query_services()``, which runs before
    ``wire_source_ingestion()`` builds the ``SourceIngestionService`` it
    needs to read ``_paper_meta_backfilling`` from — the runtime backfills
    the attribute once that component exists (mirrors the deferred
    ``catalog.source_ingestion`` assignment in ``wire_source_ingestion()``).
    Unlike that catalog wiring this is a plain strong reference, not a
    ``weakref``: nothing outside the repository (no other service, no
    ``pending_bus`` handler — it re-resolves ``repository()`` fresh on every
    call rather than retaining a specific facade's components) holds onto a
    ``PendingActionsService`` instance longer than its owning facade, so
    there is no transitive-retention path for a strong ref to keep alive
    (contrast ``ScaleArtifactRuntime``, which callers legitimately hold past
    a single request — see test_retained_scale_runtime_does_not_transitively_retain_repository).
    """

    def __init__(
        self, projections, *, scale_runtime, source_ingestion=None, now=None, transfers=None
    ) -> None:
        self.projections = projections
        self.scale_runtime = scale_runtime
        self.source_ingestion = source_ingestion
        # The repository clock seam; the deletion-window notices compare
        # ``delete_after`` against it (``app.domain.notebook_delete_grace``).
        self.now = now
        # The web notebook export/import jobs (``NotebookTransferJobs``): their
        # entries come from the registry in memory, not from a projection.
        self.transfers = transfers

    def _bell_transfer_items(self, user_id: str) -> list[dict[str, Any]]:
        """The user's export/import jobs for the bell; a failure drops only this
        kind (logged by class), as ``_bell_deletion_notices`` does."""
        if self.transfers is None:
            return []
        try:
            return self.transfers.pending_items(user_id)
        except Exception as exc:  # noqa: BLE001 - one kind must not hide the others
            _log.warning("pending actions: reading the transfer jobs failed (%s)", type(exc).__name__)
            return []

    def deletion_notices(self, user_id: str) -> list[dict[str, Any]]:
        """The notebooks inside a deletion window that concern ``user_id``
        (owner, or a reader holding Memory there), oldest deadline first --
        the payload of ``GET /api/me/notebook-deletions`` and of the bell's
        ``notebook_pending_delete`` items."""
        if self.now is None:
            return []
        return self.projections.pending_delete_notices(user_id, now=self.now())

    def deletion_notice(self, user_id: str, notebook_id: str) -> dict[str, Any] | None:
        """``notebook_id``'s notice for any reader of it (not only the notice
        audience): the explanation code of ``GET /notebooks/{id}``'s 404.
        ``None`` when it is not inside an open window or the caller cannot
        read it."""
        if self.now is None:
            return None
        rows = self.projections.pending_delete_notices(
            user_id, now=self.now(), notebook_id=notebook_id
        )
        return rows[0] if rows else None

    def _bell_deletion_notices(self, user_id: str) -> list[dict[str, Any]]:
        """The bell's deletion-window entries. A failing read drops only this
        kind (logged for diagnosis), never the whole bell: the notebook list's
        block reads the same notices from its own endpoint and says there
        when they cannot be read (fail-closed), so nothing is silently lost."""
        try:
            return self.deletion_notices(user_id)
        except Exception:  # noqa: BLE001 - one kind must not hide the others
            _log.warning(
                "pending actions: reading the deletion-window notices failed",
                exc_info=True,
            )
            return []

    def list_for_user(self, user_id: str) -> dict:
        projection = self.projections.pending_actions_projection_rows(user_id)
        # 删除窗口里的库排在最前:它们有截止时刻,而且计入未读(见 count)。
        items = [
            {
                "type": "notebook_pending_delete",
                "notebook_id": notice["notebook_id"],
                "notebook_name": notice["name"],
                "delete_after": notice["delete_after"],
                "is_owner": notice["is_owner"],
                "memory_count": notice["memory_count"],
                "transferable_count": notice["transferable_count"],
            }
            for notice in self._bell_deletion_notices(user_id)
        ] + projection["items"]
        for notebook_id in projection["notebook_ids"]:
            try:
                status = self.scale_runtime.status(notebook_id)
            except Exception:  # noqa: BLE001 - one index must not hide other actions
                status = None
            if status is not None:
                state = status.get("state")
                if state in ("stale", "suggested", "building", "queued"):
                    item: dict[str, Any] = {
                        "type": "index",
                        "state": state,
                        "notebook_id": notebook_id,
                        "notebook_name": projection["notebook_names"].get(notebook_id, ""),
                    }
                    total = status.get("total_chunks") or 0
                    delta = status.get("delta_chunks") or 0
                    # queued 不是构建进度——(total-delta)/total 会误导排队中的库
                    # 显示出一个虚假的完成百分比。
                    if state == "building" and total:
                        item["progress"] = round(100.0 * max(0, total - delta) / total)
                    items.append(item)

            if self.source_ingestion is None:
                continue
            progress = self.source_ingestion.paper_meta_backfill_progress(notebook_id)
            if progress is not None:
                items.append(
                    {
                        "type": "paper_meta",
                        "state": "building",
                        "notebook_id": notebook_id,
                        "notebook_name": projection["notebook_names"].get(notebook_id, ""),
                        "progress": progress,
                    }
                )

        # 网页导出/导入作业:进行中的只呈现不计数(与 ask / index building 同理——用户
        # 自己刚发起的);导出完成待下载、作业失败计入 count(等用户处理)。
        items.extend(self._bell_transfer_items(user_id))
        count = sum(
            1
            for item in items
            if item["type"]
            in ("report_outline", "governance", "share_request", "notebook_pending_delete")
            or (
                item["type"] == "index"
                and item["state"] in ("stale", "suggested")
            )
            or (
                item["type"] == "notebook_transfer"
                and item["state"] in ("succeeded", "failed")
            )
            # type "ask"(进行中的提问)同样不计入 count:它是**状态展示**,不是
            # 「待你确认」的动作——用户几秒前刚发起它,让它响铃就是每问一个问题
            # 都响一次。呈现照旧(条目在 items 里),只是不进这个数。
            # paper_meta building 不计入 count(跟 index building 一致——只显示，不响铃)
        )
        return {"count": count, "items": items}
