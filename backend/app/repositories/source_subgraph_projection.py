"""Backend-neutral bounded reads for selected-source graph snapshots.

The caller owns the read transaction.  Every data query constrains source
identity (and relation endpoints) before ``LIMIT``; this module never reads a
whole-notebook graph and filters it afterward.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from app.domain.knowledge_contracts import USABLE_STATUSES


def _in_clause(placeholder: str, values: Sequence[str]) -> str:
    return ",".join(placeholder for _ in values)


def _rows(connection: Any, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]


def _effective_generation(row: Mapping[str, Any]) -> tuple[str, int | None]:
    latest_status = str(row.get("latest_status") or "")
    latest_error = str(row.get("latest_error") or "")
    generation = ""
    generation_error = ""
    if latest_status == "completed" and latest_error.startswith("kg objects="):
        generation = str(row.get("latest_id") or "")
        generation_error = latest_error
    elif latest_status == "completed" and "retry_incomplete=1" in latest_error:
        generation = str(row.get("successful_id") or "")
        generation_error = str(row.get("successful_error") or "")
    match = re.match(r"^kg objects=(\d+)(?:\s|$)", generation_error)
    return generation, (int(match.group(1)) if match else None)


def source_subgraph_signature_on(
    connection: Any,
    notebook_id: str,
    source_ids: Sequence[str],
    *,
    placeholder: str,
    postgres: bool,
) -> tuple:
    """Return a content-free generation signature for one frozen scope.

    ``kg_reset_epoch`` (batch-3-W1 PR-2, design doc Sec 3.2 table #14) is
    appended as a FIFTH, trailing element, and — R1 (P1-2, post-review) —
    CONDITIONALLY: only when ``epoch > 0``, mirroring Sec 3.4's rule for
    ``ScaleArtifactRuntime.version()`` exactly. This tuple is not just a
    process-local cache key: ``build_source_partition``
    (``services/kg/source_partition_index.py``) bakes it verbatim into each
    per-source partition's ON-DISK manifest.json as ``source_signature``,
    compared byte-for-byte on every read (``inspect_source_partition_
    manifest``). Appending unconditionally would make EVERY existing on-disk
    manifest (written before this column existed, 4 elements) compare
    unequal to a freshly computed 5-element signature the instant this PR
    ships — every partition in the fleet would read as
    ``source_partition_identity_mismatch`` and downgrade to the
    no-partition-graph path, AND an epoch-0 notebook's signature would STILL
    be a 5-tuple going forward with no way back to 4, so a partition built
    post-rollout could never again match one built pre-rollout even though
    nothing about that notebook's KG ever reset (Sec 3.4's fleet-wide-
    rebuild-storm hazard, replayed on-disk instead of in the version() memo).
    Gating on epoch > 0 keeps an epoch-0 notebook's signature exactly the
    4-tuple it always was — existing manifests keep matching, no spurious
    identity_mismatch, no forced re-partition. A notebook that HAS been
    through delete_notebook_kg gets a 5-tuple, and its EXISTING 4-element
    on-disk manifest legitimately fails to match: that mismatch is CORRECT,
    not a bug — the KG really was reset since that partition was built, so
    the old partition really is stale, and the read is meant to be refused
    (``source_partition_identity_mismatch``) rather than served. It also
    self-heals: version() (Sec 3.4) already made that SAME epoch bump change
    the notebook's scale-artifact version, so the next scale build/fold
    republishes this notebook's partitions with the new 5-element signature
    baked in, and reads resume matching from then on. See
    ``services/source_partitioned_ppr.py``'s ``_graph`` for the matching
    conditional reconstruction and its ``len(signature)`` gate.
    """
    if not source_ids:
        return (0, 0, 0, ())
    ph = _in_clause(placeholder, source_ids)
    order_id = 'id COLLATE "C"' if postgres else "id"
    source_index_expr = "COALESCE(source_index_backfilled,0)"
    state = connection.execute(
        "SELECT COALESCE(kg_mutation_seq,0) AS kg_seq,"
        "COALESCE(cluster_mutation_seq,0) AS cluster_seq,"
        f"{source_index_expr} AS source_index_backfilled,"
        "COALESCE(kg_reset_epoch,0) AS kg_reset_epoch "
        f"FROM unified_kg_state WHERE notebook_id={placeholder}",
        (notebook_id,),
    ).fetchone()
    source_rows = _rows(
        connection,
        "SELECT s.id,s.updated_at,"
        "(SELECT er.id FROM extraction_runs er WHERE er.notebook_id=s.notebook_id "
        " AND er.source_id=s.id AND er.run_type='kg' "
        f" ORDER BY er.created_at DESC,{('er.id COLLATE "C"' if postgres else 'er.id')} DESC LIMIT 1) AS latest_id,"
        "(SELECT er.status FROM extraction_runs er WHERE er.notebook_id=s.notebook_id "
        " AND er.source_id=s.id AND er.run_type='kg' "
        f" ORDER BY er.created_at DESC,{('er.id COLLATE "C"' if postgres else 'er.id')} DESC LIMIT 1) AS latest_status,"
        "(SELECT er.error_message FROM extraction_runs er WHERE er.notebook_id=s.notebook_id "
        " AND er.source_id=s.id AND er.run_type='kg' "
        f" ORDER BY er.created_at DESC,{('er.id COLLATE "C"' if postgres else 'er.id')} DESC LIMIT 1) AS latest_error,"
        "(SELECT er.id FROM extraction_runs er WHERE er.notebook_id=s.notebook_id "
        " AND er.source_id=s.id AND er.run_type='kg' AND er.status='completed' "
        " AND substr(COALESCE(er.error_message,''),1,11)='kg objects=' "
        f" ORDER BY er.created_at DESC,{('er.id COLLATE "C"' if postgres else 'er.id')} DESC LIMIT 1) AS successful_id,"
        "(SELECT er.error_message FROM extraction_runs er WHERE er.notebook_id=s.notebook_id "
        " AND er.source_id=s.id AND er.run_type='kg' AND er.status='completed' "
        " AND substr(COALESCE(er.error_message,''),1,11)='kg objects=' "
        f" ORDER BY er.created_at DESC,{('er.id COLLATE "C"' if postgres else 'er.id')} DESC LIMIT 1) AS successful_error "
        f"FROM sources s WHERE s.notebook_id={placeholder} "
        "AND s.source_type NOT IN ('memory','knowhow') "
        f"AND s.id IN ({ph}) ORDER BY {order_id}",
        (notebook_id, *source_ids),
    )
    backfills = {
        row["source_id"]: row
        for row in _rows(
            connection,
            "SELECT source_id,source_generation,projection_version,status,"
            "objects_scanned,facts_written,incomplete_objects,incomplete_reason,"
            "failure_code,updated_at FROM knowledge_source_fact_backfills "
            f"WHERE notebook_id={placeholder} AND source_id IN ({ph})",
            (notebook_id, *source_ids),
        )
    }
    per_source = []
    for row in source_rows:
        source_id = str(row["id"])
        generation, expected_objects = _effective_generation(row)
        backfill = backfills.get(source_id, {})
        per_source.append(
            (
                source_id,
                str(row.get("updated_at") or ""),
                str(row.get("latest_id") or ""),
                str(row.get("latest_status") or ""),
                str(row.get("latest_error") or ""),
                str(row.get("successful_id") or ""),
                str(row.get("successful_error") or ""),
                -1 if expected_objects is None else expected_objects,
                str(backfill.get("source_generation") or ""),
                int(backfill.get("projection_version") or 0),
                str(backfill.get("status") or ""),
                int(backfill.get("objects_scanned") or 0),
                int(backfill.get("facts_written") or 0),
                int(backfill.get("incomplete_objects") or 0),
                str(backfill.get("incomplete_reason") or ""),
                str(backfill.get("failure_code") or ""),
                str(backfill.get("updated_at") or ""),
            )
        )
    epoch = int(state["kg_reset_epoch"]) if state else 0
    signature = (
        int(state["kg_seq"]) if state else 0,
        int(state["cluster_seq"]) if state else 0,
        int(state["source_index_backfilled"]) if state else 0,
        tuple(per_source),
    )
    # Conditional, and MUST stay the trailing element — see this function's
    # own docstring for why unconditional is the same fleet-wide hazard
    # Sec 3.4 exists to prevent, replayed on-disk.
    if epoch:
        signature = signature + (epoch,)
    return signature


def _effective_source_states(signature: tuple) -> list[dict[str, Any]]:
    states = []
    for item in signature[3]:
        (
            source_id,
            _source_updated,
            latest_id,
            latest_status,
            latest_error,
            successful_id,
            successful_error,
            expected_objects,
            ledger_generation,
            ledger_version,
            ledger_status,
            *_rest,
        ) = item
        generation, _parsed_expected = _effective_generation(
            {
                "latest_id": latest_id,
                "latest_status": latest_status,
                "latest_error": latest_error,
                "successful_id": successful_id,
                "successful_error": successful_error,
            }
        )
        projection_status = "no_generation"
        projection_version = 0
        if generation:
            if expected_objects < 0:
                projection_status = "missing"
            else:
                projection_status = "live_pending_validation"
            projection_version = 1
            if ledger_generation:
                projection_version = int(ledger_version or 0)
                if ledger_generation != generation:
                    projection_status = "generation_drift"
                elif projection_version != 1:
                    projection_status = "projection_version_unsupported"
                else:
                    projection_status = ledger_status or "missing"
        states.append(
            {
                "source_id": source_id,
                "generation": generation,
                "expected_objects": expected_objects,
                "projection_version": projection_version,
                "projection_status": projection_status,
            }
        )
    return states


def source_subgraph_rows_on(
    connection: Any,
    notebook_id: str,
    source_ids: Sequence[str],
    limits: Mapping[str, int],
    *,
    placeholder: str,
    postgres: bool,
) -> dict[str, Any]:
    signature = source_subgraph_signature_on(
        connection,
        notebook_id,
        source_ids,
        placeholder=placeholder,
        postgres=postgres,
    )
    states = _effective_source_states(signature)
    result: dict[str, Any] = {
        "signature": signature,
        "kg_generation": signature[0],
        "cluster_generation": signature[1],
        "sources": states,
        "objects": [],
        "relations": [],
        "chunks": [],
        "facts": [],
        "fact_elements": [],
        "clusters": [],
        "reasons": [],
    }
    expected = tuple(sorted(set(source_ids)))
    if tuple(row["source_id"] for row in states) != expected:
        result["reasons"].append("source_scope_drift")
        return result
    if not signature[2]:
        result["reasons"].append("source_index_unavailable")
        return result
    safe_statuses = {"live_pending_validation", "complete", "incomplete"}
    if any(row["projection_status"] not in safe_statuses for row in states):
        result["reasons"].append("source_projection_unavailable")
        return result

    ph = _in_clause(placeholder, source_ids)
    status_ph = _in_clause(placeholder, USABLE_STATUSES)
    id_order = ' COLLATE "C"' if postgres else ""

    def bounded(
        name: str, sql: str, params: Sequence[Any], limit_key: str
    ) -> list[dict]:
        limit = max(1, int(limits[limit_key]))
        rows = _rows(connection, sql, (*params, limit + 1))
        if len(rows) > limit:
            result["reasons"].append(f"{name}_limit_exceeded")
        return rows[:limit]

    object_from = (
        "FROM knowledge_object_sources kos JOIN knowledge_objects ko "
        if postgres
        else "FROM knowledge_object_sources kos INDEXED BY idx_kos_source_object "
        "CROSS JOIN knowledge_objects ko "
    )
    result["objects"] = bounded(
        "object",
        (
            "SELECT kos.source_id,ko.id AS object_id,ko.object_type "
            + object_from
            + "ON ko.id=kos.object_id AND ko.notebook_id=kos.notebook_id "
            + f"WHERE kos.notebook_id={placeholder} AND kos.source_id IN ({ph}) "
            + f"AND ko.status IN ({status_ph}) "
            + f"ORDER BY kos.source_id{id_order},ko.id{id_order} LIMIT {placeholder}"
        ),
        (notebook_id, *source_ids, *USABLE_STATUSES),
        "objects",
    )

    generation_clauses = []
    generation_params: list[Any] = [notebook_id]
    for state in states:
        generation_clauses.append(
            f"(f.source_id={placeholder} AND f.source_generation={placeholder})"
        )
        generation_params.extend((state["source_id"], state["generation"]))
    current_generation_sql = " OR ".join(generation_clauses) or "0=1"
    result["facts"] = bounded(
        "fact",
        "SELECT f.id AS fact_id,f.source_id,f.source_generation,"
        "f.global_object_id AS object_id,f.object_type,f.payload,f.evidence,"
        "f.projection_version FROM knowledge_source_facts f "
        f"WHERE f.notebook_id={placeholder} AND ({current_generation_sql}) "
        f"ORDER BY f.source_id{id_order},f.id{id_order} LIMIT {placeholder}",
        generation_params,
        "facts",
    )
    result["fact_elements"] = bounded(
        "fact_element",
        "SELECT fe.fact_id,fe.source_id,fe.source_generation,fe.element_id "
        "FROM knowledge_source_fact_elements fe JOIN knowledge_source_facts f "
        "ON f.id=fe.fact_id AND f.notebook_id=fe.notebook_id "
        "AND f.source_id=fe.source_id AND f.source_generation=fe.source_generation "
        f"WHERE fe.notebook_id={placeholder} AND ({current_generation_sql}) "
        f"ORDER BY fe.source_id{id_order},fe.fact_id{id_order},fe.element_id{id_order} "
        f"LIMIT {placeholder}",
        generation_params,
        "fact_elements",
    )
    chunk_from = (
        "FROM chunks " if postgres else "FROM chunks INDEXED BY idx_chunks_source "
    )
    result["chunks"] = bounded(
        "chunk",
        (
            "SELECT id AS chunk_id,source_id,text,section_path,created_at,element_ids "
            + chunk_from
            + f"WHERE notebook_id={placeholder} AND source_id IN ({ph}) "
            + f"ORDER BY source_id{id_order},id{id_order} LIMIT {placeholder}"
        ),
        (notebook_id, *source_ids),
        "chunks",
    )
    result["relations"] = bounded(
        "relation",
        "SELECT r.id AS relation_id,r.source_id,r.source_object_id,"
        "r.target_object_id,r.edge_type,r.evidence,r.review_status "
        "FROM knowledge_relations r JOIN knowledge_objects so "
        "ON so.id=r.source_object_id AND so.notebook_id=r.notebook_id "
        "JOIN knowledge_objects to2 ON to2.id=r.target_object_id "
        "AND to2.notebook_id=r.notebook_id "
        f"WHERE r.notebook_id={placeholder} AND r.source_id IN ({ph}) "
        "AND COALESCE(r.review_status,'pending')!='rejected' "
        f"AND so.status IN ({status_ph}) AND to2.status IN ({status_ph}) "
        "AND EXISTS (SELECT 1 FROM knowledge_object_sources ks "
        " WHERE ks.notebook_id=r.notebook_id AND ks.object_id=r.source_object_id "
        f" AND ks.source_id IN ({ph})) "
        "AND EXISTS (SELECT 1 FROM knowledge_object_sources kt "
        " WHERE kt.notebook_id=r.notebook_id AND kt.object_id=r.target_object_id "
        f" AND kt.source_id IN ({ph})) "
        f"ORDER BY r.id{id_order} LIMIT {placeholder}",
        (
            notebook_id,
            *source_ids,
            *USABLE_STATUSES,
            *USABLE_STATUSES,
            *source_ids,
            *source_ids,
        ),
        "relations",
    )
    # 批 3·W2 §1.4:published 代次谓词(绑定参数,COALESCE 兜无 state 行)。
    published_gen = (
        "cc.generation = COALESCE((SELECT cluster_generation "
        f"FROM unified_kg_state WHERE notebook_id = {placeholder}), 0)"
    )
    if postgres:
        cluster_sql = (
            "SELECT cc.canonical_id,cc.member_object_id "
            "FROM concept_clusters cc JOIN knowledge_objects ko "
            "ON ko.id=cc.member_object_id AND ko.notebook_id=cc.notebook_id "
            f"WHERE cc.notebook_id={placeholder} "
            f"AND {published_gen} "
            f"AND ko.status IN ({status_ph}) "
            "AND EXISTS (SELECT 1 FROM knowledge_object_sources kos "
            " WHERE kos.notebook_id=cc.notebook_id "
            "AND kos.object_id=cc.member_object_id "
            f" AND kos.source_id IN ({ph})) "
            f"ORDER BY cc.canonical_id{id_order},cc.member_object_id{id_order} "
            f"LIMIT {placeholder}"
        )
        cluster_params = (notebook_id, notebook_id, *USABLE_STATUSES, *source_ids)
    else:
        # CROSS JOIN fixes the SQLite loop order: selected source memberships
        # are the outer relation, so rows owned only by unselected sources are
        # never visited before the bounded cluster lookup.
        cluster_sql = (
            "SELECT DISTINCT cc.canonical_id,cc.member_object_id "
            "FROM knowledge_object_sources kos INDEXED BY idx_kos_source_object "
            "CROSS JOIN knowledge_objects ko ON ko.id=kos.object_id "
            "AND ko.notebook_id=kos.notebook_id "
            "CROSS JOIN concept_clusters cc INDEXED BY idx_clusters_member "
            "ON cc.member_object_id=ko.id AND cc.notebook_id=ko.notebook_id "
            f"WHERE kos.notebook_id={placeholder} AND kos.source_id IN ({ph}) "
            f"AND ko.status IN ({status_ph}) "
            f"AND {published_gen} "
            "ORDER BY cc.canonical_id,cc.member_object_id "
            f"LIMIT {placeholder}"
        )
        cluster_params = (notebook_id, *source_ids, *USABLE_STATUSES, notebook_id)
    result["clusters"] = bounded(
        "cluster",
        cluster_sql,
        cluster_params,
        "cluster_memberships",
    )
    return result


def source_graph_partition_rows_on(
    connection: Any,
    notebook_id: str,
    source_id: str,
    *,
    placeholder: str,
    postgres: bool,
    limits: Mapping[str, int],
) -> dict[str, Any]:
    """Read one bounded *offline* graph partition without touching peers.

    Unlike :func:`source_subgraph_rows_on`, this projection is used only by the
    scale-index build/fold job. Every statement starts from one source identity
    and uses the configured LIMIT+1 ceiling, so cost is bounded by that source
    rather than the notebook. The payload excludes
    source/chunk text and fact payloads: the partition builder needs only
    authorization, endpoint types, evidence ownership and element bindings.
    """
    signature = source_subgraph_signature_on(
        connection,
        notebook_id,
        (source_id,),
        placeholder=placeholder,
        postgres=postgres,
    )
    states = _effective_source_states(signature)
    result: dict[str, Any] = {
        "signature": signature,
        "source": states[0] if len(states) == 1 else None,
        "objects": [],
        "facts": [],
        "fact_elements": [],
        "chunks": [],
        "relations": [],
        "clusters": [],
        "reasons": [],
    }
    if len(states) != 1 or states[0]["source_id"] != source_id:
        result["reasons"].append("source_scope_drift")
        return result
    if not signature[2]:
        result["reasons"].append("source_index_unavailable")
        return result
    state = states[0]
    if (
        state["projection_status"]
        not in {
            "live_pending_validation",
            "complete",
            "incomplete",
        }
        or not state["generation"]
    ):
        result["reasons"].append("source_projection_unavailable")
        return result

    status_ph = _in_clause(placeholder, USABLE_STATUSES)
    order = ' COLLATE "C"' if postgres else ""

    def bounded(
        name: str, sql: str, params: Sequence[Any], limit_key: str
    ) -> list[dict[str, Any]]:
        limit = max(1, int(limits[limit_key]))
        rows = _rows(connection, sql + f" LIMIT {placeholder}", (*params, limit + 1))
        if len(rows) > limit:
            result["reasons"].append(f"{name}_limit_exceeded")
        return rows[:limit]

    object_from = (
        "FROM knowledge_object_sources kos JOIN knowledge_objects ko "
        if postgres
        else "FROM knowledge_object_sources kos INDEXED BY idx_kos_source_object "
        "CROSS JOIN knowledge_objects ko "
    )
    result["objects"] = bounded(
        "object",
        "SELECT ko.id AS object_id,ko.object_type "
        + object_from
        + "ON ko.id=kos.object_id AND ko.notebook_id=kos.notebook_id "
        + f"WHERE kos.notebook_id={placeholder} AND kos.source_id={placeholder} "
        + f"AND ko.status IN ({status_ph}) ORDER BY ko.id{order}",
        (notebook_id, source_id, *USABLE_STATUSES),
        "objects",
    )
    result["facts"] = bounded(
        "fact",
        "SELECT id AS fact_id,global_object_id AS object_id,object_type,"
        "projection_version FROM knowledge_source_facts "
        f"WHERE notebook_id={placeholder} AND source_id={placeholder} "
        f"AND source_generation={placeholder} ORDER BY id{order}",
        (notebook_id, source_id, state["generation"]),
        "facts",
    )
    result["fact_elements"] = bounded(
        "fact_element",
        "SELECT fact_id,element_id FROM knowledge_source_fact_elements "
        f"WHERE notebook_id={placeholder} AND source_id={placeholder} "
        f"AND source_generation={placeholder} "
        f"ORDER BY fact_id{order},element_id{order}",
        (notebook_id, source_id, state["generation"]),
        "fact_elements",
    )
    chunk_from = (
        "FROM chunks " if postgres else "FROM chunks INDEXED BY idx_chunks_source "
    )
    result["chunks"] = bounded(
        "chunk",
        "SELECT id AS chunk_id,element_ids "
        + chunk_from
        + f"WHERE notebook_id={placeholder} AND source_id={placeholder} "
        f"ORDER BY id{order}",
        (notebook_id, source_id),
        "chunks",
    )
    # Endpoint ownership is deliberately not restricted to this one partition:
    # an A-owned relation whose target is authorized by selected B must become
    # available when A+B are combined.  Runtime rechecks both endpoints against
    # the union of selected object partitions before admitting the edge.
    result["relations"] = bounded(
        "relation",
        "SELECT r.id AS relation_id,r.source_object_id,r.target_object_id,"
        "r.edge_type,r.evidence,r.review_status "
        "FROM knowledge_relations r JOIN knowledge_objects so "
        "ON so.id=r.source_object_id AND so.notebook_id=r.notebook_id "
        "JOIN knowledge_objects to2 ON to2.id=r.target_object_id "
        "AND to2.notebook_id=r.notebook_id "
        f"WHERE r.notebook_id={placeholder} AND r.source_id={placeholder} "
        "AND COALESCE(r.review_status,'pending')!='rejected' "
        f"AND so.status IN ({status_ph}) AND to2.status IN ({status_ph}) "
        f"ORDER BY r.id{order}",
        (notebook_id, source_id, *USABLE_STATUSES, *USABLE_STATUSES),
        "relations",
    )
    # 批 3·W2 §1.4:published 代次谓词(同上一站点)。
    published_gen = (
        "cc.generation = COALESCE((SELECT cluster_generation "
        f"FROM unified_kg_state WHERE notebook_id = {placeholder}), 0)"
    )
    if postgres:
        cluster_sql = (
            "SELECT cc.canonical_id,cc.member_object_id "
            "FROM concept_clusters cc JOIN knowledge_object_sources kos "
            "ON kos.notebook_id=cc.notebook_id "
            "AND kos.object_id=cc.member_object_id "
            f"WHERE cc.notebook_id={placeholder} AND kos.source_id={placeholder} "
            f"AND {published_gen} "
            f"ORDER BY cc.canonical_id{order},cc.member_object_id{order}"
        )
    else:
        cluster_sql = (
            "SELECT cc.canonical_id,cc.member_object_id "
            "FROM knowledge_object_sources kos INDEXED BY idx_kos_source_object "
            "CROSS JOIN concept_clusters cc INDEXED BY idx_clusters_member "
            "ON cc.notebook_id=kos.notebook_id "
            "AND cc.member_object_id=kos.object_id "
            f"WHERE kos.notebook_id={placeholder} AND kos.source_id={placeholder} "
            f"AND {published_gen} "
            "ORDER BY cc.canonical_id,cc.member_object_id"
        )
    result["clusters"] = bounded(
        "cluster",
        cluster_sql,
        (notebook_id, source_id, notebook_id),
        "cluster_memberships",
    )
    return result
