from __future__ import annotations

from typing import Any


class PendingActionsService:
    """Read-only aggregation for the user action centre."""

    def __init__(self, projections, *, scale_runtime) -> None:
        self.projections = projections
        self.scale_runtime = scale_runtime

    def list_for_user(self, user_id: str) -> dict:
        projection = self.projections.pending_actions_projection_rows(user_id)
        items = projection["items"]
        for notebook_id in projection["notebook_ids"]:
            try:
                status = self.scale_runtime.status(notebook_id)
            except Exception:  # noqa: BLE001 - one index must not hide other actions
                continue
            state = status.get("state")
            if state not in ("stale", "suggested", "building", "queued"):
                continue
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

        count = sum(
            1
            for item in items
            if item["type"] in ("report_outline", "governance")
            or (
                item["type"] == "index"
                and item["state"] in ("stale", "suggested")
            )
        )
        return {"count": count, "items": items}
