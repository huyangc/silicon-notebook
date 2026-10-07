"""Write side of promotion provenance (PR-E8, B-12) on PostgreSQL.

The rule is ``app.domain.promotion_provenance`` (read its docstring first); this
module only does the reads and writes the approving transaction needs.  SQLite
twin: ``app/repositories/sqlite/promotion_provenance_store.py`` (same steps,
same statements in its dialect).

Cost: two primary-key probes bounded by the evidence of ONE promoted object
(its source ids, then its foreign element ids), bound through ``id_binding``
(one parameter, a custom plan), plus one batched insert per table.  Nothing
reads by notebook.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence, Tuple

from app.domain.promotion_provenance import (
    PROMOTION_SOURCE_TYPE,
    OriginElement,
    PromotionPlan,
    evidence_source_ids,
    foreign_element_ids,
    plan_promotion_evidence,
)
from app.repositories.postgres._store_utils import (
    execute_many,
    jsonb,
    normalize_timestamp,
)
from app.repositories.postgres.id_binding import bind_ids, execute_ids, member_of


def plan_for_library(
    connection: Any,
    base_notebook_id: str,
    evidence: Sequence[Any],
    *,
    fallback_origin_notebook_id: str = "",
    memory: Optional[Tuple[str, str]] = None,
) -> PromotionPlan:
    """Read what the rule needs in the caller's transaction and plan."""
    source_notebooks: dict[str, str] = {}
    source_ids = evidence_source_ids(evidence)
    if source_ids:
        bound = bind_ids(source_ids)
        source_notebooks = {
            str(row["id"]): str(row["notebook_id"])
            for row in execute_ids(
                connection,
                f"SELECT id,notebook_id FROM sources WHERE {member_of('id', bound)}",
                (bound.param,),
            ).fetchall()
        }
    own = {
        source_id for source_id, notebook_id in source_notebooks.items()
        if notebook_id == base_notebook_id
    }
    origin_elements: dict[str, OriginElement] = {}
    element_ids = foreign_element_ids(evidence, own)
    if element_ids:
        bound = bind_ids(element_ids)
        origin_elements = {
            str(row["id"]): OriginElement(str(row["source_id"]), str(row["text"] or ""))
            for row in execute_ids(
                connection,
                "SELECT id,source_id,text FROM source_elements "
                f"WHERE {member_of('id', bound)}",
                (bound.param,),
            ).fetchall()
        }
    return plan_promotion_evidence(
        base_notebook_id,
        evidence,
        own_source_ids=own,
        source_notebooks=source_notebooks,
        origin_elements=origin_elements,
        fallback_origin_notebook_id=fallback_origin_notebook_id,
        memory=memory,
    )


def write_plan(
    connection: Any, base_notebook_id: str, plan: PromotionPlan, now: str
) -> None:
    """Insert the plan's promotion sources and elements, reusing existing
    ones (content-addressed ids), and touch the sources' ``updated_at`` so the
    change signal sees the new elements."""
    if not plan.sources:
        return
    stamp = normalize_timestamp(now)
    execute_many(
        connection,
        "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
        "file_name,file_path,source_url,file_size,file_hash,summary,doc_type,"
        "created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,'active','parsed','','','',0,'','','',%s,%s) "
        "ON CONFLICT (id) DO NOTHING",
        [
            (row.id, base_notebook_id, row.title, PROMOTION_SOURCE_TYPE, stamp, stamp)
            for row in plan.sources
        ],
    )
    execute_many(
        connection,
        "INSERT INTO source_elements "
        "(id,source_id,element_type,location_label,text,metadata,created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        [
            (
                row.id, row.source_id, row.element_type, row.location_label,
                row.text, jsonb(row.metadata), stamp,
            )
            for row in plan.elements
        ],
    )
    bound = bind_ids([row.id for row in plan.sources])
    execute_ids(
        connection,
        f"UPDATE sources SET updated_at=%s WHERE {member_of('id', bound)}",
        (stamp, bound.param),
    )


def materialize_promotion_evidence(
    connection: Any,
    base_notebook_id: str,
    evidence: Sequence[Any],
    now: str,
    *,
    fallback_origin_notebook_id: str = "",
    memory: Optional[Tuple[str, str]] = None,
) -> list:
    """Rewrite ``evidence`` for the public library and write the rows it
    names; returns the rewritten evidence (the caller stores it)."""
    plan = plan_for_library(
        connection,
        base_notebook_id,
        evidence,
        fallback_origin_notebook_id=fallback_origin_notebook_id,
        memory=memory,
    )
    write_plan(connection, base_notebook_id, plan, now)
    return plan.evidence
