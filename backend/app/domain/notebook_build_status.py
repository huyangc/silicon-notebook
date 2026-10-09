"""The one projection of a notebook's knowledge-graph build state.

Shared by ``ScaleArtifactRuntime.index_status`` (the HTTP ``index-status``
read) and the MCP ``get_notebook`` tool, so the two surfaces cannot drift.
Pure: it reads only the notebook summary and the unified-KG status the caller
already holds.
"""
from __future__ import annotations

from typing import Any, Mapping


def kg_build_view(notebook: Any, unified: Mapping[str, Any]) -> dict[str, Any]:
    """``{"kg": ..., "unified_kg": ...}`` for one notebook."""
    job = getattr(notebook, "kg_build", None)
    return {
        "kg": {
            "ready": bool(notebook.kg_ready),
            "building": bool(notebook.kg_building),
            "pending_sources": int(notebook.kg_pending_sources),
            "job": job.model_dump(mode="json") if job else None,
        },
        "unified_kg": {
            "dirty": bool(unified.get("dirty", False)),
            "building": bool(unified.get("viz_building", False)),
            "last_rebuild_at": unified.get("last_rebuild_at", ""),
        },
    }
