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
    for summary in repo.list_sources(notebook_id):
        try:
            repo.extract_source(summary.id)
            done.append(summary.id)
        except Exception as exc:  # noqa: BLE001 — one bad source must not abort the run
            print(f"[reextract] source {summary.id} failed: {exc}")
    return done
