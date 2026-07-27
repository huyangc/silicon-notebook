"""Re-extract every source of a notebook (offline job).

`extract_source` deletes the source's old KG objects/relations and rebuilds them
from the current extractor, so re-running is idempotent per source. A failing
source is logged and skipped — the rest still run."""

from __future__ import annotations

from typing import List


def reextract_notebook(repo, notebook_id: str) -> List[str]:
    """Re-extract all sources of `notebook_id`. Returns the source ids that
    completed successfully (in order)."""
    done: List[str] = []
    after_id = ""
    while source_ids := repo.maintenance.user_source_ids_page(
        notebook_id, after_id=after_id, limit=500
    ):
        for source_id in source_ids:
            try:
                repo.extract_source(source_id)
                done.append(source_id)
            except Exception as exc:  # noqa: BLE001 — one bad source must not abort the run
                print(f"[reextract] source {source_id} failed: {exc}")
        after_id = source_ids[-1]
        if len(source_ids) < 500:
            break
    return done
