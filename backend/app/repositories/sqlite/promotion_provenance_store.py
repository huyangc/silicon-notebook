"""Write side of promotion provenance (PR-E8, B-12) on SQLite.

Twin of ``app/repositories/postgres/promotion_provenance_store.py`` -- the rule
is ``app.domain.promotion_provenance`` and the cost and step notes are written
there once.  SQLite specifics: the two primary-key probes are DRIVEN by the
bound id list (``drive_by``: one seek per id of one object's evidence), and the
inserts skip an existing id (``ON CONFLICT(id) DO NOTHING``).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional, Sequence, Tuple

from app.domain.promotion_provenance import (
    PROMOTION_SOURCE_TYPE,
    OriginElement,
    PromotionPlan,
    evidence_source_ids,
    foreign_element_ids,
    plan_promotion_evidence,
)
from app.repositories.sqlite.id_binding import bind_ids, drive_by
from app.repositories.sqlite.memory_sql import memory_source_type_predicate


def plan_for_library(
    connection: sqlite3.Connection,
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
            for row in connection.execute(
                f"SELECT id,notebook_id FROM sources WHERE {drive_by('id', bound)}",
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
        # A Memory source's element is never read (PostgreSQL twin).
        origin_elements = {
            str(row["id"]): OriginElement(str(row["source_id"]), str(row["text"] or ""))
            for row in connection.execute(
                "SELECT e.id,e.source_id,e.text FROM source_elements e "
                "JOIN sources s ON s.id=e.source_id "
                f"WHERE {drive_by('e.id', bound)} "
                f"AND NOT ({memory_source_type_predicate('s.source_type')})",
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
    connection: sqlite3.Connection,
    base_notebook_id: str,
    plan: PromotionPlan,
    now: str,
) -> None:
    """Insert the plan's promotion sources and elements, reusing existing
    ones, and touch the sources' ``updated_at`` (PostgreSQL twin)."""
    if not plan.sources:
        return
    connection.executemany(
        "INSERT INTO sources (id,notebook_id,title,source_type,status,"
        "parse_status,file_name,file_path,source_url,file_size,file_hash,summary,"
        "doc_type,created_at,updated_at) "
        "VALUES (?,?,?,?,'active','parsed','','','',0,'','','',?,?) "
        "ON CONFLICT(id) DO NOTHING",
        [
            (row.id, base_notebook_id, row.title, PROMOTION_SOURCE_TYPE, now, now)
            for row in plan.sources
        ],
    )
    connection.executemany(
        "INSERT INTO source_elements "
        "(id,source_id,element_type,location_label,text,metadata,created_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        [
            (
                row.id, row.source_id, row.element_type, row.location_label,
                row.text, json.dumps(row.metadata, ensure_ascii=False), now,
            )
            for row in plan.elements
        ],
    )
    bound = bind_ids([row.id for row in plan.sources])
    connection.execute(
        f"UPDATE sources SET updated_at=? WHERE {drive_by('id', bound)}",
        (now, bound.param),
    )


def materialize_promotion_evidence(
    connection: sqlite3.Connection,
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
