"""Merge notebook and global Ask samples for the post-completion learning chains.

Pure leaf module shared by both ``ask_state_store`` adapters (they must not
import each other): the notebook arm and the global arm of one sampler are
each bounded in SQL, and these helpers fold the two into ONE newest-first
sample under the same two bounds the single-table read had. Every row here is
already projected (``project_ask_row`` / ``project_run_row`` shapes); the raw
global row contributes only what the projection keeps.
"""
from __future__ import annotations

import json
from typing import Any

from app.domain.global_ask_attribution import touched_notebook_ids
from app.domain.retrieval_experience import project_run_step, project_trace_step
from app.repositories.ports import (
    cap_sampled_steps,
    merge_sampled_rows,
    project_ask_row,
    project_run_row,
)


def _trace_steps(raw: Any) -> list:
    """The persisted ``answer.reasoning_trace`` of a global job, as a list of
    step dicts -- JSON text on SQLite, an already-decoded list on PostgreSQL."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return list(raw) if isinstance(raw, list) else []


def _json_list(raw: Any) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return list(raw) if isinstance(raw, list) else []


def global_row_touches(row: Any, notebook_id: str) -> bool:
    """Did this global job count for ``notebook_id``? The SQL arm prefilters
    on participation (index-friendly); this applies the attribution rule the
    completion hook applied (``touched_notebook_ids``), so a library the run
    resolved but never searched or cited stays out of its overlay sample."""
    skipped = [
        entry.get("notebook_id") if isinstance(entry, dict) else entry
        for entry in _json_list(row["skipped_json"])
    ]
    return notebook_id in touched_notebook_ids(
        _json_list(row["resolved_json"]), _json_list(row["searched_json"]),
        _json_list(row["cited_json"]), skipped,
    )


def _project_steps(raw: Any, project, budget: int) -> list:
    """At most ``budget`` projected steps out of one row's trace. The SQL arm
    already sliced the array to ``step_limit`` elements, and this stops
    decoding once the running newest-first budget is spent, so the step
    ceiling bounds transfer and projection work, not just the returned list."""
    if budget <= 0:
        return []
    projected = []
    for step in _trace_steps(raw):
        if len(projected) >= budget:
            break
        item = project(step)
        if item is not None:
            projected.append(item)
    return projected


def merge_ask_samples(
    asks: list, global_rows: list, job_limit: int, step_limit: int, *, notebook_id: str,
) -> list:
    """Overlay-chain sample: the member's notebook asks (with their trace rows
    already attached) plus their global asks attributed to the notebook."""
    stamps = {ask["job_id"]: ask["created_at"] for ask in asks}
    merged = list(asks)
    # Global rows arrive newest first; the step budget is spent in that order
    # (the same newest-first rule ``cap_sampled_steps`` applies to the merge).
    budget = max(1, int(step_limit))
    for row in global_rows:
        if not global_row_touches(row, notebook_id):
            continue
        ask = project_ask_row(row["id"], row["question"], row["status"], row["created_at"])
        stamps[ask["job_id"]] = ask["created_at"]
        ask["steps"] = _project_steps(row["trace_json"], project_trace_step, budget)
        budget -= len(ask["steps"])
        merged.append(ask)
    sampled = merge_sampled_rows(
        merged, limit=job_limit,
        created_at_of=lambda r: stamps.get(r["job_id"], ""), id_of=lambda r: r["job_id"],
    )
    return cap_sampled_steps(sampled, step_limit=step_limit)


def merge_run_samples(
    runs: list, stamps: dict, global_rows: list, job_limit: int, step_limit: int,
) -> list:
    """Global-partition experience sample: notebook reasoning runs plus global
    reasoning runs, both as opaque ``project_run_row`` rows."""
    stamps = dict(stamps)
    merged = list(runs)
    budget = max(1, int(step_limit))
    for row in global_rows:
        run = project_run_row(row["id"], row["mode"])
        stamps[run["run_id"]] = row["created_at"]
        run["steps"] = _project_steps(row["trace_json"], project_run_step, budget)
        budget -= len(run["steps"])
        merged.append(run)
    sampled = merge_sampled_rows(
        merged, limit=job_limit,
        created_at_of=lambda r: stamps.get(r["run_id"], ""), id_of=lambda r: r["run_id"],
    )
    return cap_sampled_steps(sampled, step_limit=step_limit)
