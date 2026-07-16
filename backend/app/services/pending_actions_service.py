from __future__ import annotations

from typing import Any


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

    def __init__(self, projections, *, scale_runtime, source_ingestion=None) -> None:
        self.projections = projections
        self.scale_runtime = scale_runtime
        self.source_ingestion = source_ingestion

    def list_for_user(self, user_id: str) -> dict:
        projection = self.projections.pending_actions_projection_rows(user_id)
        items = projection["items"]
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
                        "state": "building" if state == "queued" else state,
                        "notebook_id": notebook_id,
                        "notebook_name": projection["notebook_names"].get(notebook_id, ""),
                    }
                    total = status.get("total_chunks") or 0
                    delta = status.get("delta_chunks") or 0
                    if state in ("building", "queued") and total:
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

        count = sum(
            1
            for item in items
            if item["type"] in ("report_outline", "governance")
            or (
                item["type"] == "index"
                and item["state"] in ("stale", "suggested")
            )
            # paper_meta building 不计入 count(跟 index building 一致——只显示，不响铃)
        )
        return {"count": count, "items": items}
