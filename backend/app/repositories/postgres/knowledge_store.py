"""Knowledge-object persistence store (Task 13).

Owns the knowledge type/list/graph/schema/provenance/FTS SQL plus the
``add_relations`` / ``relations_for_notebook`` compatibility primitives and
the connection-taking chunk writes ``store_kg`` rides.

Composition rules (Gate 5): primitives take the CALLER's connection wherever
the facade owns a transaction/connection boundary today — commit boundaries,
``_write`` trace patches and ``_connect`` spies keep observing every query
because the (possibly wrapped) connection object flows through unchanged.
Only ``get_object_row`` opens its own read connection (plan-frozen signature).
SQL text is moved verbatim — statement-matching failure-injection wrappers in
the frozen suites keep binding.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence

from psycopg import errors, sql

from app.core.json_safety import validate_finite_json
from app.models.common import Evidence
from app.repositories.lexical_query import corpus_gated_recall_terms
from app.repositories.ports import ChunkLexicalSearchTimeout
from app.repositories.postgres._store_utils import (
    execute_many,
    iso_timestamp,
    json_value,
    jsonb,
    normalize_timestamp,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.mount_sql import (
    MOUNT_JOIN,
    MOUNT_VALID,
    MOUNTED_BASE_IDS_SUBQUERY,
)
from app.repositories.postgres.search import (
    chunk_candidate_documents,
    chunk_candidate_rows_for_terms,
    chunk_exact_candidate_rows,
    deterministic_lexical_score_terms,
    knowledge_candidate_documents,
    knowledge_candidate_rows_for_terms,
)


_GRAPH_RESET_TABLES = frozenset({
    "concept_clusters",
    "concept_merge_candidates",
    "knowledge_embeddings",
    "extraction_runs",
    "kg_relation_completion_state",
    # kg_analysis_artifacts: the KG-quality-analysis product LEDGER (batch-3-W1
    # PR-2, design doc Sec 3.2 table #15). Blanket-deleted here, same as
    # concept_clusters/concept_merge_candidates/kg_relation_completion_state —
    # NOT reset like unified_kg_state (below), because a ledger row's whole
    # meaning is "this artifact was built at this seq"; there is no
    # meaningful "reset" shape for it short of not existing. Before this was
    # added, a cleared graph left its (notebook_id, kind) rows carrying a
    # kg_mutation_seq from BEFORE the reset, so kg_analysis's
    # _artifact_freshness computed seq_behind = 0 - built < 0 — a value the
    # module's own contract treats as "database was hand-edited" (see
    # kg_analysis.py's _artifact_freshness docstring). Deleting the ledger
    # rows here makes the post-clear read report ABSENCE_NEVER_COMPUTED
    # instead, which is the truthful answer.
    "kg_analysis_artifacts",
    # kg_community_edges / kg_source_profiles: the DETAIL tables the ledger's
    # two board-dependent kinds describe. R1 (P2-1, post-review): the ledger
    # row and its detail rows are a documented single unit —
    # discard_board_dependent_kg_analysis_artifacts's own docstring (both
    # backends' unified_kg_store.py) states plainly "明细行与账本行都删...
    # 留着的是指向已重铸板块 id 的悬空行". Deleting only the ledger kind rows
    # here (as an earlier revision of this set did) would leave exactly that
    # dangling half behind — at production scale up to 200k cross-board edge
    # rows / ~source-count profile rows per notebook, never cleaned by
    # anything else once the ledger row that would have gated a re-read of
    # them (_detail_rows_are_readable) is gone.
    "kg_community_edges",
    "kg_source_profiles",
    # unified_kg_state is DELIBERATELY NOT a member of this set: it is reset
    # in place by the explicit UPDATE at the end of
    # delete_notebook_graph_rows (design doc Sec 3.3 option C), not
    # blanket-deleted by the loop below — see that UPDATE's comment for why.
})

_DELETE_OBJECT_BATCH_SIZE = 500


def _retrieval_evidence(raw: object) -> list[Evidence]:
    """Hydrate valid evidence cards while tolerating malformed legacy items."""
    items = json_value(raw, [])
    if not isinstance(items, list):
        return []
    evidence: list[Evidence] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            evidence.append(Evidence(**item))
        except (TypeError, ValueError):
            continue
    return evidence


def _lexical_candidate_union(
    db: Any,
    notebook_id: str,
    query: str,
    k: int,
    *,
    candidate_rows_for_terms,
    candidate_documents,
    output_id: str,
    text_field: str,
    allowed_source_ids: Sequence[str] | None = None,
    corpus_langs: Sequence[str] | None = None,
    allow_knn: bool = False,
    authoritative_source_filter: bool = False,
    knn_max_term_chars: int | None = None,
    routing_stats: dict[str, int | float] | None = None,
) -> list[dict]:
    """Build the PostgreSQL equivalent of SQLite's bounded FTS OR query.

    Every surviving term costs one real LATERAL probe here, which is why the
    corpus gate matters far more on this backend than on SQLite: see
    `corpus_gated_recall_terms` for the measured 29.7s → 0.96s.

    `allow_knn` rides the same seam as `corpus_langs`: a producer-side hint the
    shared union logic forwards without interpreting.  The knowledge producer
    may honour it (GiST `<->` early stop); the chunk producer ignores it.
    """
    terms = corpus_gated_recall_terms(query, corpus_langs)
    result_limit = max(0, int(k))
    if not terms or not result_limit:
        return []

    # Give every term a bounded share so an exact-phrase crowd cannot suppress
    # a rare independent identifier.  The union stays below 4K + 64 rows.
    candidate_budget = max(result_limit * 4, len(terms), 12)
    per_term_limit = max(1, (candidate_budget + len(terms) - 1) // len(terms))
    producer_options: dict[str, Any] = {"allow_knn": allow_knn}
    if knn_max_term_chars is not None:
        producer_options["knn_max_term_chars"] = knn_max_term_chars
    if routing_stats is not None:
        producer_options["routing_stats"] = routing_stats
    if allowed_source_ids is None:
        candidates = candidate_rows_for_terms(
            db, notebook_id, terms, per_term_limit, **producer_options
        )
    else:
        candidates = candidate_rows_for_terms(
            db, notebook_id, terms, per_term_limit,
            list(dict.fromkeys(allowed_source_ids)),
            authoritative_source_filter=authoritative_source_filter,
            **producer_options,
        )
    if not candidates:
        return []

    groups: dict[int, list[str]] = {rank: [] for rank in range(len(terms))}
    term_ranks_by_id: dict[str, set[int]] = {}
    for row in candidates:
        candidate_id = row["candidate_id"]
        term_rank = int(row["term_rank"])
        groups[term_rank].append(candidate_id)
        term_ranks_by_id.setdefault(candidate_id, set()).add(term_rank)

    # Round-robin the scarcest term groups first.  This enforces the global K
    # hydration bound and prevents a common term from crowding out a rare id.
    ordered_ranks = sorted(groups, key=lambda rank: (len(groups[rank]), rank))
    positions = {rank: 0 for rank in ordered_ranks}
    ids: list[str] = []
    selected: set[str] = set()
    while len(ids) < result_limit:
        progressed = False
        for rank in ordered_ranks:
            group = groups[rank]
            while positions[rank] < len(group):
                candidate_id = group[positions[rank]]
                positions[rank] += 1
                if candidate_id in selected:
                    continue
                selected.add(candidate_id)
                ids.append(candidate_id)
                progressed = True
                break
            if len(ids) >= result_limit:
                break
        if not progressed:
            break

    rows = candidate_documents(db, ids)
    output = []
    for row in rows:
        text = row[text_field] or ""
        matched_terms = [terms[rank] for rank in term_ranks_by_id[row["id"]]]
        score = deterministic_lexical_score_terms(matched_terms, text)
        output.append({
            output_id: row["id"],
            "score": score,
            "match": "lexical",
            **({"name": text} if output_id == "object_id" else {}),
        })
    output.sort(key=lambda item: (-item["score"], item[output_id]))
    return output[:result_limit]


def _json_document(value: Any, *, expected: type, field: str) -> Any:
    if isinstance(value, str):
        value = json.loads(value)
    if value is None or not isinstance(value, expected):
        raise ValueError(f"{field} must be a {expected.__name__}")
    validate_finite_json(value, field=field)
    return value


def _json_text(value: Any, default: Any) -> str:
    parsed = json_value(value, default)
    return json.dumps(parsed, ensure_ascii=False, allow_nan=False)


def _compat_rows(rows, *, payload: bool = False, evidence: bool = False):
    output = []
    for row in rows:
        item = dict(row)
        if payload and "payload" in item:
            item["payload"] = _json_text(item["payload"], {})
        if evidence and "evidence" in item:
            item["evidence"] = _json_text(item["evidence"], [])
        for column in ("created_at", "updated_at", "last_reviewed"):
            if column in item:
                item[column] = iso_timestamp(item[column])
        output.append(item)
    return output


def _compat_schema_rows(rows):
    output = _compat_rows(rows)
    for row in output:
        row["fields"] = _json_text(row.get("fields"), [])
        row["list_fields"] = _json_text(row.get("list_fields"), [])
    return output


def _object_insert_row(row: Sequence[Any]) -> tuple:
    if len(row) != 9:
        raise ValueError("knowledge object insert row must contain nine values")
    return (
        row[0], row[1], row[2], row[3],
        jsonb(_json_document(row[4], expected=dict, field="knowledge payload")),
        jsonb(_json_document(row[5], expected=list, field="knowledge evidence")),
        row[6], normalize_timestamp(row[7]), normalize_timestamp(row[8]),
    )


def _relation_insert_row(row: Sequence[Any]) -> tuple:
    if len(row) != 8:
        raise ValueError("knowledge relation insert row must contain eight values")
    return (
        row[0], row[1], row[2], row[3], row[4], row[5],
        jsonb(_json_document(row[6], expected=list, field="relation evidence")),
        normalize_timestamp(row[7]),
    )


def _completion_generation_is_current(
    connection: Any,
    notebook_id: str,
    source_id: str,
    run_id: str,
) -> bool:
    row = connection.execute(
        "SELECT er.id FROM extraction_runs er JOIN sources s ON s.id=er.source_id "
        "WHERE er.notebook_id=%s AND er.source_id=%s "
        "AND s.source_type NOT IN ('memory','knowhow') "
        "ORDER BY er.created_at DESC, er.id COLLATE \"C\" DESC LIMIT 1",
        (notebook_id, source_id),
    ).fetchone()
    return bool(row and row["id"] == run_id)


# batch-3-W1 T-5a: the ordered (table, WHERE-predicate) registry the pre-reset
# DRAIN pages over — SQLite twin's ``_GRAPH_DRAIN_STEPS`` comment has the full
# ordering/convergence rationale. Entries mirror ``delete_notebook_graph_
# rows``'s statements in its EFFECTIVE order: the five predicate deletes, then
# ``sorted(_GRAPH_RESET_TABLES - {knowledge_embeddings, extraction_runs})``
# (spelled out literally here — the mirror test pins agreement), then the two
# trailing predicate deletes. PostgreSQL has no kg_objects_fts shadow, so no
# 14th entry.
_GRAPH_DRAIN_STEPS: tuple[tuple[str, str, int, bool], ...] = (
    (
        "knowledge_source_fact_backfills",
        "notebook_id=%s AND NOT EXISTS (SELECT 1 FROM sources s "
        "WHERE s.id=knowledge_source_fact_backfills.source_id AND s.notebook_id=%s "
        "AND s.source_type IN ('memory','knowhow'))",
        2,
        True,
    ),
    # codex #663 R5 P1 + R9 P2: knowledge_source_fact_elements cascades
    # off its parent fact (fk_ksfe_fact ON DELETE CASCADE, both backends)
    # with unbounded elements-per-fact fan-out — without its own drain
    # step, a fact page (or the final transaction, when facts sit under
    # the threshold) cascade-deletes arbitrarily many child rows and the
    # row budget stops bounding anything. MIRRORED (R9 P2): the final
    # pass now runs the byte-identical DELETE explicitly before the
    # facts' own — the FK cascade then finds nothing, and the deleted
    # child rows show up in counts instead of vanishing into the
    # cascade. The predicate rides ksfe's OWN source_id (stamped from
    # the same extraction run as its fact's), so it marks exactly the
    # children the cascade would have removed.
    (
        "knowledge_source_fact_elements",
        "notebook_id=%s AND NOT EXISTS (SELECT 1 FROM sources s "
        "WHERE s.id=knowledge_source_fact_elements.source_id "
        "AND s.notebook_id=%s AND s.source_type IN ('memory','knowhow'))",
        2,
        True,
    ),
    (
        "knowledge_source_facts",
        "notebook_id=%s AND NOT EXISTS (SELECT 1 FROM sources s "
        "WHERE s.id=knowledge_source_facts.source_id AND s.notebook_id=%s "
        "AND s.source_type IN ('memory','knowhow'))",
        2,
        True,
    ),
    (
        "knowledge_relations",
        "notebook_id = %s AND NOT EXISTS (SELECT 1 FROM sources s "
        "WHERE s.id=knowledge_relations.source_id "
        "AND s.notebook_id=%s AND s.source_type IN ('memory','knowhow'))",
        2,
        True,
    ),
    # codex #663 R3 P1: the two dependent tables with UNBOUNDED per-object
    # fan-out (cluster memberships; provenance rows of merged objects) get
    # their own ROW-budgeted pre-drain BEFORE any parent object dies —
    # each page is one bounded statement in its own transaction, so the
    # knowledge_objects page's in-transaction cascade (kept for the F3
    # no-orphan-commit invariant) only ever sweeps the residue that landed
    # after these steps exhausted. mirrored=False: the final pass needs no
    # counterpart (its atomic DELETEs cover these rows via their own
    # object-missing/blanket statements), so the mirror test skips them.
    # Deleting a LIVE object's membership/provenance row early is safe —
    # it is a sparser reverse index, never a dangling reference.
    (
        "concept_clusters",
        "notebook_id=%s AND member_object_id IN "
        "(SELECT ko.id FROM knowledge_objects ko WHERE ko.notebook_id=%s AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=ko.source_id AND s.notebook_id=%s AND s.source_type IN ('memory','knowhow')))",
        3,
        False,
    ),
    (
        "knowledge_object_sources",
        "notebook_id=%s AND object_id IN "
        "(SELECT ko.id FROM knowledge_objects ko WHERE ko.notebook_id=%s AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=ko.source_id AND s.notebook_id=%s AND s.source_type IN ('memory','knowhow')))",
        3,
        False,
    ),
    (
        "knowledge_objects",
        "notebook_id = %s AND NOT EXISTS (SELECT 1 FROM sources s "
        "WHERE s.id=knowledge_objects.source_id "
        "AND s.notebook_id=%s AND s.source_type IN ('memory','knowhow'))",
        2,
        True,
    ),
    (
        "knowledge_object_sources",
        "notebook_id=%s AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
        "WHERE ko.notebook_id=%s AND ko.id=knowledge_object_sources.object_id)",
        2,
        True,
    ),
    ("concept_clusters", "notebook_id = %s", 1, True),
    ("concept_merge_candidates", "notebook_id = %s", 1, True),
    ("kg_analysis_artifacts", "notebook_id = %s", 1, True),
    ("kg_community_edges", "notebook_id = %s", 1, True),
    ("kg_relation_completion_state", "notebook_id = %s", 1, True),
    ("kg_source_profiles", "notebook_id = %s", 1, True),
    (
        "knowledge_embeddings",
        "notebook_id=%s AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
        "WHERE ko.notebook_id=%s AND ko.id=knowledge_embeddings.object_id)",
        2,
        True,
    ),
    (
        "extraction_runs",
        "notebook_id=%s AND NOT EXISTS (SELECT 1 FROM sources s "
        "WHERE s.id=extraction_runs.source_id "
        "AND s.notebook_id=%s AND s.source_type IN ('memory','knowhow'))",
        2,
        True,
    ),
)


class KnowledgeStore:
    # 能力声明(镜像 SQLite 侧的 False):这个适配器的 fts_search 能兑现 KNN
    # 访问路径提示,所以服务层的规模判定值得跑。
    lexical_knn_capable = True

    def __init__(self, database: PostgresDatabase, seams) -> None:
        self.database = database
        self.seams = seams

    def _connect(self):
        return self.database.connect()

    # ------------------------------------------------ lifecycle projections
    @staticmethod
    def begin_graph_reset_isolation(db) -> None:
        """batch-3-W1 T-5a (codex #663 R12 P1): PostgreSQL: pin the transaction snapshot BEFORE the in-tx bound probe

        Runs as the FIRST statement of ``delete_notebook_kg``'s final
        transaction. On PostgreSQL, REPEATABLE READ freezes the
        snapshot at that first statement, so the in-transaction bound
        probe and every subsequent DELETE see the SAME row set — a
        concurrent ``store_kg`` committing mid-transaction can no
        longer inflate a DELETE past what the probe admitted. Rows
        committed after the snapshot survive the reset (the final
        pass's pre-existing READ COMMITTED semantics already allowed
        that; the production rebuild's re-extraction converges them),
        and a write-write serialization conflict (SQLSTATE 40001,
        e.g. the unified_kg_state upsert racing a concurrent
        ``mark_dirty``) aborts the attempt cleanly — the caller's
        retry loop treats it exactly like a failed bound probe."""
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")

    @staticmethod
    def graph_drain_backlog(
        db: Any, notebook_id: str, threshold: int, start: int = 0
    ) -> "tuple[str, int] | None":
        """batch-3-W1 T-5a — SQLite twin's docstring has the full rationale
        (exists-at-offset point probe, read-only, keeps the small-graph
        ``delete_notebook_kg`` path free of extra write transactions; the
        ``start`` cursor rides the registry's strictly-forward convergence,
        T-5a review F4)."""
        for index in range(max(0, int(start)), len(_GRAPH_DRAIN_STEPS)):
            table, predicate, params, _mirrored = _GRAPH_DRAIN_STEPS[index]
            row = db.execute(
                f"SELECT 1 FROM {table} WHERE {predicate} "
                f"LIMIT 1 OFFSET {int(threshold)}",
                (notebook_id,) * params,
            ).fetchone()
            if row is not None:
                return table, index
        return None

    @staticmethod
    def drain_notebook_graph_rows_page(
        db: Any, notebook_id: str, step: int, limit: int
    ) -> dict[str, int]:
        """batch-3-W1 T-5a — one bounded page of ``table``'s matching rows,
        §1.5 form-two (``ctid IN (SELECT ctid … LIMIT n)``; ctid is the only
        universal row address here — several of these tables have composite
        or no single-column PKs). SQLite twin's docstring carries the
        caller-owns-the-transaction + same-tx choke-point-bump census
        discipline AND the F3 rationale for the ``knowledge_objects``
        special page below (same-transaction dependent cleanup, the
        ``_delete_object_id_batch`` shape — ``= ANY(%s)`` here, mirroring
        that method's own PostgreSQL spelling)."""
        if not 0 <= int(step) < len(_GRAPH_DRAIN_STEPS):
            raise ValueError(f"unknown graph drain step: {step}")
        table, predicate, params, _mirrored = _GRAPH_DRAIN_STEPS[int(step)]
        if table == "knowledge_source_facts":
            # codex #663 R14 P2 — SQLite twin's comment has the rationale
            # (explicit counted child delete before the parent page; the FK
            # cascade then finds nothing).
            # codex #663 R15 P2b(同 ko 页):FOR UPDATE 让并发替换等到本页
            # 提交,子行不会在「删子」与「删父」之间落地成孤儿。
            ids = [
                row["id"] for row in db.execute(
                    f"SELECT id FROM knowledge_source_facts WHERE {predicate} "
                    f"LIMIT {int(limit)} FOR UPDATE",
                    (notebook_id,) * params,
                ).fetchall()
            ]
            if not ids:
                return {}
            counts = {"knowledge_source_facts": 0,
                      "knowledge_source_fact_elements": 0}
            # codex #663 R16 P1b + R18 P2 — SQLite twin's comment: ONE
            # budgeted child sweep per page transaction; budget filled →
            # parents stay, the backlog probe re-selects this step.
            cur = db.execute(
                "DELETE FROM knowledge_source_fact_elements "
                "WHERE ctid IN (SELECT ctid "
                "FROM knowledge_source_fact_elements "
                f"WHERE fact_id = ANY(%s) LIMIT {int(limit)})",
                (ids,),
            )
            counts["knowledge_source_fact_elements"] = cur.rowcount
            if cur.rowcount >= int(limit):
                return {name: n for name, n in counts.items() if n}
            for offset in range(0, len(ids), _DELETE_OBJECT_BATCH_SIZE):
                batch = ids[offset : offset + _DELETE_OBJECT_BATCH_SIZE]
                cur = db.execute(
                    "DELETE FROM knowledge_source_facts WHERE id = ANY(%s)",
                    (batch,),
                )
                counts["knowledge_source_facts"] += cur.rowcount
            return {name: n for name, n in counts.items() if n}
        if table == "knowledge_objects":
            # codex #663 R15 P2b: FOR UPDATE — a concurrent store_kg
            # replacing one of these objects must WAIT until this page
            # commits, or its re-inserted embedding/provenance rows could
            # land between our dependent DELETE and the parent DELETE and
            # come out the other side as committed orphans.
            ids = [
                row["id"] for row in db.execute(
                    f"SELECT id FROM knowledge_objects WHERE {predicate} "
                    f"LIMIT {int(limit)} FOR UPDATE",
                    (notebook_id,) * params,
                ).fetchall()
            ]
            if not ids:
                return {}
            # codex #663 R2 P2 ×2 — SQLite twin's comment has the full
            # rationale (per-statement bound via _DELETE_OBJECT_BATCH_SIZE
            # sub-batches inside the one page transaction; counts from the
            # object DELETE's actual rowcount, never len(ids)).
            counts = {
                "knowledge_objects": 0,
                "knowledge_embeddings": 0,
                "concept_clusters": 0,
                "knowledge_object_sources": 0,
            }
            for offset in range(0, len(ids), _DELETE_OBJECT_BATCH_SIZE):
                batch = ids[offset : offset + _DELETE_OBJECT_BATCH_SIZE]
                cur = db.execute(
                    "DELETE FROM knowledge_embeddings WHERE object_id = ANY(%s)",
                    (batch,),
                )
                counts["knowledge_embeddings"] += cur.rowcount
                cur = db.execute(
                    "DELETE FROM concept_clusters "
                    "WHERE notebook_id = %s AND member_object_id = ANY(%s)",
                    (notebook_id, batch),
                )
                counts["concept_clusters"] += cur.rowcount
                cur = db.execute(
                    "DELETE FROM knowledge_object_sources WHERE object_id = ANY(%s)",
                    (batch,),
                )
                counts["knowledge_object_sources"] += cur.rowcount
                cur = db.execute(
                    "DELETE FROM knowledge_objects WHERE id = ANY(%s)", (batch,)
                )
                counts["knowledge_objects"] += cur.rowcount
            return {name: n for name, n in counts.items() if n}
        cur = db.execute(
            f"DELETE FROM {table} WHERE ctid IN ("
            f"SELECT ctid FROM {table} WHERE {predicate} LIMIT {int(limit)})",
            (notebook_id,) * params,
        )
        return {table: cur.rowcount} if cur.rowcount else {}

    @staticmethod
    def delete_notebook_graph_rows(db: Any, notebook_id: str, now: str) -> dict[str, int]:
        """Wipe user-document KG while preserving hidden projection lifecycles.

        Memory extraction is owned by confirmation/edit, and Knowhow projection
        is deterministic zero-LLM output. A document rebuild must not delete
        either source's objects/relations; Memory embeddings and extraction runs
        are preserved with them. Notebook-wide derived cluster/state tables are
        rebuilt from the surviving plus newly extracted objects.

        ``now`` (batch-3-W1 PR-2) stamps the ``unified_kg_state`` reset's
        ``updated_at`` — the same caller-supplied-clock seam every other
        writer of this table already threads through (``mark_dirty`` /
        ``bump_cluster_seq``), not a bare SQL ``now()``, so tests keep an
        injectable clock here too.
        """
        counts: dict[str, int] = {}
        cur = db.execute(
            "DELETE FROM knowledge_source_fact_backfills WHERE notebook_id=%s "
            "AND NOT EXISTS (SELECT 1 FROM sources s "
            "WHERE s.id=knowledge_source_fact_backfills.source_id AND s.notebook_id=%s "
            "AND s.source_type IN ('memory','knowhow'))",
            (notebook_id, notebook_id),
        )
        counts["knowledge_source_fact_backfills"] = cur.rowcount
        # codex #663 R9 P2: explicit, COUNTED child delete before the
        # parent facts — the FK cascade (fk_ksfe_fact) then finds
        # nothing, so the aggregate counts contract covers these rows
        # and the drain registry keeps a 1:1 mirrored statement.
        cur = db.execute(
            "DELETE FROM knowledge_source_fact_elements WHERE notebook_id=%s "
            "AND NOT EXISTS (SELECT 1 FROM sources s "
            "WHERE s.id=knowledge_source_fact_elements.source_id "
            "AND s.notebook_id=%s AND s.source_type IN ('memory','knowhow'))",
            (notebook_id, notebook_id),
        )
        counts["knowledge_source_fact_elements"] = cur.rowcount
        cur = db.execute(
            "DELETE FROM knowledge_source_facts WHERE notebook_id=%s "
            "AND NOT EXISTS (SELECT 1 FROM sources s "
            "WHERE s.id=knowledge_source_facts.source_id AND s.notebook_id=%s "
            "AND s.source_type IN ('memory','knowhow'))",
            (notebook_id, notebook_id),
        )
        counts["knowledge_source_facts"] = cur.rowcount
        # T-5a codex #663 R1 P2: relations BEFORE objects — SQLite twin's
        # comment at the same spot has the full edges-before-nodes rationale.
        cur = db.execute(
            "DELETE FROM knowledge_relations WHERE notebook_id = %s "
            "AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=knowledge_relations.source_id "
            "AND s.notebook_id=%s AND s.source_type IN ('memory','knowhow'))",
            (notebook_id, notebook_id),
        )
        counts["knowledge_relations"] = cur.rowcount
        cur = db.execute(
            "DELETE FROM knowledge_objects WHERE notebook_id = %s "
            "AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=knowledge_objects.source_id "
            "AND s.notebook_id=%s AND s.source_type IN ('memory','knowhow'))",
            (notebook_id, notebook_id),
        )
        counts["knowledge_objects"] = cur.rowcount
        cur = db.execute(
            "DELETE FROM knowledge_object_sources WHERE notebook_id=%s "
            "AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
            "WHERE ko.notebook_id=%s AND ko.id=knowledge_object_sources.object_id)",
            (notebook_id, notebook_id),
        )
        counts["knowledge_object_sources"] = cur.rowcount
        for table in sorted(
            _GRAPH_RESET_TABLES - {"knowledge_embeddings", "extraction_runs"}
        ):
            cur = db.execute(
                sql.SQL("DELETE FROM {} WHERE notebook_id = %s").format(
                    sql.Identifier(table)
                ),
                (notebook_id,),
            )
            counts[table] = cur.rowcount
        cur = db.execute(
            "DELETE FROM knowledge_embeddings WHERE notebook_id=%s "
            "AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
            "WHERE ko.notebook_id=%s AND ko.id=knowledge_embeddings.object_id)",
            (notebook_id, notebook_id),
        )
        counts["knowledge_embeddings"] = cur.rowcount
        cur = db.execute(
            "DELETE FROM extraction_runs WHERE notebook_id=%s "
            "AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=extraction_runs.source_id "
            "AND s.notebook_id=%s AND s.source_type IN ('memory','knowhow'))",
            (notebook_id, notebook_id),
        )
        counts["extraction_runs"] = cur.rowcount
        # unified_kg_state: RESET in place to its birth-row shape, never
        # deleted (design doc batch-3-W1 Sec 3.3 option C, D-3). Dropping the
        # row (the pre-PR-2 behaviour) made a delete+reingest re-climb
        # kg_mutation_seq/cluster_mutation_seq/mention_seq from the same
        # starting values a prior version had already used — every process
        # cache keyed on that triple (or on version_signal, which embeds it)
        # could alias a stale entry onto genuinely different content
        # (kg_mutation.py's FULL CENSUS "Deliberately NOT moved" entry for
        # delete_notebook_kg; kg_analysis.py's _state_view docstring). This
        # reproduces the exact row shape a fresh create_notebook birth row
        # has EXCEPT it advances kg_reset_epoch instead of leaving it at 0 —
        # so kg_analysis._state_view's ``kg_mutation_seq==0 and not
        # last_rebuild_at`` "no KG history" judgment stays byte-identical
        # (the epoch column is not part of that judgment), while every
        # epoch-aware reader can now tell "notebook never had a KG" (epoch 0)
        # apart from "notebook's KG was reset N times" (epoch N).
        # kg_reset_epoch ONLY increases here — this is its one writer in the
        # whole codebase (see the column's migration comment and
        # kg_mutation.py's FULL CENSUS).
        # source_index_backfilled / chunk_elements_indexed /
        # indexing_pipeline_id / indexing_pipeline_version are DELIBERATELY
        # NOT touched on the UPDATE branch: they are provenance/pipeline-
        # identity columns, not KG content state, and the caller
        # (knowledge_lifecycle.delete_notebook_kg) separately re-asserts the
        # source_index_backfilled certificate when it was already set (its
        # own docstring covers why).
        #
        # R1 (P0-2, post-review): UPSERT, not a bare UPDATE. A plain UPDATE
        # WHERE notebook_id=%s is a silent no-op when the row does not exist
        # — reachable in production (scripts/merge_dbs.py's KG_STATE_TABLES
        # deliberately DELETEs a merged notebook's unified_kg_state row so
        # the deployment recomputes it fresh; nothing stops delete_notebook_
        # kg from later running against that same notebook before anything
        # else re-creates the row). A no-op reset leaves kg_reset_epoch
        # un-advanced AND the row still absent, so every epoch-aware reader's
        # "row is None" fallback keeps computing the SAME (epoch=0, seq=0)
        # version key both before and after this call — even though the
        # graph rows this same transaction just deleted were real content.
        # That is exactly the aliasing hazard kg_reset_epoch exists to close,
        # reappearing through the one path that skips the UPDATE entirely.
        # The INSERT branch therefore starts kg_reset_epoch at 1 (not 0): a
        # missing-row delete is itself a reset event, so its post-state must
        # be observably different from the "row never existed" sentinel the
        # missing row is currently returning everywhere. The other INSERT-
        # branch columns are left to their table DEFAULTs (source_index_
        # backfilled=0, chunk_elements_indexed=0, indexing_pipeline_id='',
        # indexing_pipeline_version='builtin.chunk.v1') — the conservative
        # "unknown/uncertified" shape a historical row predating forward
        # maintenance gets, deliberately NOT create_notebook's own
        # source_index_backfilled=1 (this row is being synthesized mid-
        # lifecycle for a notebook that already had content, not born empty).
        cur = db.execute(
            "INSERT INTO unified_kg_state ("
            "notebook_id, dirty, kg_mutation_seq, cluster_mutation_seq, "
            "cluster_input_version, last_rebuild_at, object_count, "
            "relation_count, cluster_count, community_seq, canonical_rel_seq, "
            "mention_seq, kg_reset_epoch, updated_at"
            ") VALUES (%s, 0, 0, 0, '', NULL, 0, 0, 0, -1, -1, -1, 1, %s) "
            "ON CONFLICT (notebook_id) DO UPDATE SET "
            "dirty=0, kg_mutation_seq=0, cluster_mutation_seq=0, "
            "cluster_input_version='', last_rebuild_at=NULL, "
            "object_count=0, relation_count=0, cluster_count=0, "
            "community_seq=-1, canonical_rel_seq=-1, mention_seq=-1, "
            "kg_reset_epoch=unified_kg_state.kg_reset_epoch+1, "
            "updated_at=excluded.updated_at",
            (notebook_id, normalize_timestamp(now)),
        )
        counts["unified_kg_state"] = cur.rowcount
        # PostgreSQL search indexes derive from knowledge_objects itself.
        counts["kg_objects_fts"] = 0
        return counts

    @staticmethod
    def notebook_tier_row(db: Any, notebook_id: str):
        return db.execute("SELECT tier FROM notebooks WHERE id=%s", (notebook_id,)).fetchone()

    @staticmethod
    def relink_rows(db: Any, notebook_id: str):
        """Whole-notebook relink input — REFERENCE ONLY, never on a live path.

        See the SQLite twin: three unbounded ``fetchall``s (objects with their
        payload+evidence documents, all relations, all source ids). Production
        relink pages by source (``relink_source_page`` +
        ``relink_orphan_source_ids``); this survives as the differential test's
        oracle for the historical whole-graph answer.
        """
        objects = db.execute(
            "SELECT id, object_type, source_id, payload, evidence FROM knowledge_objects "
            "WHERE notebook_id = %s AND status != 'deprecated'", (notebook_id,),
        ).fetchall()
        relations = db.execute(
            "SELECT source_object_id, target_object_id, edge_type "
            "FROM knowledge_relations WHERE notebook_id = %s", (notebook_id,),
        ).fetchall()
        valid_sources = {
            row["id"] for row in db.execute(
                "SELECT id FROM sources WHERE notebook_id = %s", (notebook_id,)
            ).fetchall()
        }
        return (
            _compat_rows(objects, payload=True, evidence=True),
            relations,
            valid_sources,
        )

    @staticmethod
    def relink_source_page(
        db: Any,
        notebook_id: str,
        after_created_at: Any,
        after_id: str,
        limit: int,
    ):
        """One ``(created_at, id)`` keyset page of the notebook's OWN sources.

        This is where the shape had to change. The obvious driver —
        ``SELECT DISTINCT source_id FROM knowledge_objects WHERE notebook_id=%s
        ORDER BY source_id`` — has no notebook-prefixed index to stand on here: the
        only index leading with ``source_id`` is
        ``idx_knowledge_objects_source_id``, which is ordered ACROSS notebooks, so
        each page walks a stretch of other notebooks' objects and filters them out.
        Measured on the shared base: the tail page removed ~1.2M neighbour rows,
        183 ms warm — and every page pays it again, on a cold instance long enough
        to trip the statement timeout. ``sources`` already has
        ``idx_sources_notebook_created``, so this is the repository's ordinary
        bounded source keyset (``s.id COLLATE "C"`` tie-break, exactly like
        ``source_build_state_page``) and every page stays notebook-local.

        Hidden synthetic sources (``memory`` / ``knowhow`` projections) are
        deliberately NOT excluded, unlike the build-target page — their rows own
        knowledge objects, and skipping them would drop those partitions.

        Pair with ``relink_orphan_source_ids``: objects have no foreign key to
        ``sources``, so partitions keyed by ``''`` or by a deleted source have no
        row here and must be discovered separately.
        """
        return db.execute(
            "SELECT id, created_at FROM sources WHERE notebook_id = %s "
            "AND (%s::timestamptz IS NULL "
            " OR (created_at, id COLLATE \"C\") > (%s, %s)) "
            "ORDER BY created_at, id COLLATE \"C\" LIMIT %s",
            (
                notebook_id,
                after_created_at,
                after_created_at,
                after_id,
                max(1, int(limit)),
            ),
        ).fetchall()

    @staticmethod
    def relink_orphan_source_ids(db: Any, notebook_id: str):
        """The object ``source_id`` values that name NO source of this notebook.

        ``''`` plus every id left behind by a deleted source. Run ONCE per relink
        pass, so that together with the ``sources`` keyset the two cover exactly the
        distinct ``source_id`` values present in ``knowledge_objects`` — nothing
        dropped, nothing visited twice.

        **Cost, stated plainly:** one bounded pass over THIS notebook's objects
        (``notebook_id`` equality, then an anti-join / correlated ``NOT EXISTS``
        against ``sources``). One scan per relink RUN, not per page — which is the
        whole point of moving the per-page work onto ``sources``. Acceptable for a
        background pass.

        The RESULT is bounded by deletions rather than by object count (one row per
        distinct orphan id), so no limit is imposed: a cap here could only silently
        drop partitions.
        """
        return db.execute(
            "SELECT DISTINCT ko.source_id AS source_id FROM knowledge_objects ko "
            "WHERE ko.notebook_id = %s AND (ko.source_id = '' OR NOT EXISTS("
            " SELECT 1 FROM sources s "
            " WHERE s.id = ko.source_id AND s.notebook_id = ko.notebook_id)) "
            "ORDER BY ko.source_id",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def relink_object_rows_for_source(db: Any, notebook_id: str, source_id: str):
        """Every non-deprecated object of ONE source, in insertion (ordinal) order.

        ``ordinal`` is the reviewed PostgreSQL counterpart of SQLite's ``rowid``
        (``POSTGRES_ROWID_ORDINAL_TABLES`` covers ``knowledge_objects``). The order
        is load-bearing: ``complete_isolated_edges`` walks isolated nodes in input
        order under a per-node edge cap. It is a DELIBERATE pin rather than a
        reproduction of the historical order — see the SQLite twin for why the old
        unordered query's de facto ``updated_at`` order was a planner accident and
        what the registered consequence is.
        """
        return _compat_rows(
            db.execute(
                "SELECT id, object_type, payload, evidence FROM knowledge_objects "
                "WHERE notebook_id = %s AND source_id = %s AND status != 'deprecated' "
                "ORDER BY ordinal",
                (notebook_id, source_id),
            ).fetchall(),
            payload=True,
            evidence=True,
        )

    @staticmethod
    def relink_relation_rows_for_objects(db: Any, notebook_id: str, object_ids):
        """Relations with EITHER endpoint among ``object_ids`` (caller batches).

        Split into two statements for parity with the SQLite twin, where the
        combined ``OR`` form provably drops the endpoint bound and scans every
        relation in the notebook. PostgreSQL would have coped — EXPLAIN ANALYZE
        shows it building a ``BitmapOr`` over both endpoint indexes — so the split
        costs it one extra round trip and buys one shape to reason about instead of
        two. Rows may repeat across the two statements; all consumers build sets.

        Cross-source edges MUST come back here — a node connected only by an edge
        that leaves the source would otherwise look isolated to its own partition.
        """
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("%s" for _ in ids)
        rows = list(db.execute(
            f"SELECT source_object_id, target_object_id, edge_type "
            f"FROM knowledge_relations WHERE notebook_id=%s "
            f"AND source_object_id IN ({ph})",
            (notebook_id, *ids),
        ).fetchall())
        rows.extend(db.execute(
            f"SELECT source_object_id, target_object_id, edge_type "
            f"FROM knowledge_relations WHERE notebook_id=%s "
            f"AND target_object_id IN ({ph})",
            (notebook_id, *ids),
        ).fetchall())
        return rows

    @staticmethod
    def relink_source_is_live(db: Any, notebook_id: str, source_id: str) -> bool:
        """Does this partition key still name a row of the notebook's sources?

        ``knowledge_relations.source_id`` has an FK to ``sources``; new relink rows
        must store NULL when the object's source is gone (or ''), exactly as the
        whole-notebook version did with its ``valid_sources`` set.
        """
        if not source_id:
            return False
        row = db.execute(
            "SELECT 1 FROM sources WHERE id=%s AND notebook_id=%s",
            (source_id, notebook_id),
        ).fetchone()
        return row is not None

    @staticmethod
    def incremental_object_rows(
        db: Any, notebook_id: str, source_id: str, object_type: str,
        *, exclude_source: bool = False,
    ):
        if object_type == "concept" and exclude_source:
            rows = db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=%s "
                "AND object_type='concept' AND status!='deprecated' AND source_id!=%s",
                (notebook_id, source_id),
            ).fetchall()
        elif object_type == "concept":
            rows = db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=%s AND source_id=%s "
                "AND object_type='concept' AND status!='deprecated'",
                (notebook_id, source_id),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=%s AND source_id=%s "
                "AND object_type=%s AND status!='deprecated'",
                (notebook_id, source_id, object_type),
            ).fetchall()
        return _compat_rows(rows, payload=True)

    @staticmethod
    def concept_embedding_rows(db: Any, notebook_id: str):
        """This notebook's LIVE **concept** object vectors (SQLite parity).

        See the SQLite adapter's docstring for the equivalence argument: the
        predicates copy incremental fusion's own ``incremental_object_rows(...,
        'concept')`` filter, so the keys its no-ANN bridge branch actually
        reads are unchanged while the claim/formula/procedure vectors it never
        read stop being fetched.
        """
        return db.execute(
            "SELECT e.object_id AS object_id, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN knowledge_objects o ON o.id = e.object_id "
            "WHERE e.notebook_id=%s AND o.object_type='concept' "
            "AND o.status!='deprecated'",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def embedding_rows_for_objects(db: Any, notebook_id: str, object_ids):
        ids = list(object_ids)
        if not ids:
            return []
        return db.execute(
            "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=%s "
            "AND object_id IN ({})".format(",".join("%s" for _ in ids)),
            (notebook_id, *ids),
        ).fetchall()

    @staticmethod
    def valid_object_ids(db: Any, object_ids):
        ids = list(object_ids)
        if not ids:
            return set()
        ph = ",".join("%s" for _ in ids)
        return {
            row["id"] for row in db.execute(
                f"SELECT id FROM knowledge_objects WHERE id IN ({ph}) AND status!='deprecated'",
                ids,
            ).fetchall()
        }

    @staticmethod
    def source_build_rows(db: Any, notebook_id: str):
        """Sources eligible for LLM KG extraction (build/rebuild) plus the
        subset that already has KG objects. Excludes hidden Memory/Knowhow
        projection sources: Memory owns its explicit confirmation-time KG
        lifecycle, while Knowhow objects are the projector's deterministic
        zero-LLM output, so a knowhow hidden source (whose KOs a rebuild wipe
        deliberately preserves — see ``delete_notebook_graph_rows``) must never
        be handed to the extraction pipeline as a target, and even a knowhow
        table not yet projected (elements present, zero KOs) must not become an
        empty-KG extraction target — either would fabricate LLM-derived
        case/procedure objects, violating the feature's zero-LLM invariant."""
        source_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM sources WHERE notebook_id = %s "
                "AND source_type NOT IN ('memory','knowhow')", (notebook_id,)
            ).fetchall()
        ]
        kg_source_ids = {
            row["source_id"] for row in db.execute(
                "SELECT DISTINCT ko.source_id FROM knowledge_objects ko "
                "WHERE ko.notebook_id = %s AND ko.source_id != '' "
                "AND COALESCE(("
                "  SELECT er.status FROM extraction_runs er "
                "  WHERE er.source_id=ko.source_id AND er.run_type='kg' "
                "  ORDER BY er.created_at DESC, er.ordinal DESC LIMIT 1"
                "), 'completed')='completed'",
                (notebook_id,),
            ).fetchall()
        }
        return source_ids, kg_source_ids

    @staticmethod
    def source_build_state_page(
        db: Any,
        notebook_id: str,
        after_created_at: Any,
        after_id: str,
        limit: int,
    ):
        """Bounded source state using the existing notebook/created index."""
        return db.execute(
            "SELECT s.id,s.created_at,"
            "EXISTS(SELECT 1 FROM source_elements e WHERE e.source_id=s.id) "
            "AS has_elements,"
            "EXISTS(SELECT 1 FROM knowledge_objects ko "
            " WHERE ko.notebook_id=%s AND ko.source_id=s.id AND ko.source_id<>'') "
            "AS has_graph,"
            "EXISTS(SELECT 1 FROM knowledge_objects ko "
            " WHERE ko.notebook_id=%s AND ko.source_id=s.id AND ko.source_id<>'' "
            " AND COALESCE((SELECT er.status FROM extraction_runs er "
            "  WHERE er.source_id=s.id AND er.run_type='kg' "
            "  ORDER BY er.created_at DESC,er.ordinal DESC LIMIT 1),'completed')"
            " ='completed') AS has_kg,"
            "COALESCE((SELECT er.error_message FROM extraction_runs er "
            " WHERE er.source_id=s.id AND er.run_type='kg' "
            " ORDER BY er.created_at DESC,er.ordinal DESC LIMIT 1),'') "
            "AS latest_kg_error,"
            # 见 SQLite 侧同名列的说明:与 latest_kg_error 配对的状态,供
            # models.sources.kg_analyzed_without_objects 判「已分析但零产出」。
            "COALESCE((SELECT er.status FROM extraction_runs er "
            " WHERE er.source_id=s.id AND er.run_type='kg' "
            " ORDER BY er.created_at DESC,er.ordinal DESC LIMIT 1),'') "
            "AS latest_kg_status "
            "FROM sources s WHERE s.notebook_id=%s "
            "AND (%s::timestamptz IS NULL "
            " OR (s.created_at,s.id COLLATE \"C\")>(%s,%s)) "
            "AND s.source_type NOT IN ('memory','knowhow') "
            "ORDER BY s.created_at,s.id COLLATE \"C\" LIMIT %s",
            (
                notebook_id,
                notebook_id,
                notebook_id,
                after_created_at,
                after_created_at,
                after_id,
                max(1, int(limit)),
            ),
        ).fetchall()

    @staticmethod
    def sources_with_elements(db: Any, notebook_id: str) -> set:
        """该 notebook 下已产出 source_elements(已成功 parse)的 source_id 集合。
        build_notebook_kg 用它把无 elements 的源(parse 未落地)排除出抽取 targets——
        否则接地校验(build_records)没有 element 可绑,抽出的节点被整源丢弃、objects=0。

        问的是「哪些来源有元素」,答案的规模是来源数;所以从 sources 驱动、每个来源
        用 EXISTS 探一次索引即可,那次探测命中第一行就停。等价的 DISTINCT-over-JOIN
        写法要先把该 notebook 的**每一行元素**都取出来再去重,代价随元素数增长——
        千万级元素的库为了得到几千个 id 扫全部元素行。集合本身逐字相同。"""
        return {
            row["source_id"] for row in db.execute(
                "SELECT s.id AS source_id FROM sources s WHERE s.notebook_id = %s "
                "AND EXISTS (SELECT 1 FROM source_elements e WHERE e.source_id = s.id)",
                (notebook_id,),
            ).fetchall()
        }

    @staticmethod
    def active_object_count(db: Any, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects "
            "WHERE notebook_id=%s AND status!='deprecated'", (notebook_id,),
        ).fetchone()["c"])

    @staticmethod
    def unified_graph_rows(db: Any, notebook_id: str):
        return _compat_rows(db.execute(
            "SELECT id, object_type, payload, status FROM knowledge_objects "
            "WHERE notebook_id=%s AND status!='deprecated' ORDER BY ordinal",
            (notebook_id,),
        ).fetchall(), payload=True)

    @staticmethod
    def neighbor_relation_rows(db: Any, notebook_id: str, object_ids):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("%s" for _ in ids)
        return db.execute(
            f"SELECT source_object_id, target_object_id, edge_type FROM knowledge_relations "
            f"WHERE notebook_id=%s "
            f"AND (source_object_id IN ({ph}) OR target_object_id IN ({ph}))",
            (notebook_id, *ids, *ids),
        ).fetchall()

    @staticmethod
    def object_meta_rows_for_notebook(
        db: Any, notebook_id: str, object_ids,
    ):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("%s" for _ in ids)
        return _compat_rows(db.execute(
            f"SELECT id, object_type, payload FROM knowledge_objects "
            f"WHERE notebook_id=%s AND id IN ({ph})", (notebook_id, *ids),
        ).fetchall(), payload=True)

    @staticmethod
    def community_context_rows(db: Any, notebook_id: str, members):
        ids = list(members)
        if not ids:
            return [], []
        ph = ",".join("%s" for _ in ids)
        objects = db.execute(
            f"SELECT id, object_type, payload FROM knowledge_objects WHERE id IN ({ph})", ids,
        ).fetchall()
        relations = db.execute(
            f"SELECT source_object_id, target_object_id, edge_type FROM knowledge_relations "
            f"WHERE notebook_id=%s AND review_status!='rejected' "
            f"AND source_object_id IN ({ph}) AND target_object_id IN ({ph}) "
            f"ORDER BY id COLLATE \"C\"",
            [notebook_id, *ids, *ids],
        ).fetchall()
        return _compat_rows(objects, payload=True), relations

    def get_notebook(self, notebook_id: str) -> None:
        with self.database.connect() as db:
            if db.execute("SELECT 1 FROM notebooks WHERE id=%s", (notebook_id,)).fetchone() is None:
                raise KeyError(notebook_id)

    def has_kg(self, notebook_id: str) -> bool:
        with self.database.connect() as db:
            return bool(db.execute(
                "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id=%s)",
                (notebook_id,),
            ).fetchone()["exists"])

    @staticmethod
    def any_mounted_has_kg_on(db: Any, notebook_id: str) -> bool:
        """本库挂载的参考库中是否有任一已建 KG —— 驱动前端严格推理门控。
        未挂载 → False(即便系统里存在有图的公共知识库)。"""
        return bool(db.execute(
            "SELECT EXISTS(SELECT 1 " + MOUNT_JOIN + MOUNT_VALID
            + " AND EXISTS(SELECT 1 FROM knowledge_objects ko WHERE ko.notebook_id = b.id)) AS exists",
            (notebook_id,),
        ).fetchone()["exists"])

    def any_mounted_has_kg(self, notebook_id: str) -> bool:
        with self.database.connect() as db:
            return self.any_mounted_has_kg_on(db, notebook_id)

    def any_mounted_has_kg_compat(
        self, notebook_id: str, db: "Any | None" = None
    ) -> bool:
        return (
            self.any_mounted_has_kg_on(db, notebook_id) if db is not None
            else self.any_mounted_has_kg(notebook_id)
        )

    def retrieval_objects_compat(
        self, db: Any, notebook_id: str, object_type: str,
        statuses, id_filter,
    ) -> list[dict]:
        if id_filter is not None:
            id_filter = list(dict.fromkeys(id_filter))
        return self.retrieval_objects(
            db,
            notebook_id,
            object_type,
            statuses,
            id_filter,
            batch_size=self.seams.in_chunk_size(),
        )

    def begin_extraction(
        self,
        source_id: str,
        notebook_id: str,
        run_id: str,
        created_at: str,
        *,
        preserve_existing: bool = False,
        indexing_pipeline_id: str = "",
        indexing_pipeline_version: str = "builtin.chunk.v1",
    ) -> None:
        with self.database.write() as db:
            self.begin_extraction_run(
                db,
                source_id,
                notebook_id,
                run_id,
                created_at,
                preserve_existing=preserve_existing,
                indexing_pipeline_id=indexing_pipeline_id,
                indexing_pipeline_version=indexing_pipeline_version,
            )

    def finish_extraction(self, run_id: str, status: str, message: str) -> None:
        notebook_id = ""
        with self.database.write() as db:
            row = db.execute(
                "SELECT notebook_id FROM extraction_runs WHERE id=%s",
                (run_id,),
            ).fetchone()
            self.finish_extraction_run(
                db, run_id, status, message, self.seams.now()
            )
            if row is not None:
                notebook_id = row["notebook_id"]
        if notebook_id:
            # pending_source_count depends on the latest run status, while its
            # version key is the KG mutation sequence. A status-only terminal
            # update therefore needs an explicit post-commit invalidation.
            # PostgreSQL queries do not use SQLite's process-local count memo.
            pass

    def add_relations_current(
        self, notebook_id: str, source_id: str, relations: List[dict]
    ) -> int:
        with self.database.write() as db:
            return self.add_relations(
                db, notebook_id, source_id, relations, self.seams.now()
            )

    @staticmethod
    def object_version_row(db: Any, notebook_id: str):
        row = db.execute(
            "SELECT COUNT(*) AS c, MAX(updated_at) AS ts "
            "FROM knowledge_objects WHERE notebook_id = %s", (notebook_id,),
        ).fetchone()
        row = dict(row)
        row["ts"] = iso_timestamp(row["ts"])
        return row

    @staticmethod
    def relation_context_rows(db: Any, notebook_id: str,
                              relation_ids=None, *, batch_size: int = 900):
        base_sql = (
            "SELECT r.id AS id, r.source_object_id AS s, r.target_object_id AS t, "
            "r.edge_type AS et, r.evidence AS ev, r.review_status AS review_status, "
            "so.payload AS sp, tp.payload AS tpl, "
            "so.object_type AS st, tp.object_type AS tt "
            "FROM knowledge_relations r "
            "JOIN knowledge_objects so ON so.id = r.source_object_id "
            "JOIN knowledge_objects tp ON tp.id = r.target_object_id "
            "WHERE r.notebook_id = %s"
        )
        if relation_ids is None:
            rows = db.execute(base_sql, (notebook_id,)).fetchall()
            return KnowledgeStore._compat_relation_context(rows)
        ids = list(relation_ids)
        rows = []
        for offset in range(0, len(ids), batch_size):
            batch = ids[offset:offset + batch_size]
            ph = ",".join("%s" for _ in batch)
            rows.extend(db.execute(
                base_sql + f" AND r.id IN ({ph})", (notebook_id, *batch),
            ).fetchall())
        return KnowledgeStore._compat_relation_context(rows)

    @staticmethod
    def relation_id_rows_for_objects(
        db: Any,
        notebook_id: str,
        object_ids,
        limit: int,
        *,
        batch_size: int = 900,
    ):
        """PostgreSQL parity for bounded lexical-endpoint relation recall."""
        values = list(dict.fromkeys(object_ids))
        remaining = max(0, int(limit))
        if not values or not remaining:
            return []
        rows = []
        seen: set[str] = set()
        streams = [
            (endpoint, object_id)
            for object_id in values
            for endpoint in ("source_object_id", "target_object_id")
        ]
        cursors = {stream_index: "" for stream_index in range(len(streams))}
        active = list(range(len(streams)))
        stream_batch_size = max(1, min(int(batch_size), 200))
        while remaining > 0 and active:
            page_size = max(1, (remaining + len(active) - 1) // len(active))
            counts = {stream_index: 0 for stream_index in active}
            last_ids: dict[int, str] = {}
            for batch_offset in range(0, len(active), stream_batch_size):
                batch = active[batch_offset:batch_offset + stream_batch_size]
                branches = []
                params = []
                for stream_index in batch:
                    endpoint, object_id = streams[stream_index]
                    branches.append(
                        f"SELECT id,{stream_index} AS stream_index FROM ("
                        f"SELECT id FROM knowledge_relations "
                        f"WHERE notebook_id=%s AND review_status!='rejected' "
                        f"AND {endpoint}=%s AND id>%s ORDER BY id LIMIT %s"
                        f") AS relation_stream_{stream_index}"
                    )
                    params.extend((
                        notebook_id,
                        object_id,
                        cursors[stream_index],
                        page_size,
                    ))
                found = db.execute(" UNION ALL ".join(branches), params).fetchall()
                for row in sorted(found, key=lambda item: item["stream_index"]):
                    stream_index = row["stream_index"]
                    counts[stream_index] += 1
                    last_ids[stream_index] = max(
                        last_ids.get(stream_index, ""), row["id"]
                    )
                    if row["id"] in seen:
                        continue
                    seen.add(row["id"])
                    rows.append(row)
                    remaining -= 1
                    if remaining <= 0:
                        return rows
            next_active = []
            for stream_index in active:
                fetched = counts[stream_index]
                if stream_index in last_ids:
                    cursors[stream_index] = last_ids[stream_index]
                if fetched == page_size:
                    next_active.append(stream_index)
            active = next_active
        return rows

    @staticmethod
    def _compat_relation_context(rows):
        output = []
        for row in rows:
            item = dict(row)
            item["ev"] = _json_text(item.get("ev"), [])
            item["sp"] = _json_text(item.get("sp"), {})
            item["tpl"] = _json_text(item.get("tpl"), {})
            output.append(item)
        return output

    @staticmethod
    def relation_exists(db: Any, notebook_id: str) -> bool:
        return db.execute(
            "SELECT 1 FROM knowledge_relations WHERE notebook_id = %s LIMIT 1",
            (notebook_id,),
        ).fetchone() is not None

    @staticmethod
    def relation_endpoint_rows(db: Any, notebook_id: str,
                               source_ids=None):
        if source_ids:
            values = list(source_ids)
            ph = ",".join("%s" for _ in values)
            return db.execute(
                f"SELECT source_object_id, target_object_id FROM knowledge_relations "
                f"WHERE notebook_id=%s AND source_id IN ({ph})",
                (notebook_id, *values),
            ).fetchall()
        return db.execute(
            "SELECT source_object_id, target_object_id FROM knowledge_relations "
            "WHERE notebook_id = %s", (notebook_id,),
        ).fetchall()

    @staticmethod
    def relation_connected_object_ids(db: Any, notebook_id: str, object_ids):
        """Return only candidates that have at least one incident relation.

        Each correlated EXISTS can stop at the first indexed edge, keeping the
        result and database work bounded by the candidate window rather than a
        high-degree node's complete adjacency list.
        """
        values = list(dict.fromkeys(object_ids))
        if not values:
            return []
        candidates = ",".join("(%s)" for _ in values)
        return db.execute(
            f"WITH candidates(object_id) AS (VALUES {candidates}) "
            "SELECT object_id FROM candidates AS c "
            "WHERE EXISTS ("
            "SELECT 1 FROM knowledge_relations AS r "
            "WHERE r.notebook_id=%s AND r.source_object_id=c.object_id LIMIT 1"
            ") OR EXISTS ("
            "SELECT 1 FROM knowledge_relations AS r "
            "WHERE r.notebook_id=%s AND r.target_object_id=c.object_id LIMIT 1"
            ")",
            (*values, notebook_id, notebook_id),
        ).fetchall()

    @staticmethod
    def neighbor_ids(db: Any, notebook_id: str, object_id: str,
                     *, endpoint: str, edge_type=None, limit: int | None = None,
                     usable_statuses: Sequence[str] | None = None):
        # `limit` 非 None 时按 `r.id` 定序取前 N 行:排序键是
        # `idx_knowledge_relations_nb_source_id`/`_nb_target_id`
        # `(notebook_id, source/target_object_id, id)` 的第三列,前两列都是等值
        # 谓词,索引本身即给出该序。limit=None 逐位保持历史行为。
        #
        # `usable_statuses` 非空时对**邻居那一侧**的别名加 status 谓词,且必须落在
        # LIMIT **之前**(理由与 SQLite 侧同:事后过滤等于让不可用对象白占有界读取
        # 窗口)。两个后端必须给出同一组语义。None/空 → SQL 逐字回到历史形状。
        if endpoint not in {"source_object_id", "target_object_id"}:
            raise ValueError("invalid relation endpoint")
        statement = sql.SQL(
            "SELECT r.source_object_id,r.target_object_id,r.edge_type,"
            "src.object_type AS source_type,tgt.object_type AS target_type "
            "FROM knowledge_relations AS r "
            "JOIN knowledge_objects AS src ON src.id=r.source_object_id "
            "JOIN knowledge_objects AS tgt ON tgt.id=r.target_object_id "
            "WHERE r.notebook_id=%s AND r.{}=%s AND r.review_status!='rejected'"
        ).format(sql.Identifier(endpoint))
        if edge_type:
            statement += sql.SQL(" AND edge_type=%s")
        params = [notebook_id, object_id] + ([edge_type] if edge_type else [])
        if usable_statuses:
            # endpoint 指的是**锚点**那一端,邻居在另一端。
            neighbour_alias = "tgt" if endpoint == "source_object_id" else "src"
            statuses = list(usable_statuses)
            statement += sql.SQL(" AND {}.status IN ({})").format(
                sql.Identifier(neighbour_alias),
                sql.SQL(",").join(sql.Placeholder() for _ in statuses),
            )
            params.extend(statuses)
        if limit is not None:
            statement += sql.SQL(" ORDER BY r.id LIMIT %s")
            params.append(int(limit))
        return db.execute(statement, params).fetchall()

    def usable_object_rows(self, notebook_id: str, object_ids: Sequence[str]):
        with self.database.connect() as db:
            rows = self.usable_object_rows_on(
                db, object_ids, ("reviewed", "approved", "project_specific", "conflict"),
            )
        return [dict(row) for row in rows if row["notebook_id"] == notebook_id]

    @staticmethod
    def usable_object_rows_on(db: Any, object_ids, statuses,
                              *, batch_size: int = 500):
        # 与 SQLite 侧同一分片口径(两个后端的行为必须同义)。PostgreSQL 的参数
        # 上限高得多,但一条 20 万参数的 IN 同样不是这条路该发的语句。
        ids = list(object_ids)
        if not ids:
            return []
        status_ph = ",".join("%s" for _ in statuses)
        out: list = []
        for offset in range(0, len(ids), batch_size):
            batch = ids[offset:offset + batch_size]
            ph = ",".join("%s" for _ in batch)
            rows = db.execute(
                f"SELECT * FROM knowledge_objects WHERE id IN ({ph}) "
                f"AND status IN ({status_ph})", [*batch, *statuses],
            ).fetchall()
            out.extend(_compat_rows(rows, payload=True, evidence=True))
        return out

    @staticmethod
    def graph_version_rows(db: Any, notebook_id: str):
        rel = db.execute(
            "SELECT COUNT(*) AS c, MAX(created_at) AS ts, "
            "COALESCE(SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END), 0) AS n_rej, "
            "COALESCE(SUM(CASE WHEN review_status = 'verified' THEN 1 ELSE 0 END), 0) AS n_ver "
            "FROM knowledge_relations WHERE notebook_id = %s", (notebook_id,),
        ).fetchone()
        obj = db.execute(
            "SELECT COUNT(*) AS c, MAX(updated_at) AS ts "
            "FROM knowledge_objects WHERE notebook_id = %s", (notebook_id,),
        ).fetchone()
        rel, obj = dict(rel), dict(obj)
        rel["ts"] = iso_timestamp(rel["ts"])
        obj["ts"] = iso_timestamp(obj["ts"])
        return rel, obj

    @staticmethod
    def graph_object_rows(db: Any, notebook_id: str, statuses):
        ph = ",".join("%s" for _ in statuses)
        return _compat_rows(db.execute(
            "SELECT id, object_type, payload FROM knowledge_objects "
            f"WHERE notebook_id = %s AND status IN ({ph}) "
            "ORDER BY ordinal, id COLLATE \"C\"",
            (notebook_id, *statuses),
        ).fetchall(), payload=True)

    @staticmethod
    def graph_relation_rows(db: Any, notebook_id: str,
                            *, include_id_evidence: bool = True):
        statement = (
            "SELECT id, source_object_id, target_object_id, edge_type, evidence "
            "FROM knowledge_relations "
            "WHERE notebook_id = %s AND review_status != 'rejected' "
            "ORDER BY id COLLATE \"C\""
            if include_id_evidence else
            "SELECT source_object_id, target_object_id FROM knowledge_relations "
            "WHERE notebook_id = %s AND review_status != 'rejected' "
            "ORDER BY id COLLATE \"C\""
        )
        return _compat_rows(db.execute(
            statement,
            (notebook_id,),
        ).fetchall(), evidence=include_id_evidence)

    @staticmethod
    def object_evidence_rows(db: Any, object_ids):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("%s" for _ in ids)
        return _compat_rows(db.execute(
            f"SELECT id, evidence FROM knowledge_objects WHERE id IN ({ph})", ids,
        ).fetchall(), evidence=True)

    @staticmethod
    def notebook_object_evidence_rows(db: Any, notebook_id: str):
        return _compat_rows(db.execute(
            "SELECT id, evidence FROM knowledge_objects WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchall(), evidence=True)

    @staticmethod
    def follow_start_row(db: Any, object_id: str,
                         active_notebook_id: str, statuses):
        """起点授权门:只有 active 自己的对象、或 active 挂载的参考库里的对象,
        才能作为 follow_chain 的合法起点(未挂载的 tier='base' 库不算,即便它已发布)。"""
        ph = ",".join("%s" for _ in statuses)
        row = db.execute(
            f"SELECT ko.*, n.tier AS notebook_tier "
            f"FROM knowledge_objects ko JOIN notebooks n ON n.id=ko.notebook_id "
            f"WHERE ko.id=%s AND ko.status IN ({ph}) "
            "AND (ko.notebook_id=%s OR ko.notebook_id IN ("
            + MOUNTED_BASE_IDS_SUBQUERY + "))",
            (object_id, *statuses, active_notebook_id, active_notebook_id),
        ).fetchone()
        if row is None:
            return None
        return _compat_rows([row], payload=True, evidence=True)[0]

    @staticmethod
    def follow_endpoint_rows(db: Any, notebook_id: str, object_id: str,
                             endpoint: str, limit: int):
        if endpoint not in {"source_object_id", "target_object_id"}:
            raise ValueError("invalid relation endpoint")
        statement = sql.SQL(
            "SELECT r.id,r.notebook_id,r.source_id,r.source_object_id,"
            "r.target_object_id,r.edge_type,r.review_status "
            "FROM knowledge_relations AS r "
            "WHERE r.notebook_id=%s AND r.{}=%s "
            "ORDER BY CASE r.review_status "
            "WHEN 'verified' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, "
            "r.id COLLATE \"C\" LIMIT %s"
        ).format(sql.Identifier(endpoint))
        return db.execute(
            statement,
            (notebook_id, object_id, limit),
        ).fetchall()

    @staticmethod
    def follow_relation_evidence_rows(db: Any, relation_ids):
        ids = list(relation_ids)
        if not ids:
            return []
        ph = ",".join("%s" for _ in ids)
        rows = db.execute(
            f"SELECT r.id, r.evidence, s.title AS source_title "
            f"FROM knowledge_relations r LEFT JOIN sources s ON s.id=r.source_id "
            f"WHERE r.id IN ({ph})", tuple(ids),
        ).fetchall()
        return _compat_rows(rows, evidence=True)

    @staticmethod
    def follow_object_rows(db: Any, notebook_id: str,
                           object_ids, statuses):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("%s" for _ in ids)
        status_ph = ",".join("%s" for _ in statuses)
        rows = db.execute(
            f"SELECT * FROM knowledge_objects WHERE notebook_id=%s "
            f"AND id IN ({ph}) AND status IN ({status_ph})",
            (notebook_id, *ids, *statuses),
        ).fetchall()
        return _compat_rows(rows, payload=True, evidence=True)

    @staticmethod
    def in_network_relation_rows(db: Any, notebook_id: str,
                                 object_ids):
        """SQLite 侧同名方法的 parity 实现;T2(批 1 热点整改)的 ``DISTINCT``/
        ``ORDER BY`` 理由见那侧 docstring——两侧逐字同一套语义。"""
        ids = list(object_ids)
        if len(ids) < 2:
            return []
        ph = ",".join("%s" for _ in ids)
        return db.execute(
            f"SELECT DISTINCT r.source_object_id, r.target_object_id, r.edge_type, "
            f"src.object_type AS source_type, tgt.object_type AS target_type "
            f"FROM knowledge_relations AS r "
            f"JOIN knowledge_objects AS src ON src.id=r.source_object_id "
            f"JOIN knowledge_objects AS tgt ON tgt.id=r.target_object_id "
            f"WHERE r.notebook_id=%s AND r.review_status!='rejected' "
            f"AND r.source_object_id IN ({ph}) "
            f"AND r.target_object_id IN ({ph}) "
            f"ORDER BY r.source_object_id, r.edge_type, r.target_object_id",
            [notebook_id, *ids, *ids],
        ).fetchall()

    @staticmethod
    def retrieval_objects(
        db: Any,
        notebook_id: str,
        object_type: str,
        statuses: Optional[Iterable[str]],
        id_filter: Optional[Iterable[str]],
        *,
        batch_size: int = 900,
    ) -> List[dict]:
        base_query = "SELECT * FROM knowledge_objects WHERE notebook_id=%s AND object_type=%s"
        params: List[object] = [notebook_id, object_type]
        if statuses is not None:
            values = list(statuses)
            base_query += f" AND status IN ({','.join('%s' for _ in values)})"
            params.extend(values)
        if id_filter is not None:
            ids = list(id_filter)
            if not ids:
                return []
            rows = []
            batch_size = max(1, int(batch_size))
            for offset in range(0, len(ids), batch_size):
                batch = ids[offset:offset + batch_size]
                rows.extend(db.execute(
                    base_query + f" AND id IN ({','.join('%s' for _ in batch)})",
                    (*params, *batch),
                ).fetchall())
            rows.sort(key=lambda row: (row["created_at"], row["id"]))
        else:
            rows = db.execute(base_query + " ORDER BY created_at ASC, id ASC", params).fetchall()
        return [{
            "id": row["id"],
            "payload": json_value(row["payload"], {}),
            "evidence": _retrieval_evidence(row["evidence"]),
            "status": row["status"],
            "owner": row["owner"],
            "last_reviewed": iso_timestamp(row["last_reviewed"])
            if "last_reviewed" in row.keys() else "",
        } for row in rows]

    @staticmethod
    def duplicate_seed_rows(
        db: Any, notebook_id: str, object_type: str,
    ) -> List[dict]:
        """R3 T-B1 (KG-3) dedup pass 1 -- see the ``KnowledgeStorePort``
        docstring for the shared rationale (evidence-free thin BLOCKING
        projection, ``status`` pushdown, ORDER BY parity with
        ``retrieval_objects``).

        ``payload->>'name'`` returns SQL NULL for a missing key, a JSON
        ``null``, or a non-object payload -- ``find_duplicates`` applies the
        SAME ``str(name) if name is not None else ""`` coercion the
        review-queue endpoint read uses (``KnowledgeGovernanceService.
        review_queue``), so a string/integer name renders identically on both
        backends; see that call site's docstring for the registered
        (pre-existing, non-regressing) cross-dialect divergence on other JSON
        scalar shapes."""
        if object_type == "procedure":
            rows = db.execute(
                "SELECT id, status, payload FROM knowledge_objects "
                "WHERE notebook_id=%s AND object_type=%s AND status != 'deprecated' "
                "ORDER BY created_at ASC, id ASC",
                (notebook_id, object_type),
            ).fetchall()
            return [{
                "id": row["id"],
                "status": row["status"],
                "payload": json_value(row["payload"], {}),
            } for row in rows]
        rows = db.execute(
            "SELECT id, status, payload->>'name' AS name FROM knowledge_objects "
            "WHERE notebook_id=%s AND object_type=%s AND status != 'deprecated' "
            "ORDER BY created_at ASC, id ASC",
            (notebook_id, object_type),
        ).fetchall()
        return [{
            "id": row["id"],
            "status": row["status"],
            "name": row["name"],
        } for row in rows]

    @staticmethod
    def duplicate_member_rows(
        db: Any, notebook_id: str, object_ids: Sequence[str], *, batch_size: int = 900,
    ) -> List[dict]:
        """R3 T-B1 (KG-3) dedup pass 2 -- see ``KnowledgeStorePort`` for the
        shared rationale (no ``evidence`` column; unspecified row order).

        ``notebook_id`` stays in the predicate alongside ``id = ANY(...)`` --
        unlike the SQLite twin below, PostgreSQL plans this as a primary-key
        scan with ``notebook_id`` evaluated as a filter, not a per-batch
        notebook-wide scan (same conclusion as ``GovernanceStore.
        review_queue_rows``'s endpoint lookup)."""
        ids = list(dict.fromkeys(object_ids))
        rows: List[Any] = []
        for offset in range(0, len(ids), batch_size):
            batch = ids[offset:offset + batch_size]
            rows.extend(db.execute(
                "SELECT id, payload FROM knowledge_objects "
                "WHERE notebook_id=%s AND id = ANY(%s)",
                (notebook_id, batch),
            ).fetchall())
        return [{
            "id": row["id"],
            "payload": json_value(row["payload"], {}),
        } for row in rows]

    def _element_texts(self, db, element_ids, *, with_ordinal: bool = False):
        ids = [e for e in element_ids if e]
        if not ids:
            return {}, {}
        ph = ",".join("%s" for _ in ids)
        rows = db.execute(f"SELECT id, text FROM source_elements WHERE id IN ({ph})", ids).fetchall()
        texts = {r["id"]: r["text"] for r in rows}
        if not with_ordinal:
            return texts, {}
        order_rows = db.execute(
            "SELECT se.id FROM source_elements se JOIN sources s ON se.source_id=s.id "
            "WHERE s.notebook_id=(SELECT notebook_id FROM sources WHERE id=("
            "SELECT source_id FROM source_elements WHERE id=%s LIMIT 1)) "
            "ORDER BY se.created_at ASC, se.id ASC",
            (ids[0],),
        ).fetchall()
        ordinal = {r["id"]: i for i, r in enumerate(order_rows)}
        return texts, ordinal
    def _enrich_evidence(self, db, evidence):
        element_ids = list(
            dict.fromkeys(e.get("element_id") for e in evidence if e.get("element_id"))
        )
        details = {}
        if element_ids:
            ph = ",".join("%s" for _ in element_ids)
            rows = db.execute(
                f"SELECT id, source_id, element_type, location_label, text "
                f"FROM source_elements WHERE id IN ({ph})",
                element_ids,
            ).fetchall()
            details = {row["id"]: row for row in rows}
        out = []
        for e in evidence:
            enriched = dict(e)
            detail = details.get(e.get("element_id", ""))
            if detail is not None:
                enriched.update({
                    "source_id": detail["source_id"],
                    "element_id": detail["id"],
                    "element_type": detail["element_type"],
                    "location_label": detail["location_label"],
                    "element_text": detail["text"],
                })
            else:
                enriched["element_text"] = e.get("quoted_span", "")
            enriched["quoted_span"] = e.get("quoted_span", "")
            enriched["source_title"] = e.get("source_title", "") or enriched.get("source_id", "")
            out.append(enriched)
        return out
    def node_context(self, notebook_id, object_id, *, check_access: bool = True):
        if check_access:
            self.get_notebook(notebook_id)
        with self._connect() as db:
            row = db.execute("SELECT id, object_type, payload, evidence FROM knowledge_objects WHERE id=%s AND notebook_id=%s", (object_id, notebook_id)).fetchone()
            if row is None:
                raise KeyError(object_id)
            obj_type = row["object_type"]
            payload = json_value(row["payload"], {})
            section = payload.get("section_path", "")
            occurrences = self._enrich_evidence(db, json_value(row["evidence"], []))
            result = {"id": object_id, "object_type": obj_type, "name": payload.get("name", ""),
                      "section_path": section, "occurrences": occurrences, "definition": None, "steps": None}
            if obj_type == "concept":
                # prefer the unified cluster's fused description when present
                crow = db.execute(
                    "SELECT canonical_description FROM concept_clusters "
                    "WHERE notebook_id=%s AND member_object_id=%s AND canonical_description!='' LIMIT 1",
                    (notebook_id, object_id)).fetchone()
                if crow and crow["canonical_description"]:
                    result["definition"] = crow["canonical_description"]
                else:
                    drow = db.execute(
                        "SELECT ko.payload, ko.evidence FROM knowledge_relations r JOIN knowledge_objects ko ON ko.id=r.source_object_id "
                        "WHERE r.notebook_id=%s AND r.target_object_id=%s AND r.edge_type='defines' LIMIT 1", (notebook_id, object_id)).fetchone()
                    if drow is not None:
                        dpay = json_value(drow["payload"], {})
                        den = self._enrich_evidence(db, json_value(drow["evidence"], []))
                        result["definition"] = (den[0]["element_text"] if den else dpay.get("name", ""))
            if obj_type == "procedure":
                steps_payload = payload.get("steps")
                if isinstance(steps_payload, list) and steps_payload:
                    # New self-contained shape: ordered steps live in the object's payload.
                    eids = [s.get("element_id") for s in steps_payload if s.get("element_id")]
                    texts, _ord = self._element_texts(db, eids) if eids else ({}, {})
                    result["steps"] = [
                        {"name": s.get("name", ""),
                         "element_text": texts.get(s.get("element_id") or "", s.get("quote", "")),
                         "section_path": section}
                        for s in steps_payload
                    ]
                else:
                    # Legacy fallback: group sibling procedure nodes by exact section_path
                    # (precedes edges are sparse). Two distinct procedures sharing a heading
                    # would merge — acceptable for inspection.
                    #
                    # P2-3: this used to scan EVERY procedure object in the notebook
                    # (regardless of section) and filter in Python — O(procedures in
                    # notebook) per call. When the target node's own section_path is
                    # known (the common case — payload.get("section_path") above),
                    # bind the query to it directly in SQL via json_extract (JSON1,
                    # already used elsewhere in this file), so SQLite only reads
                    # matching rows. section_path is free text (not a dedicated
                    # column) so this is the only way to push the filter down without
                    # a schema change. If section_path is unavailable (rare: an old
                    # or malformed payload), fall back to a bounded LIMIT — this path
                    # is a display-only legacy fallback, not a correctness-critical
                    # query, so an arbitrary-but-bounded sample is acceptable.
                    if section:
                        prows = db.execute(
                            "SELECT id, payload, evidence FROM knowledge_objects "
                            "WHERE notebook_id=%s AND object_type='procedure' AND status!='deprecated' "
                            "AND (payload ->> 'section_path') COLLATE \"C\"=%s",
                            (notebook_id, section)).fetchall()
                    else:
                        prows = db.execute(
                            "SELECT id, payload, evidence FROM knowledge_objects "
                            "WHERE notebook_id=%s AND object_type='procedure' AND status!='deprecated' "
                            "LIMIT 500",
                            (notebook_id,)).fetchall()
                    candidate_steps = []
                    for pr in prows:
                        ppay = json_value(pr["payload"], {})
                        if ppay.get("section_path", "") != section:
                            continue
                        ev = json_value(pr["evidence"], [])
                        first_eid = ev[0].get("element_id") if ev else ""
                        candidate_steps.append((ppay.get("name", ""), first_eid))
                    all_step_first_eids = [eid for _, eid in candidate_steps if eid]
                    if all_step_first_eids:
                        texts, ordinal = self._element_texts(db, all_step_first_eids, with_ordinal=True)
                    else:
                        texts, ordinal = {}, {}
                    steps = []
                    for step_name, first_eid in candidate_steps:
                        steps.append({"name": step_name, "element_text": texts.get(first_eid, ""),
                                      "section_path": section, "_ord": ordinal.get(first_eid, 1_000_000)})
                    steps.sort(key=lambda s: s["_ord"])
                    for s in steps:
                        s.pop("_ord", None)
                    result["steps"] = steps
            return result

    # ------------------------------------------------------------- counts
    @staticmethod
    def count_knowledge(
        db: Any, notebook_id: str, object_type: str, statuses
    ) -> int:
        placeholders = ",".join("%s" for _ in statuses)
        row = db.execute(
            f"SELECT COUNT(*) AS count FROM knowledge_objects "
            f"WHERE notebook_id = %s AND object_type = %s AND status IN ({placeholders})",
            (notebook_id, object_type, *statuses),
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def count_active_objects(db: Any, notebook_id: str) -> int:
        from app.repositories.postgres import knowledge_counts_cache
        return knowledge_counts_cache.active_object_count(db, notebook_id)

    @staticmethod
    def type_counts(
        db: Any, notebook_id: str
    ) -> "tuple[Dict[str, int], Dict[str, str]]":
        from app.repositories.postgres import knowledge_counts_cache
        counts = knowledge_counts_cache.type_counts(db, notebook_id)  # non-deprecated
        # Labels are resolved by SchemaRegistryService so global + notebook
        # overlay semantics have one implementation rather than dialect SQL
        # duplicated in both knowledge stores.
        return counts, {}

    # --------------------------------------------------------------- list
    @staticmethod
    def knowledge_object_page_rows(
        db: Any,
        notebook_id: str,
        object_type: str,
        after: tuple[object, str] | None,
        limit: int,
    ) -> "List[Any]":
        """Backend twin of the SQLite keyset page (see it for why keyset and
        not OFFSET, and why the usable-status predicate is the caller's).

        ``created_at`` is ``timestamptz`` here, so the cursor value travels
        back as the ``datetime`` this adapter returned — never re-rendered
        text.  Row values keep the comparison index-comparable on
        ``idx_knowledge_objects_nb_type_created``.  ``source_id`` travels with
        the row for the same reason ``status`` does: the private-Memory
        exclusion is evaluated by the caller, inside its over-scan ceiling.
        """
        params: List[Any] = [notebook_id, object_type]
        clause = ""
        if after is not None:
            clause = "AND (created_at,id) > (%s,%s) "
            params.extend([after[0], after[1]])
        params.append(max(1, int(limit)))
        return db.execute(
            "SELECT id,object_type,source_id,payload,evidence,status,created_at "
            "FROM knowledge_objects "
            f"WHERE notebook_id=%s AND object_type=%s {clause}"
            "ORDER BY created_at,id LIMIT %s",
            tuple(params),
        ).fetchall()

    @staticmethod
    def list_knowledge_page(
        db: Any,
        notebook_id: str,
        object_type: str,
        status: Optional[str],
        offset: int,
        limit: int,
    ) -> "tuple[int, List[dict]]":
        base_query = (
            "FROM knowledge_objects "
            "WHERE notebook_id = %s AND object_type = %s"
        )
        params: List[object] = [notebook_id, object_type]
        if status:
            base_query += " AND status = %s"
            params.append(status)

        # Pagination total = a slice of the seq-gated type/status count memo
        # (from_row already warmed it this request), not a fresh per-page COUNT.
        from app.repositories.postgres import knowledge_counts_cache
        total = knowledge_counts_cache.object_type_total(
            db, notebook_id, object_type, status
        )
        rows = db.execute(
            f"SELECT * {base_query} ORDER BY created_at ASC, id ASC LIMIT %s OFFSET %s",
            (*params, limit, offset),
        ).fetchall()

        objects: List[dict] = []
        for row in rows:
            keys = row.keys()
            objects.append(
                {
                    "id": row["id"],
                    "payload": json_value(row["payload"], {}),
                    "evidence": [
                        Evidence(**item)
                        for item in json_value(row["evidence"], [])
                    ],
                    "status": row["status"],
                    "owner": row["owner"],
                    "last_reviewed": iso_timestamp(row["last_reviewed"])
                    if "last_reviewed" in keys else "",
                }
            )
        return int(total), objects

    # -------------------------------------------------------------- graph
    @staticmethod
    def graph_node_rows(db: Any, notebook_id: str) -> List[dict]:
        rows = db.execute(
            "SELECT id, object_type, status, payload FROM knowledge_objects "
            "WHERE notebook_id = %s AND status != 'deprecated'", (notebook_id,)
        ).fetchall()
        return _compat_rows(rows, payload=True)

    @staticmethod
    def relations_for_notebook(db: Any, notebook_id: str) -> List[dict]:
        rows = db.execute(
            "SELECT * FROM knowledge_relations WHERE notebook_id = %s",
            (notebook_id,),
        ).fetchall()
        return [
            {
                "id": r["id"], "source_id": r["source_id"],
                "source_object_id": r["source_object_id"],
                "target_object_id": r["target_object_id"], "edge_type": r["edge_type"],
                "evidence": json_value(r["evidence"], []),
            }
            for r in rows
        ]

    def add_relations(
        self,
        db: Any,
        notebook_id: str,
        source_id: str,
        relations: List[dict],
        now: str,
    ) -> int:
        for rel in relations:
            db.execute(
                """
                INSERT INTO knowledge_relations
                (id, notebook_id, source_id, source_object_id, target_object_id,
                 edge_type, evidence, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    self.seams.new_id("rel"), notebook_id, source_id,
                    rel["source_object_id"], rel["target_object_id"],
                    rel["edge_type"],
                    jsonb(_json_document(
                        rel.get("evidence", []),
                        expected=list,
                        field="relation evidence",
                    )),
                    normalize_timestamp(now),
                ),
            )
        return len(relations)

    # ------------------------------------------------- store_kg chunk writes
    @staticmethod
    def insert_object_chunk(
        connection: Any, rows: Sequence[tuple]
    ) -> None:
        execute_many(
            connection,
            "INSERT INTO knowledge_objects "
            "(id, notebook_id, object_type, status, owner, payload, evidence, "
            "source_candidate_id, source_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, '', %s, %s, NULL, %s, %s, %s)",
            [_object_insert_row(row) for row in rows],
        )

    @staticmethod
    def validate_source_fact_publish(
        connection: Any,
        notebook_id: str,
        source_id: str,
        source_generation: str,
        element_ids: Sequence[str],
    ) -> None:
        source = connection.execute(
            "SELECT id FROM sources WHERE id=%s AND notebook_id=%s FOR UPDATE",
            (source_id, notebook_id),
        ).fetchone()
        if source is None:
            raise RuntimeError("stale source fact generation")
        row = connection.execute(
            "SELECT id, status FROM extraction_runs "
            "WHERE notebook_id=%s AND source_id=%s "
            "ORDER BY created_at DESC, id COLLATE \"C\" DESC LIMIT 1",
            (notebook_id, source_id),
        ).fetchone()
        if not row or row["id"] != source_generation or row["status"] != "running":
            raise RuntimeError("stale source fact generation")
        expected = list(dict.fromkeys(str(value) for value in element_ids if value))
        if not expected:
            return
        owned = int(connection.execute(
            "SELECT COUNT(*) AS c FROM source_elements "
            "WHERE source_id=%s AND id=ANY(%s)",
            (source_id, expected),
        ).fetchone()["c"])
        if owned != len(expected):
            raise RuntimeError("source fact evidence crosses source generation boundary")

    @staticmethod
    def validate_stage_source_elements(
        connection: Any,
        notebook_id: str,
        source_id: str,
        element_ids: Sequence[str],
    ) -> None:
        source = connection.execute(
            "SELECT id FROM sources WHERE id=%s AND notebook_id=%s",
            (source_id, notebook_id),
        ).fetchone()
        if source is None:
            raise RuntimeError("stale indexing stage source")
        expected = list(dict.fromkeys(str(value) for value in element_ids if value))
        if not expected:
            return
        owned = int(connection.execute(
            "SELECT COUNT(*) AS c FROM source_elements "
            "WHERE source_id=%s AND id=ANY(%s)",
            (source_id, expected),
        ).fetchone()["c"])
        if owned != len(expected):
            raise RuntimeError("staged evidence crosses source boundary")

    @staticmethod
    def insert_source_fact_rows(
        connection: Any,
        rows: Sequence[tuple],
        element_rows: Sequence[tuple],
        *,
        projection_origin: str = "live",
    ) -> None:
        if projection_origin not in {"live", "historical"}:
            raise ValueError("invalid source-fact projection origin")
        execute_many(
            connection,
            "INSERT INTO knowledge_source_facts "
            "(id, notebook_id, source_id, source_generation, local_object_id, global_object_id, "
            "object_type, payload, evidence, projection_version, created_at, updated_at, "
            "projection_origin) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            [
                (
                    *row[:7],
                    jsonb(_json_document(
                        row[7], expected=dict, field="source fact payload"
                    )),
                    jsonb(_json_document(
                        row[8], expected=list, field="source fact evidence"
                    )),
                    row[9],
                    normalize_timestamp(row[10]),
                    normalize_timestamp(row[11]),
                    projection_origin,
                )
                for row in rows
            ],
        )
        execute_many(
            connection,
            "INSERT INTO knowledge_source_fact_elements "
            "(fact_id, notebook_id, source_id, source_generation, element_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (fact_id, element_id) DO NOTHING",
            [(*row[:5], normalize_timestamp(row[5])) for row in element_rows],
        )

    @staticmethod
    def insert_relation_chunk(
        connection: Any, rows: Sequence[tuple]
    ) -> None:
        execute_many(
            connection,
            "INSERT INTO knowledge_relations "
            "(id, notebook_id, source_id, source_object_id, target_object_id, "
            "edge_type, evidence, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [_relation_insert_row(row) for row in rows],
        )

    @staticmethod
    def completion_generation_is_current(
        connection: Any, notebook_id: str, source_id: str, run_id: str
    ) -> bool:
        return _completion_generation_is_current(
            connection, notebook_id, source_id, run_id
        )

    @staticmethod
    def completion_validate_scope(
        connection: Any, notebook_id: str, source_id: str, run_id: str,
        object_ids: Sequence[str], element_ids: Sequence[str]
    ) -> bool:
        # Serialize the final generation check + insert with reparse/delete.
        # begin_extraction_run takes the same source-row lock before publishing a
        # new generation; DELETE naturally conflicts with this lock as well.
        locked = connection.execute(
            "SELECT id FROM sources WHERE id=%s AND notebook_id=%s FOR UPDATE",
            (source_id, notebook_id),
        ).fetchone()
        if locked is None:
            return False
        if not _completion_generation_is_current(
            connection, notebook_id, source_id, run_id
        ):
            return False
        objects = list(dict.fromkeys(object_ids))
        elements = list(dict.fromkeys(element_ids))
        if not objects:
            return False
        object_count = connection.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=%s "
            "AND source_id=%s AND status IN "
            "('approved','reviewed','project_specific','conflict') AND id=ANY(%s)",
            (notebook_id, source_id, objects),
        ).fetchone()["c"]
        if int(object_count) != len(objects):
            return False
        if elements:
            element_count = connection.execute(
                "SELECT COUNT(*) AS c FROM source_elements WHERE source_id=%s AND id=ANY(%s)",
                (source_id, elements),
            ).fetchone()["c"]
            if int(element_count) != len(elements):
                return False
        return True

    @staticmethod
    def completion_existing_keys(
        connection: Any, notebook_id: str, object_ids: Sequence[str]
    ) -> set[tuple[str, str, str]]:
        ids = list(dict.fromkeys(object_ids))
        if not ids:
            return set()
        rows = connection.execute(
            "SELECT source_object_id,target_object_id,edge_type FROM knowledge_relations "
            "WHERE notebook_id=%s AND source_object_id=ANY(%s) AND target_object_id=ANY(%s)",
            (notebook_id, ids, ids),
        ).fetchall()
        return {(r["source_object_id"], r["target_object_id"], r["edge_type"]) for r in rows}

    @staticmethod
    def completion_page(
        connection: Any, notebook_id: str, source_id: str, run_id: str,
        mode: str, schema_version: int, reasoning_edge_types: Sequence[str],
        edge_contract_rows: Sequence[tuple[str, str, str]],
        known_edge_types: Sequence[str], core_node_types: Sequence[str],
        limit: int, now: str
    ) -> dict:
        if not _completion_generation_is_current(
            connection, notebook_id, source_id, run_id
        ):
            return {"rows": [], "cursor": "", "next_cursor": "", "exhausted": False,
                    "generation_conflict": True}
        connection.execute(
            "INSERT INTO kg_relation_completion_state "
            "(notebook_id,source_id,source_generation,mode,next_object_id,status,"
            "schema_version,updated_at) VALUES (%s,%s,%s,%s,'','pending',%s,%s) "
            "ON CONFLICT (source_id,source_generation,mode) DO NOTHING",
            (notebook_id, source_id, run_id, mode, schema_version,
             normalize_timestamp(now)),
        )
        connection.execute(
            "UPDATE kg_relation_completion_state SET next_object_id='',status='pending',"
            "schema_version=%s,updated_at=%s WHERE source_id=%s AND source_generation=%s "
            "AND mode=%s AND schema_version!=%s",
            (schema_version, normalize_timestamp(now), source_id, run_id, mode,
             schema_version),
        )
        state = connection.execute(
            "SELECT next_object_id,status FROM kg_relation_completion_state "
            "WHERE source_id=%s AND source_generation=%s AND mode=%s",
            (source_id, run_id, mode),
        ).fetchone()
        cursor = str(state["next_object_id"] or "")
        if state["status"] == "completed":
            return {"rows": [], "cursor": cursor, "next_cursor": cursor,
                    "exhausted": True, "already_completed": True}
        page_limit = max(1, int(limit))
        reasoning_types = list(dict.fromkeys(reasoning_edge_types))
        contract_rows = tuple(dict.fromkeys(edge_contract_rows))
        known_types = list(dict.fromkeys(known_edge_types))
        core_types = list(dict.fromkeys(core_node_types))
        if not reasoning_types or not contract_rows or not known_types or not core_types:
            raise ValueError("completion edge contract must not be empty")
        pair_sql = " OR ".join(
            "(r.edge_type=%s AND rs.object_type=%s AND rt.object_type=%s)"
            for _ in contract_rows
        )
        queryable_sql = (
            f"(({pair_sql}) OR (r.edge_type=ANY(%s) AND "
            "(NOT (rs.object_type=ANY(%s)) OR NOT (rt.object_type=ANY(%s)))))"
        )
        contract_params = tuple(
            value for row in contract_rows for value in row
        ) + (known_types, core_types, core_types)
        rows = connection.execute(
            "SELECT ko.id,ko.object_type,ko.payload,ko.evidence,ko.status,"
            "EXISTS(SELECT 1 FROM knowledge_relations r "
            " JOIN knowledge_objects rs ON rs.id=r.source_object_id "
            " JOIN knowledge_objects rt ON rt.id=r.target_object_id "
            " WHERE r.notebook_id=ko.notebook_id AND r.review_status!='rejected' "
            " AND (r.source_object_id=ko.id OR r.target_object_id=ko.id) "
            f" AND {queryable_sql}) AS has_relation,"
            "EXISTS(SELECT 1 FROM knowledge_relations r "
            " JOIN knowledge_objects rs ON rs.id=r.source_object_id "
            " JOIN knowledge_objects rt ON rt.id=r.target_object_id "
            " WHERE r.notebook_id=ko.notebook_id AND r.review_status!='rejected' "
            " AND (r.source_object_id=ko.id OR r.target_object_id=ko.id) "
            f" AND r.edge_type=ANY(%s) AND {queryable_sql}) "
            "AS has_reasoning_relation FROM knowledge_objects ko "
            "WHERE ko.notebook_id=%s AND ko.source_id=%s AND ko.id>%s "
            "AND ko.status IN ('approved','reviewed','project_specific','conflict') "
            "ORDER BY ko.id COLLATE \"C\" LIMIT %s",
            (*contract_params, reasoning_types, *contract_params,
             notebook_id, source_id, cursor,
             page_limit + 1),
        ).fetchall()
        page = rows[:page_limit]
        return {
            "rows": page,
            "cursor": cursor,
            "next_cursor": str(page[-1]["id"]) if page else cursor,
            "exhausted": len(rows) <= page_limit,
        }

    @staticmethod
    def completion_candidate_rows(
        connection: Any, notebook_id: str, source_id: str,
        object_ids: Sequence[str]
    ) -> list:
        ids = list(dict.fromkeys(object_ids))
        if not ids:
            return []
        return connection.execute(
            "SELECT id,object_type,payload,evidence,status FROM knowledge_objects "
            "WHERE notebook_id=%s AND source_id=%s AND id=ANY(%s) "
            "AND status IN ('approved','reviewed','project_specific','conflict')",
            (notebook_id, source_id, ids),
        ).fetchall()

    @staticmethod
    def completion_element_rows(
        connection: Any, source_id: str, element_ids: Sequence[str]
    ) -> list:
        ids = list(dict.fromkeys(element_ids))
        if not ids:
            return []
        return connection.execute(
            "SELECT id,source_id,element_type,location_label,text "
            "FROM source_elements WHERE source_id=%s AND id=ANY(%s)",
            (source_id, ids),
        ).fetchall()

    @staticmethod
    def completion_pending_states(
        connection: Any, after_source_id: str, after_mode: str, limit: int
    ) -> list:
        return connection.execute(
            "SELECT st.notebook_id,st.source_id,st.source_generation,st.mode,s.title "
            "FROM kg_relation_completion_state AS st "
            "JOIN sources AS s ON s.id=st.source_id "
            "WHERE st.status='pending' AND (st.source_id COLLATE \"C\">%s OR "
            "(st.source_id=%s AND st.mode COLLATE \"C\">%s)) "
            "AND st.source_generation=(SELECT er.id FROM extraction_runs AS er "
            "WHERE er.notebook_id=st.notebook_id AND er.source_id=st.source_id "
            "ORDER BY er.created_at DESC,er.id COLLATE \"C\" DESC LIMIT 1) "
            "ORDER BY st.source_id COLLATE \"C\",st.mode COLLATE \"C\" LIMIT %s",
            (after_source_id, after_source_id, after_mode, max(1, int(limit))),
        ).fetchall()

    @staticmethod
    def completion_mark_state_stale(
        connection: Any, notebook_id: str, source_id: str,
        run_id: str, mode: str, now: str
    ) -> bool:
        cursor = connection.execute(
            "UPDATE kg_relation_completion_state SET status='stale',updated_at=%s "
            "WHERE notebook_id=%s AND source_id=%s AND source_generation=%s "
            "AND mode=%s AND status='pending'",
            (normalize_timestamp(now), notebook_id, source_id, run_id, mode),
        )
        return cursor.rowcount == 1

    @staticmethod
    def completion_transition_mode_state(
        connection: Any, notebook_id: str, source_id: str, run_id: str,
        old_mode: str, new_mode: str, schema_version: int, now: str
    ) -> bool:
        """Atomically publish the new recoverable cursor before retiring the old."""
        if new_mode not in {"shadow", "write"}:
            return False
        locked = connection.execute(
            "SELECT id FROM sources WHERE id=%s AND notebook_id=%s FOR UPDATE",
            (source_id, notebook_id),
        ).fetchone()
        if locked is None or not _completion_generation_is_current(
            connection, notebook_id, source_id, run_id
        ):
            return False
        timestamp = normalize_timestamp(now)
        connection.execute(
            "INSERT INTO kg_relation_completion_state "
            "(notebook_id,source_id,source_generation,mode,next_object_id,status,"
            "schema_version,updated_at) VALUES (%s,%s,%s,%s,'','pending',%s,%s) "
            "ON CONFLICT (source_id,source_generation,mode) DO NOTHING",
            (notebook_id, source_id, run_id, new_mode, schema_version, timestamp),
        )
        connection.execute(
            "UPDATE kg_relation_completion_state SET status='pending',"
            "next_object_id=CASE WHEN schema_version!=%s THEN '' ELSE next_object_id END,"
            "schema_version=%s,updated_at=%s WHERE notebook_id=%s AND source_id=%s "
            "AND source_generation=%s AND mode=%s AND status='stale'",
            (schema_version, schema_version, timestamp, notebook_id, source_id,
             run_id, new_mode),
        )
        connection.execute(
            "UPDATE kg_relation_completion_state SET status='stale',updated_at=%s "
            "WHERE notebook_id=%s AND source_id=%s AND source_generation=%s "
            "AND mode=%s AND status='pending'",
            (timestamp, notebook_id, source_id, run_id, old_mode),
        )
        return True

    @staticmethod
    def completion_advance_state(
        connection: Any, notebook_id: str, source_id: str, run_id: str,
        mode: str, schema_version: int, expected_cursor: str,
        next_cursor: str, status: str, now: str
    ) -> bool:
        if status not in {"pending", "completed"}:
            return False
        locked = connection.execute(
            "SELECT id FROM sources WHERE id=%s AND notebook_id=%s FOR UPDATE",
            (source_id, notebook_id),
        ).fetchone()
        if locked is None or not _completion_generation_is_current(
            connection, notebook_id, source_id, run_id
        ):
            return False
        cursor = connection.execute(
            "UPDATE kg_relation_completion_state SET next_object_id=%s,status=%s,updated_at=%s "
            "WHERE notebook_id=%s AND source_id=%s AND source_generation=%s AND mode=%s "
            "AND schema_version=%s AND next_object_id=%s AND status='pending'",
            (next_cursor, status, normalize_timestamp(now), notebook_id, source_id,
             run_id, mode, schema_version, expected_cursor),
        )
        return cursor.rowcount == 1

    @staticmethod
    def insert_completion_relations(connection: Any, rows: Sequence[tuple]) -> int:
        inserted = 0
        for row in rows:
            converted = _relation_insert_row(row)
            cursor = connection.execute(
                "INSERT INTO knowledge_relations "
                "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,"
                "evidence,created_at,review_status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending') "
                "ON CONFLICT (id) DO NOTHING",
                converted,
            )
            inserted += max(0, int(cursor.rowcount or 0))
        return inserted

    @staticmethod
    def insert_kg_fts_rows(
        connection: Any, rows: Sequence[tuple]
    ) -> None:
        # PostgreSQL GIN/trigram indexes derive directly from knowledge_objects.
        del connection, rows

    @staticmethod
    def insert_object_source_rows(
        connection: Any, rows: Sequence[tuple]
    ) -> None:
        """Forward maintenance (P0-4 reverse index) for FRESH inserts — rows
        never had prior entries, so a plain batched INSERT suffices (no
        DELETE-first)."""
        execute_many(
            connection,
            "INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id) "
            "VALUES (%s, %s, %s)",
            rows,
        )

    # ------------------------------------------------- knowhow projection
    # (Task 5, knowhow-tables PR-1): the deterministic projector writes
    # case/procedure/tool objects and their edges directly (bypassing
    # store_kg's fresh-id-per-call allocation — knowhow ids are STABLE
    # hashes of row_id/column_id/table_id so reprojection is idempotent, not
    # append-only), reusing insert_object_chunk/insert_relation_chunk above
    # for the actual INSERTs. These primitives cover the row/table-scoped
    # DELETEs that pattern needs and are absent from the plain store_kg path.
    @staticmethod
    def delete_objects_by_source_and_row(
        connection: Any, source_id: str, row_id: str
    ) -> None:
        """Delete this row's PRIOR case+procedure objects (any column) under
        the knowhow hidden source, keyed by ``payload.row_id`` — NOT tool
        objects (table-scoped, deduped across rows, so they carry no
        ``row_id`` key and are correctly left untouched here; project_table's
        full rebuild is what sweeps orphaned tools). json_extract on payload
        is unindexed but source_id narrows the scan first (idx_knowledge_
        objects_source), acceptable at this feature's bounded scale."""
        connection.execute(
            "DELETE FROM knowledge_object_sources WHERE object_id IN ("
            "SELECT id FROM knowledge_objects WHERE source_id = %s "
            "AND (payload ->> 'row_id') COLLATE \"C\" = %s)",
            (source_id, row_id),
        )
        connection.execute(
            "DELETE FROM knowledge_objects WHERE source_id = %s "
            "AND (payload ->> 'row_id') COLLATE \"C\" = %s",
            (source_id, row_id),
        )

    @staticmethod
    def delete_objects_by_source(
        connection: Any, source_id: str
    ) -> None:
        """Full wipe of every object (case+procedure+tool) a knowhow table's
        hidden source has ever produced — project_table's escape-hatch
        rebuild and delete_table_projection's cleanup both use this rather
        than the row-scoped variant above, so a stale/orphaned tool (or a
        procedure whose column has since been deleted) never survives a full
        rebuild."""
        connection.execute(
            "DELETE FROM knowledge_object_sources WHERE object_id IN ("
            "SELECT id FROM knowledge_objects WHERE source_id = %s)",
            (source_id,),
        )
        connection.execute(
            "DELETE FROM knowledge_objects WHERE source_id = %s", (source_id,)
        )

    @classmethod
    def prune_cluster_rows_for_source(
        cls,
        connection: Any,
        notebook_id: str,
        source_id: str,
        keep_object_ids: Iterable[str] = (),
    ) -> int:
        """Drop this source's stale ``concept_clusters`` membership rows
        (SQLite parity — see that adapter's docstring for why the surviving,
        about-to-be-reinserted ids must be spared and why this has to run
        BEFORE ``delete_objects_by_source``)."""
        keep = set(keep_object_ids)
        stale = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM knowledge_objects WHERE source_id = %s",
                (source_id,),
            ).fetchall()
            if row["id"] not in keep
        ]
        deleted = 0
        for offset in range(0, len(stale), _DELETE_OBJECT_BATCH_SIZE):
            batch = stale[offset : offset + _DELETE_OBJECT_BATCH_SIZE]
            cursor = connection.execute(
                "DELETE FROM concept_clusters "
                "WHERE notebook_id = %s AND member_object_id = ANY(%s)",
                (notebook_id, batch),
            )
            deleted += int(getattr(cursor, "rowcount", 0) or 0)
        return deleted

    @staticmethod
    def delete_relations_by_source_object(
        connection: Any, notebook_id: str, source_object_id: str
    ) -> None:
        """Delete every edge OUT of one case object (identified_by/
        diagnosed_by/fixed_by/requires_tool all have the case as source, per
        the knowhow projection spec) — one call cleans all of a row's prior
        edges regardless of which cell changed. Uses idx_knowledge_relations_
        nb_source (notebook_id, source_object_id)."""
        connection.execute(
            "DELETE FROM knowledge_relations WHERE notebook_id = %s "
            "AND source_object_id = %s",
            (notebook_id, source_object_id),
        )

    @staticmethod
    def delete_relations_by_source(
        connection: Any, source_id: str
    ) -> None:
        """Full wipe of every relation a knowhow table's hidden source has
        ever produced (project_table / delete_table_projection). Uses
        idx_knowledge_relations_source."""
        connection.execute(
            "DELETE FROM knowledge_relations WHERE source_id = %s", (source_id,)
        )

    @staticmethod
    def insert_object_if_missing(
        connection: Any, row: tuple
    ) -> None:
        """Upsert-by-absence for tool objects: a tool's id is a stable hash of
        (table_id, normalized name), so the SAME tool referenced by multiple
        rows always maps to the SAME id — the first row to mention it creates
        the row, later rows are a no-op (INSERT OR IGNORE on the id PRIMARY
        KEY) rather than a second, redundant insert attempt."""
        connection.execute(
            "INSERT INTO knowledge_objects "
            "(id, notebook_id, object_type, status, owner, payload, evidence, "
            "source_candidate_id, source_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, '', %s, %s, NULL, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            _object_insert_row(row),
        )

    @staticmethod
    def legacy_typed_table_ids(
        connection: Any, object_types: Sequence[str], id_prefix: str
    ) -> List[str]:
        """knowhow-tables PR-2+3 Task 2's one-shot startup migration bridge:
        every DISTINCT ``payload.table_id`` among objects whose
        ``object_type`` is one of ``object_types`` AND whose id starts with
        ``id_prefix`` — the detection query
        ``app.services.knowhow.projection.find_legacy_projected_table_ids``
        (its only caller) needs to find knowhow tables still carrying PR-1's
        fixed case/procedure/tool vocabulary so they can be reprojected under
        the cell-level dynamic-type model. A plain SELECT; the caller owns
        interpretation/scheduling. Kept here (not inline SQL in the service
        layer) per this codebase's SQL-ownership rule (Task 27,
        test_repository_callers_static.py): every knowledge_objects query
        lives in this store, never in a services/* file."""
        placeholders = ",".join("%s" for _ in object_types)
        rows = connection.execute(
            f"SELECT DISTINCT (payload ->> 'table_id') COLLATE \"C\" AS table_id "
            f"FROM knowledge_objects WHERE object_type IN ({placeholders}) "
            f"AND id LIKE %s",
            (*object_types, f"{id_prefix}%"),
        ).fetchall()
        return [r["table_id"] for r in rows if r["table_id"]]

    def get_object_row(
        self, notebook_id: str, object_id: str
    ) -> "dict | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM knowledge_objects WHERE id=%s AND notebook_id=%s",
                (object_id, notebook_id),
            ).fetchone()
            return (_compat_rows([row], payload=True, evidence=True)[0]
                    if row is not None else None)

    # --------------------------------------------------------- provenance
    @staticmethod
    def source_ids_from_evidence(evidence_json: Optional[str | list]) -> set:
        """PURE: parse an evidence JSON TEXT column value into the set of distinct
        source_ids it references (Evidence.source_id is present on every item —
        confirmed in app/models/schemas.py; a merged object's evidence can span
        multiple sources, which is exactly why a per-object single source_id
        column is insufficient and this reverse table exists)."""
        if isinstance(evidence_json, list):
            items = evidence_json
        elif evidence_json is None or isinstance(evidence_json, (str, bytes)):
            # Whitelist, mirrored on the SQLite side: only the two shapes this
            # column legitimately takes (JSONB row already decoded to a list,
            # or a JSON text value) are accepted.  Any other type raises
            # loudly instead of degrading to an empty set — a silent empty
            # set here means the reverse index quietly loses rows.
            try:
                items = json.loads(evidence_json or "[]")
            except json.JSONDecodeError:
                items = []
        else:
            raise TypeError(
                f"evidence must be a JSON string or decoded list, "
                f"got {type(evidence_json).__name__}"
            )
        return {
            item.get("source_id")
            for item in items
            if isinstance(item, dict) and item.get("source_id")
        }

    @classmethod
    def replace_object_sources(
        cls,
        connection: Any,
        object_id: str,
        notebook_id: str,
        evidence_json: Optional[str],
    ) -> None:
        """Forward maintenance: replace object_id's rows in the reverse index with
        the source_ids its CURRENT evidence references. Called by every write path
        that creates/updates a knowledge_objects row with evidence (store_kg,
        confirm_promotion insert/merge, merge_knowledge). Delete-then-insert keeps
        this correct even when evidence shrinks (not currently possible, but cheap
        to keep safe)."""
        connection.execute(
            "DELETE FROM knowledge_object_sources WHERE object_id = %s", (object_id,)
        )
        source_ids = cls.source_ids_from_evidence(evidence_json)
        if source_ids:
            execute_many(
                connection,
                "INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id) "
                "VALUES (%s, %s, %s)",
                [(object_id, sid, notebook_id) for sid in source_ids],
            )

    @staticmethod
    def delete_object_sources(
        connection: Any, object_ids: List[str]
    ) -> None:
        """Deletion coherence: drop reverse-index rows for objects that are
        actually removed from knowledge_objects (source delete/reparse path).
        merge_knowledge does NOT call this — it deprecates the losing object
        in place rather than deleting it, so that object's evidence (now folded
        into the target too, but still physically present on its own row) must
        stay indexed until it is truly deleted."""
        if not object_ids:
            return
        for offset in range(0, len(object_ids), _DELETE_OBJECT_BATCH_SIZE):
            batch = object_ids[offset : offset + _DELETE_OBJECT_BATCH_SIZE]
            connection.execute(
                "DELETE FROM knowledge_object_sources WHERE object_id = ANY(%s)",
                (batch,),
            )

    @staticmethod
    def source_index_backfilled(db: Any, notebook_id: str) -> bool:
        row = db.execute(
            "SELECT source_index_backfilled FROM unified_kg_state WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()
        return bool(row and row["source_index_backfilled"])

    @staticmethod
    def clear_source_index_backfilled(db: Any, notebook_id: str) -> None:
        """batch-3-W1 T-5a (codex #663 R16 P1) — SQLite twin's docstring
        has the full rationale (revoked with the drain's first page,
        re-asserted after a successful final reset)."""
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=0 "
            "WHERE notebook_id=%s",
            (notebook_id,),
        )

    def mark_source_index_backfilled(
        self, db: Any, notebook_id: str
    ) -> None:
        now = self.seams.now()
        db.execute(
            """
            INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, source_index_backfilled, updated_at)
            VALUES (%s, 0, 0, 1, %s)
            ON CONFLICT(notebook_id) DO UPDATE SET
              source_index_backfilled=1,
              updated_at=excluded.updated_at
            """,
            (notebook_id, normalize_timestamp(now)),
        )

    @staticmethod
    def chunk_elements_indexed(db: Any, notebook_id: str) -> bool:
        """Has this notebook's element -> chunk reverse index been backfilled?

        One indexed single-row read on ``unified_kg_state`` — the same shape
        (and cost) as ``source_index_backfilled``. False keeps the legacy
        whole-notebook chunk scan, byte-for-byte."""
        row = db.execute(
            "SELECT chunk_elements_indexed FROM unified_kg_state WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()
        return bool(row and row["chunk_elements_indexed"])

    def mark_chunk_elements_indexed(self, db: Any, notebook_id: str) -> None:
        now = self.seams.now()
        db.execute(
            """
            INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, chunk_elements_indexed, updated_at)
            VALUES (%s, 0, 0, 1, %s)
            ON CONFLICT(notebook_id) DO UPDATE SET
              chunk_elements_indexed=1,
              updated_at=excluded.updated_at
            """,
            (notebook_id, normalize_timestamp(now)),
        )

    def stale_object_ids_for_source(
        self, db: Any, source_id: str, notebook_id: str
    ) -> List[str]:
        """Return knowledge_objects.id values whose evidence references source_id.

        Fast path (backfilled notebooks): a single indexed SQL lookup against
        knowledge_object_sources — O(matches), not O(notebook size).

        Legacy path (not yet backfilled): filter the JSONB evidence in keyset-
        paged database queries. Interactive delete/reparse must not turn into a
        notebook-wide read/parse/write backfill while holding its transaction;
        the explicit batch-ingest backfill remains responsible for populating
        the reverse index and flipping the marker."""
        object_ids: List[str] = []
        after_id = ""
        while True:
            batch = self._stale_object_ids_for_source_batch(
                db, source_id, notebook_id, after_id=after_id
            )
            if not batch:
                return object_ids
            object_ids.extend(batch)
            after_id = batch[-1]

    def _stale_object_ids_for_source_batch(
        self,
        db: Any,
        source_id: str,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = _DELETE_OBJECT_BATCH_SIZE,
    ) -> List[str]:
        """Return one stable, bounded source-reference batch."""
        page_limit = max(1, min(int(limit), _DELETE_OBJECT_BATCH_SIZE))
        if self.source_index_backfilled(db, notebook_id):
            rows = db.execute(
                "SELECT DISTINCT object_id COLLATE \"C\" AS object_id "
                "FROM knowledge_object_sources "
                "WHERE source_id = %s AND notebook_id = %s "
                "AND object_id COLLATE \"C\" > %s "
                "ORDER BY object_id COLLATE \"C\" LIMIT %s",
                (source_id, notebook_id, after_id, page_limit),
            ).fetchall()
            return [row["object_id"] for row in rows]

        rows = db.execute(
            "SELECT id AS object_id FROM knowledge_objects "
            "WHERE notebook_id = %s AND id COLLATE \"C\" > %s "
            "AND evidence @> jsonb_build_array(jsonb_build_object('source_id', %s::text)) "
            "ORDER BY id COLLATE \"C\" LIMIT %s",
            (notebook_id, after_id, source_id, page_limit),
        ).fetchall()
        return [row["object_id"] for row in rows]

    @staticmethod
    def _direct_object_ids_for_source_batch(
        db: Any,
        source_id: str,
        limit: int = _DELETE_OBJECT_BATCH_SIZE,
    ) -> List[str]:
        page_limit = max(1, min(int(limit), _DELETE_OBJECT_BATCH_SIZE))
        rows = db.execute(
            "SELECT id FROM knowledge_objects WHERE source_id = %s LIMIT %s",
            (source_id, page_limit),
        ).fetchall()
        return [row["id"] for row in rows]

    @classmethod
    def _delete_object_id_batch(
        cls, db: Any, notebook_id: str, object_ids: Sequence[str]
    ) -> None:
        """Delete one already-bounded object batch and its derived rows.

        Mirrors the SQLite adapter verbatim, including the ``concept_clusters``
        membership rows deleted in this same transaction by
        ``(notebook_id, member_object_id)`` (``idx_clusters_member`` drives the
        seek; the notebook column keeps this statement semantically identical to
        the ``sweep_orphan_clusters_page`` it replaces) — see that adapter's
        docstring for why they belong here rather than in
        ``incremental_fuse_source``'s notebook-wide orphan anti-join, why the
        knowhow projector deliberately does NOT do the same, and why no
        ``cluster_mutation_seq`` bump accompanies it.
        """
        batch = list(object_ids[:_DELETE_OBJECT_BATCH_SIZE])
        if not batch:
            return
        db.execute(
            "DELETE FROM knowledge_embeddings WHERE object_id = ANY(%s)",
            (batch,),
        )
        db.execute(
            "DELETE FROM concept_clusters "
            "WHERE notebook_id = %s AND member_object_id = ANY(%s)",
            (notebook_id, batch),
        )
        db.execute(
            "DELETE FROM knowledge_objects WHERE id = ANY(%s)",
            (batch,),
        )
        cls.delete_object_sources(db, batch)

    def clear_source_graph_state(
        self,
        db: Any,
        source_id: str,
        notebook_id: str,
    ) -> None:
        """Delete one source's graph rows without touching its extraction history."""
        db.execute(
            "DELETE FROM kg_relation_completion_state WHERE source_id = %s",
            (source_id,),
        )
        db.execute(
            "DELETE FROM knowledge_source_facts "
            "WHERE source_id=%s AND notebook_id=%s",
            (source_id, notebook_id),
        )
        db.execute(
            "DELETE FROM knowledge_source_fact_backfills "
            "WHERE source_id=%s AND notebook_id=%s",
            (source_id, notebook_id),
        )
        stale_after_id = ""
        while True:
            stale_batch = self._stale_object_ids_for_source_batch(
                db, source_id, notebook_id, after_id=stale_after_id
            )
            if not stale_batch:
                break
            stale_after_id = stale_batch[-1]
            self._delete_object_id_batch(db, notebook_id, stale_batch)
        self.delete_relations_for_source(db, source_id)
        while True:
            direct_batch = self._direct_object_ids_for_source_batch(db, source_id)
            if not direct_batch:
                break
            self._delete_object_id_batch(db, notebook_id, direct_batch)

    def clear_source_extraction_state(
        self,
        db: Any,
        source_id: str,
        notebook_id: str,
        *,
        clear_embeddings: bool,
    ) -> None:
        self.clear_source_graph_state(db, source_id, notebook_id)
        db.execute("DELETE FROM extraction_runs WHERE source_id = %s", (source_id,))
        if clear_embeddings:
            db.execute("DELETE FROM element_embeddings WHERE source_id = %s", (source_id,))

    @staticmethod
    def delete_relations_for_source(db: Any, source_id: str) -> None:
        db.execute("DELETE FROM knowledge_relations WHERE source_id = %s", (source_id,))

    def begin_extraction_run(
        self,
        db: Any,
        source_id: str,
        notebook_id: str,
        run_id: str,
        created_at: str,
        *,
        preserve_existing: bool = False,
        indexing_pipeline_id: str = "",
        indexing_pipeline_version: str = "builtin.chunk.v1",
    ) -> None:
        """Open a run, optionally retaining the current graph until replacement."""
        # Same transaction lock as completion_validate_scope/advance_state: a
        # stale completion cannot pass the generation CAS and commit after this
        # run becomes current under PostgreSQL READ COMMITTED.
        connection_row = db.execute(
            "SELECT id FROM sources WHERE id=%s AND notebook_id=%s FOR UPDATE",
            (source_id, notebook_id),
        ).fetchone()
        if connection_row is None:
            raise KeyError(source_id)
        if not preserve_existing:
            self.clear_source_extraction_state(
                db, source_id, notebook_id, clear_embeddings=False
            )
        db.execute(
            """INSERT INTO extraction_runs
               (id, notebook_id, source_id, run_type, status, error_message,
                indexing_pipeline_id, indexing_pipeline_version, created_at, updated_at)
               VALUES (%s, %s, %s, 'kg', 'running', '', %s, %s, %s, %s)""",
            (
                run_id,
                notebook_id,
                source_id,
                indexing_pipeline_id,
                indexing_pipeline_version,
                normalize_timestamp(created_at),
                normalize_timestamp(created_at),
            ))

    @staticmethod
    def finish_extraction_run(
        db: Any, run_id: str, status: str, message: str, now: str
    ) -> None:
        db.execute(
            "UPDATE extraction_runs SET status=%s, error_message=%s, updated_at=%s WHERE id=%s",
            (status, message, normalize_timestamp(now), run_id),
        )

    # ------------------------------------------------------------------ FTS
    @staticmethod
    def fts_search(
        db, notebook_id: str, q: str, k: int = 30, *,
        allowed_source_ids: Sequence[str] | None = None,
        corpus_langs: Sequence[str] | None = None,
        allow_knn: bool = False,
        authoritative_source_filter: bool = False,
        knn_max_term_chars: int | None = None,
        routing_stats: dict[str, int | float] | None = None,
    ) -> List[Dict]:
        """Return deterministic lexical knowledge hits from trigram candidates."""
        needle = (q or "").strip()
        return _lexical_candidate_union(
            db,
            notebook_id,
            needle,
            k,
            candidate_rows_for_terms=knowledge_candidate_rows_for_terms,
            candidate_documents=knowledge_candidate_documents,
            output_id="object_id",
            text_field="name",
            allowed_source_ids=allowed_source_ids,
            corpus_langs=corpus_langs,
            allow_knn=allow_knn,
            authoritative_source_filter=authoritative_source_filter,
            knn_max_term_chars=knn_max_term_chars,
            routing_stats=routing_stats,
        )

    def chunk_fts_search(
        self, db, notebook_id: str, q: str, k: int = 30, *,
        allowed_source_ids: Sequence[str] | None = None,
        corpus_langs: Sequence[str] | None = None,
    ) -> List[Dict]:
        """Return deterministic lexical chunk hits under a private deadline.

        The pool-wide timeout protects every statement, but this optional
        recall/fallback leg must not be allowed to consume that whole budget.
        A savepoint makes PostgreSQL's query-cancellation transaction abort
        local to this probe; restoring the prior GUC keeps a caller-owned
        transaction and the next pooled borrower byte-for-byte unchanged.
        """
        needle = (q or "").strip()
        if not needle or k <= 0:
            return []
        timeout_ms = max(
            1,
            math.ceil(
                float(self.database.settings.postgres_chunk_fts_timeout_seconds)
                * 1000
            ),
        )
        savepoint = "chunk_fts_budget"
        db.execute(f"SAVEPOINT {savepoint}")
        previous = db.execute(
            "SELECT current_setting('statement_timeout') AS value"
        ).fetchone()["value"]
        try:
            db.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{timeout_ms}ms",),
            )
            result = _lexical_candidate_union(
                db,
                notebook_id,
                needle,
                k,
                candidate_rows_for_terms=chunk_candidate_rows_for_terms,
                candidate_documents=chunk_candidate_documents,
                output_id="chunk_id",
                text_field="text",
                allowed_source_ids=allowed_source_ids,
                corpus_langs=corpus_langs,
            )
        except Exception as exc:
            # A cancelled PostgreSQL statement leaves the transaction aborted;
            # only ROLLBACK TO SAVEPOINT can make the caller's lease usable.
            db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
            if isinstance(exc, errors.QueryCanceled):
                raise ChunkLexicalSearchTimeout(
                    "chunk lexical search exceeded its bounded deadline"
                ) from None
            raise
        db.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(previous),),
        )
        db.execute(f"RELEASE SAVEPOINT {savepoint}")
        return result

    @staticmethod
    def chunk_exact_search(db, notebook_id: str, needle: str, k: int = 50) -> List[Dict]:
        """EXACT substring chunk hits — the identifier fast path's probe.

        Semantically equal to the SQLite adapter: no `lexical_recall_terms`
        decomposition, so `set_db` means `set_db` and never `set` OR `db`.
        `score` is this backend's own trigram similarity; the caller groups by
        section and never compares scores across backends.
        """
        term = (needle or "").strip()
        if len(term) < 3 or k <= 0:
            return []
        rows = chunk_exact_candidate_rows(db, notebook_id, term, k)
        return [
            {
                "chunk_id": row["candidate_id"],
                "source_id": row["source_id"],
                "section_path": row["section_path"] or "",
                "score": float(row["candidate_similarity"] or 0.0),
                "match": "lexical",
            }
            for row in rows
        ]

    @staticmethod
    def backfill_fts(db: Any, notebook_id: str) -> int:
        """Re-populate kg_objects_fts from knowledge_objects for this notebook.
        Idempotent: deletes existing FTS rows first, then re-inserts from
        knowledge_objects (non-deprecated, non-empty name). Returns the number
        of rows inserted."""
        rows = db.execute(
            "SELECT id, payload FROM knowledge_objects "
            "WHERE notebook_id=%s AND status != 'deprecated'",
            (notebook_id,),
        ).fetchall()
        count = 0
        for r in rows:
            try:
                payload = json_value(r["payload"], {})
            except Exception:
                payload = {}
            name = (payload.get("name") or "").strip()
            if name:
                count += 1
        return count

    @staticmethod
    def object_meta_rows(db: Any, ids: List[str]) -> List[dict]:
        if not ids:
            return []
        placeholders = ",".join("%s" for _ in ids)
        return _compat_rows(db.execute(
            f"SELECT id, object_type, status, payload FROM knowledge_objects "
            f"WHERE id IN ({placeholders})",
            ids,
        ).fetchall(), payload=True)

    # ------------------------------------------------------------- schemas
    @staticmethod
    def lock_schema_registry(db: Any) -> None:
        # Serialize cross-table absence checks: no UNIQUE constraint can span
        # the global baseline and notebook overlay tables.
        db.execute(
            "LOCK TABLE object_schemas, notebook_object_schemas "
            "IN SHARE ROW EXCLUSIVE MODE"
        )

    @staticmethod
    def schema_rows(db: Any) -> List[dict]:
        return _compat_schema_rows(db.execute("SELECT * FROM object_schemas").fetchall())

    @staticmethod
    def active_schema_rows(db: Any) -> List[dict]:
        return _compat_schema_rows(db.execute(
            "SELECT * FROM object_schemas WHERE status = 'active'"
        ).fetchall())

    @staticmethod
    def schema_row(db: Any, object_type: str) -> "dict | None":
        row = db.execute(
            "SELECT * FROM object_schemas WHERE object_type = %s", (object_type,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["fields"] = _json_text(result["fields"], [])
        result["list_fields"] = _json_text(result["list_fields"], [])
        result["created_at"] = iso_timestamp(result["created_at"])
        result["updated_at"] = iso_timestamp(result["updated_at"])
        return result

    @staticmethod
    def schema_exists(db: Any, object_type: str) -> bool:
        return db.execute(
            "SELECT 1 FROM object_schemas WHERE object_type = %s", (object_type,)
        ).fetchone() is not None

    @staticmethod
    def existing_schema_types(db: Any) -> set:
        return {
            r["object_type"]
            for r in db.execute("SELECT object_type FROM object_schemas").fetchall()
        }

    @staticmethod
    def insert_custom_schema(
        db: Any,
        object_type: str,
        plural: str,
        fields_json: str,
        primary: str,
        description: str,
        label: str,
        list_fields_json: str,
        now: str,
    ) -> None:
        db.execute(
            """
            INSERT INTO object_schemas
            (object_type, plural, fields, primary_field, description, label,
             list_fields, source, status, rationale, notebook_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'custom', 'active', '', '', %s, %s)
            """,
            (
                object_type,
                plural,
                jsonb(_json_document(fields_json, expected=list, field="schema fields")),
                primary,
                description,
                label,
                jsonb(_json_document(
                    list_fields_json, expected=list, field="schema list fields"
                )),
                normalize_timestamp(now),
                normalize_timestamp(now),
            ),
        )

    @staticmethod
    def insert_induced_schema(
        db: Any,
        object_type: str,
        plural: str,
        fields_json: str,
        primary: str,
        description: str,
        label: str,
        rationale: str,
        notebook_id: str,
        now: str,
    ) -> None:
        db.execute(
            """
            INSERT INTO object_schemas
            (object_type, plural, fields, primary_field, description, label,
             list_fields, source, status, rationale, notebook_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, '[]', 'induced', 'proposed', %s, %s, %s, %s)
            """,
            (
                object_type,
                plural,
                jsonb(_json_document(fields_json, expected=list, field="schema fields")),
                primary,
                description,
                label,
                rationale,
                notebook_id,
                normalize_timestamp(now),
                normalize_timestamp(now),
            ),
        )

    @staticmethod
    def update_schema_columns(
        db: Any,
        object_type: str,
        updates: List[str],
        values: List[object],
    ) -> None:
        allowed = {
            "plural", "fields", "primary_field", "description", "label",
            "list_fields", "status", "updated_at",
        }
        columns = []
        adapted = list(values[:-1])
        for index, update in enumerate(updates):
            column = update.split("=", 1)[0].strip()
            if column not in allowed:
                raise ValueError("unsupported object schema update column")
            columns.append(sql.SQL("{}=%s").format(sql.Identifier(column)))
            if column in {"fields", "list_fields"}:
                adapted[index] = jsonb(_json_document(
                    adapted[index], expected=list, field=f"schema {column}"
                ))
            elif column == "updated_at":
                adapted[index] = normalize_timestamp(adapted[index])
        statement = sql.SQL("UPDATE object_schemas SET {} WHERE object_type=%s").format(
            sql.SQL(",").join(columns)
        )
        db.execute(statement, (*adapted, values[-1]))

    @staticmethod
    def delete_schema_row(db: Any, object_type: str) -> None:
        db.execute(
            "DELETE FROM object_schemas WHERE object_type = %s", (object_type,)
        )

    @staticmethod
    def notebook_schema_rows(db: Any, notebook_id: str) -> List[dict]:
        return _compat_schema_rows(db.execute(
            "SELECT * FROM notebook_object_schemas WHERE notebook_id = %s",
            (notebook_id,),
        ).fetchall())

    @staticmethod
    def notebook_schema_row(
        db: Any, notebook_id: str, object_type: str
    ) -> "dict | None":
        rows = _compat_schema_rows(db.execute(
            "SELECT * FROM notebook_object_schemas "
            "WHERE notebook_id = %s AND object_type = %s",
            (notebook_id, object_type),
        ).fetchall())
        return rows[0] if rows else None

    @staticmethod
    def insert_notebook_schema(
        db: Any,
        *,
        notebook_id: str,
        object_type: str,
        plural: str,
        fields_json: str,
        primary: str,
        description: str,
        label: str,
        list_fields_json: str,
        source: str,
        status: str,
        rationale: str,
        created_by: str,
        now: str,
    ) -> None:
        db.execute(
            """
            INSERT INTO notebook_object_schemas
            (notebook_id, object_type, plural, fields, primary_field,
             description, label, list_fields, source, status, rationale,
             created_by, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                notebook_id,
                object_type,
                plural,
                jsonb(_json_document(fields_json, expected=list, field="schema fields")),
                primary,
                description,
                label,
                jsonb(_json_document(
                    list_fields_json, expected=list, field="schema list fields"
                )),
                source,
                status,
                rationale,
                created_by,
                normalize_timestamp(now),
                normalize_timestamp(now),
            ),
        )

    @staticmethod
    def update_notebook_schema_columns(
        db: Any,
        notebook_id: str,
        object_type: str,
        updates: List[str],
        values: List[object],
    ) -> None:
        allowed = {
            "plural", "fields", "primary_field", "description", "label",
            "list_fields", "status", "updated_at",
        }
        columns = []
        adapted = list(values)
        for index, update in enumerate(updates):
            column = update.split("=", 1)[0].strip()
            if column not in allowed:
                raise ValueError("unsupported notebook object schema update column")
            columns.append(sql.SQL("{}=%s").format(sql.Identifier(column)))
            if column in {"fields", "list_fields"}:
                adapted[index] = jsonb(_json_document(
                    adapted[index], expected=list, field=f"schema {column}"
                ))
            elif column == "updated_at":
                adapted[index] = normalize_timestamp(adapted[index])
        statement = sql.SQL(
            "UPDATE notebook_object_schemas SET {} "
            "WHERE notebook_id=%s AND object_type=%s"
        ).format(sql.SQL(",").join(columns))
        db.execute(statement, (*adapted, notebook_id, object_type))

    @staticmethod
    def delete_notebook_schema_row(
        db: Any, notebook_id: str, object_type: str
    ) -> None:
        db.execute(
            "DELETE FROM notebook_object_schemas "
            "WHERE notebook_id = %s AND object_type = %s",
            (notebook_id, object_type),
        )

    @staticmethod
    def notebook_schema_has_objects(
        db: Any, notebook_id: str, object_type: str
    ) -> bool:
        return db.execute(
            "SELECT 1 FROM knowledge_objects "
            "WHERE notebook_id = %s AND object_type = %s LIMIT 1",
            (notebook_id, object_type),
        ).fetchone() is not None

    # ------------------------------------------------- Task 26 primitives
    # The last facade SQL bodies, moved verbatim.  All connection-taking —
    # the facade keeps its `_connect`/`_write` boundaries (and the frozen
    # patch seats on them) and passes the possibly-wrapped connection down.

    @staticmethod
    def source_has_kg(db: Any, source_id: str) -> bool:
        """True iff the source has a complete KG graph.

        Direct/governance rows without extraction history remain compatible.
        When extraction history exists, the latest KG run must be completed so
        a failed legacy partial write cannot masquerade as resumable completion.
        """
        row = db.execute(
            "SELECT EXISTS("
            "  SELECT 1 FROM knowledge_objects ko "
            "  WHERE ko.source_id = %s AND ko.source_id != '' "
            "  AND COALESCE(("
            "    SELECT er.status FROM extraction_runs er "
            "    WHERE er.source_id=ko.source_id AND er.run_type='kg' "
            "    ORDER BY er.created_at DESC, er.ordinal DESC LIMIT 1"
            "  ), 'completed')='completed'"
            ") AS exists",
            (source_id,),
        ).fetchone()
        return bool(row["exists"])

    def insert_test_object(
        self,
        db: Any,
        notebook_id: str,
        object_type: str,
        payload: dict,
        source_id: str = "",
    ) -> str:
        """Test-only direct insert (facade `_test_insert_object` delegate).
        Ids/clock ride the compatibility seams — module `_new_id`/`_now`
        patches stay authoritative."""
        object_id = self.seams.new_id("ko")
        now = self.seams.now()
        db.execute(
            """INSERT INTO knowledge_objects
               (id, notebook_id, object_type, status, owner, payload, evidence,
                source_candidate_id, source_id, created_at, updated_at)
               VALUES (%s, %s, %s, 'approved', '', %s, '[]', NULL, %s, %s, %s)""",
            (
                object_id,
                notebook_id,
                object_type,
                jsonb(_json_document(payload, expected=dict, field="knowledge payload")),
                source_id,
                normalize_timestamp(now),
                normalize_timestamp(now),
            ),
        )
        return object_id

    @staticmethod
    def edge_centrality_source_rows(
        db: Any, notebook_id: str, max_nodes: int
    ) -> "tuple[List[str], List[dict]]":
        """Bounded (top-K by SQL degree) node ids + live relation dicts for the
        edge-betweenness loader (P0-3 semantics moved verbatim):

        1. Degree ranking via GROUP BY over non-rejected knowledge_relations —
           bounded by the distinct node count touched by an edge (isolated
           nodes cannot be edge endpoints and never rank).
        2. When bounded, only relations with BOTH endpoints in the top-K id
           set survive, loaded via json_each(%s) (pure read-side join — no
           thousand-placeholder IN and no temp-table write).
        3. Under-K graphs load every live relation plus the full object id
           set — identical result to the unbounded path.
        """
        degree: Dict[str, int] = {}
        for row in db.execute(
            "SELECT source_object_id AS n, COUNT(*) AS c FROM knowledge_relations "
            "WHERE notebook_id = %s AND review_status != 'rejected' "
            "GROUP BY source_object_id", (notebook_id,),
        ).fetchall():
            degree[row["n"]] = degree.get(row["n"], 0) + row["c"]
        for row in db.execute(
            "SELECT target_object_id AS n, COUNT(*) AS c FROM knowledge_relations "
            "WHERE notebook_id = %s AND review_status != 'rejected' "
            "GROUP BY target_object_id", (notebook_id,),
        ).fetchall():
            degree[row["n"]] = degree.get(row["n"], 0) + row["c"]

        if len(degree) > max_nodes:
            # Deterministic top-K: sort by (-degree, id) so ties break on a
            # stable, reproducible key.
            top_ids = [n for n, _ in sorted(
                degree.items(), key=lambda kv: (-kv[1], kv[0])
            )[:max_nodes]]
            rel_rows = db.execute(
                "SELECT r.id, r.source_object_id, r.target_object_id, "
                "r.edge_type, r.evidence FROM knowledge_relations r "
                "WHERE r.source_object_id=ANY(%s) AND r.target_object_id=ANY(%s) "
                "AND r.notebook_id = %s AND r.review_status != 'rejected'",
                (top_ids, top_ids, notebook_id),
            ).fetchall()
            node_ids = top_ids
        else:
            rel_rows = db.execute(
                "SELECT id, source_object_id, target_object_id, edge_type, "
                "evidence FROM knowledge_relations "
                "WHERE notebook_id = %s AND review_status != 'rejected'",
                (notebook_id,),
            ).fetchall()
            obj_rows = db.execute(
                "SELECT id, object_type FROM knowledge_objects WHERE notebook_id = %s",
                (notebook_id,),
            ).fetchall()
            node_ids = [dict(row) for row in obj_rows]

        if len(degree) > max_nodes:
            object_types = {
                row["id"]: row["object_type"]
                for row in db.execute(
                    "SELECT id, object_type FROM knowledge_objects "
                    "WHERE notebook_id = %s AND id = ANY(%s)",
                    (notebook_id, top_ids),
                ).fetchall()
            }
            node_ids = [
                {"id": object_id, "object_type": object_types.get(object_id, "")}
                for object_id in top_ids
            ]

        relations = [{
            "id": row["id"],
            "source_object_id": row["source_object_id"],
            "target_object_id": row["target_object_id"],
            "edge_type": row["edge_type"],
            "evidence": json_value(row["evidence"], []),
        } for row in rel_rows]
        return node_ids, relations

    @staticmethod
    def concept_cluster_detail_rows(
        db: Any,
        notebook_id: str,
        canonical_id: str,
        *,
        limit: Optional[int] = None,
        after: str = "",
    ) -> "tuple[List[dict], str]":
        """Cluster member rows (joined onto live knowledge_objects) plus the
        canonical name for one concept cluster. Keyset-paginated by
        ``member_object_id`` (KG-4 hub-cluster fix, R3·T-B2): a hub concept's
        member set has no bound otherwise, and this join returns full
        payload/evidence per row. ``ORDER BY ... COLLATE "C"`` pins byte-order
        comparison so this side's page order matches the SQLite mirror, whose
        default TEXT collation (BINARY) already compares UTF-8 text the same
        way — no equivalent COLLATE clause is needed there. The keyset
        comparison key carries the SAME explicit collation as the sort key
        (repo-wide convention — see e.g.
        ``postgres/maintenance.py::image_backfill_source_page`` and
        ``tests/test_image_backfill_transaction_guard.py``): today every id
        column is already declared ``text COLLATE "C"``
        (``migrations/0001_initial.sql``), so a bare comparison behaves
        identically and no runtime test can tell the two apart — this is
        defense in depth against a future column-level collation change
        silently splitting the compare/sort order and dropping members
        mid-page. ``limit=None`` keeps the legacy unbounded (but now
        deterministically ordered) read for internal callers that still need
        the full member set in one shot.

        R3 PR-B P2-1: when ``after`` is set, the WHERE clause also repeats
        the seek predicate on ``ko.id`` (``AND ko.id COLLATE "C" > %s``),
        provably redundant given the join condition ``ko.id=cc.member_object_id``
        — anything satisfying ``cc.member_object_id COLLATE "C" > after``
        satisfies ``ko.id COLLATE "C" > after`` too, and vice versa, so the
        result set is unchanged. What changes is the plan: without this,
        PostgreSQL's merge join can start its ``ko`` (knowledge_objects)
        side scan from the beginning of the table and filter down, which on
        a mid-cluster page means scanning every row up to the cursor before
        it can start returning members (measured: 200,603 rows / 104ms for a
        mid-page cursor). With the redundant predicate the planner has a
        seek condition on ``ko.id`` too and starts the scan at the cursor
        (603 rows / 2.2ms for the same page)."""
        query = (
            "SELECT cc.member_object_id, cc.canonical_name, ko.object_type, ko.payload, ko.evidence "
            "FROM concept_clusters cc "
            "JOIN knowledge_objects ko ON ko.id=cc.member_object_id "
            "WHERE cc.notebook_id=%s AND cc.canonical_id=%s AND ko.status!='deprecated'"
        )
        params: list = [notebook_id, canonical_id]
        if after:
            query += ' AND cc.member_object_id COLLATE "C" > %s AND ko.id COLLATE "C" > %s'
            params.append(after)
            params.append(after)
        query += ' ORDER BY cc.member_object_id COLLATE "C"'
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        cluster_rows = db.execute(query, tuple(params)).fetchall()
        name_row = db.execute(
            "SELECT canonical_name FROM concept_clusters WHERE notebook_id=%s AND canonical_id=%s LIMIT 1",
            (notebook_id, canonical_id),
        ).fetchone()
        return (
            _compat_rows(cluster_rows, payload=True, evidence=True),
            (name_row["canonical_name"] if name_row else ""),
        )

    @staticmethod
    def concept_cluster_member_total(db: Any, notebook_id: str, canonical_id: str) -> int:
        """COUNT with the SAME predicate shape as concept_cluster_detail_rows
        (JOIN knowledge_objects ... AND ko.status!='deprecated') — design
        review B8 (hard): a bare ``COUNT(*) FROM concept_clusters`` would
        count deprecated members and make pagination look like it never
        reaches the end."""
        row = db.execute(
            "SELECT COUNT(*) AS c FROM concept_clusters cc "
            "JOIN knowledge_objects ko ON ko.id=cc.member_object_id "
            "WHERE cc.notebook_id=%s AND cc.canonical_id=%s AND ko.status!='deprecated'",
            (notebook_id, canonical_id),
        ).fetchone()
        return int(row["c"]) if row else 0

    @staticmethod
    def concept_neighbor_rows(
        db: Any, notebook_id: str, canonical_id: str, member_ids: "List[str]",
        *, batch_size: int = 900,
    ) -> "tuple[List[dict], Dict[str, dict]]":
        """Relations touching the member set plus the batch-read non-member
        endpoint objects: returns (rel_edges, objects_by_id).

        R3·T-B2 pagination made ``member_ids`` PAGE-local (it used to be the
        whole cluster). A relation whose other endpoint is a member of the
        SAME cluster on a DIFFERENT page therefore now looks, to the
        page-local ``member_set`` check below, exactly like a genuine
        external neighbor: it is not "in member_set" either. The legacy
        unbounded read never had this problem — its member_set covered the
        WHOLE cluster, so a same-cluster endpoint could never fail that
        check. This is restoring that semantics, not adding a new filter:
        before hydrating candidates, one extra batched membership probe
        drops any candidate that ``concept_clusters`` says belongs to THIS
        canonical id, so a dense hub (a cluster with thousands/millions of
        members) can no longer force this function to hydrate full
        payload/evidence for an unbounded number of cross-page cluster-mates
        only to have the caller discard them via the (still-present, still
        doing its own independent job for cross-CLUSTER concept neighbors)
        ``object_type != 'concept'`` filter downstream.

        Membership-probe query shape: ``SELECT member_object_id FROM
        concept_clusters WHERE notebook_id=%s AND canonical_id=%s AND
        member_object_id = ANY(%s)``, batched at ``batch_size`` — same "PK
        `= ANY`, not a range predicate" safe form as
        ``GovernanceStore.sweep_orphan_clusters_page``'s bounded delete (see
        that method's docstring for the measured planner contrast): all
        three predicate columns are plain equalities against the
        ``idx_clusters_nb_canonical_member(notebook_id, canonical_id,
        member_object_id)`` composite index (migration 0043), which is an
        exact match and a strict superset of every other index on this
        table, so there is no less-specific competitor index the planner
        could degrade to. ``ANY(%s)`` itself carries no bind-parameter cap
        on PostgreSQL (a single array parameter) — batching here is for
        memory/latency pacing, matching the SQLite twin's batching (which
        DOES need it: ``SQLITE_MAX_VARIABLE_NUMBER``).

        The attached-candidate hydration query below is unconditionally
        batched too (same ``batch_size``) for the same pacing reason, and to
        keep both backends' behavior aligned now that the candidate set this
        function hydrates is no longer implicitly bounded by "one page's
        relations" (a hub's cross-page cluster-mates used to inflate it
        without limit before the exclusion above)."""
        member_set = set(member_ids)
        placeholders = ",".join("%s" for _ in member_set)
        member_list = list(member_set)
        rels_out = db.execute(
            f"SELECT source_object_id, target_object_id, edge_type "
            f"FROM knowledge_relations WHERE notebook_id=%s AND source_object_id IN ({placeholders})",
            [notebook_id] + member_list,
        ).fetchall()
        rels_in = db.execute(
            f"SELECT source_object_id, target_object_id, edge_type "
            f"FROM knowledge_relations WHERE notebook_id=%s AND target_object_id IN ({placeholders})",
            [notebook_id] + member_list,
        ).fetchall()

        attached_ids: set = set()
        rel_edges: List[dict] = []
        for rel in rels_out:
            other = rel["target_object_id"]
            if other not in member_set:
                attached_ids.add(other)
                rel_edges.append({"other": other, "edge_type": rel["edge_type"]})
        for rel in rels_in:
            other = rel["source_object_id"]
            if other not in member_set:
                attached_ids.add(other)
                rel_edges.append({"other": other, "edge_type": rel["edge_type"]})

        if attached_ids:
            candidates = list(attached_ids)
            same_cluster_ids: set = set()
            for offset in range(0, len(candidates), batch_size):
                batch = candidates[offset:offset + batch_size]
                same_cluster_ids.update(
                    row["member_object_id"] for row in db.execute(
                        "SELECT member_object_id FROM concept_clusters "
                        "WHERE notebook_id=%s AND canonical_id=%s "
                        "AND member_object_id = ANY(%s)",
                        (notebook_id, canonical_id, batch),
                    ).fetchall()
                )
            attached_ids -= same_cluster_ids

        by_other: Dict[str, dict] = {}
        if attached_ids:
            attached_list = list(attached_ids)
            attached_rows: List[Any] = []
            for offset in range(0, len(attached_list), batch_size):
                batch = attached_list[offset:offset + batch_size]
                attached_placeholders = ",".join("%s" for _ in batch)
                attached_rows.extend(db.execute(
                    f"SELECT id, object_type, payload, evidence FROM knowledge_objects "
                    f"WHERE id IN ({attached_placeholders}) AND status!='deprecated'",
                    batch,
                ).fetchall())
            by_other = {
                row["id"]: {
                    "id": row["id"],
                    "object_type": row["object_type"],
                    "payload": json_value(row["payload"], {}),
                    "evidence": json_value(row["evidence"], []),
                }
                for row in attached_rows
            }
        return rel_edges, by_other
