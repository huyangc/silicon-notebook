#!/usr/bin/env python3
"""Read-only PostgreSQL hot-path diagnostics for a production self-audit.

    python3 scripts/diag_pg_hotpaths.py
    python3 scripts/diag_pg_hotpaths.py --notebook-id nb-xxxxxxxx
    python3 scripts/diag_pg_hotpaths.py --deep      # + the 4 heavy probes (each can take ~30s)

Shape mirrors ``scripts/build_postgres_retrieval_indexes.py`` (argparse,
``--database-url-env`` defaulting to ``DATABASE_URL``, the URL is never
printed, ``database_identity(...)`` must resolve to ``postgresql``, exit-code
convention) and the output/redaction conventions of ``scripts/diag_slow.py``
(the SQLite hot-path diagnostic): one summary line per statement, no object
text or names — only statement name, plan shape (Seq Scan vs which index),
actual row count, actual ms, and shared hit/read buffer counts.

Expected duration — **even the default (non-``--deep``) run is not cheap**:
the row-count overview (step 3 below) runs a plain, unindexed-friendly
``SELECT COUNT(*)`` over every hot table database-wide, and on a ~9M-row
table that alone is minute-scale, not the sub-second latency the EXPLAIN
probes in step 2 report for their own (notebook-scoped, indexed) predicates.
``--deep`` adds four heavier probes — the two ILIKE full-text searches and the
two missing-vector anti-join COUNTs (``missing_chunk_vectors_count`` /
``missing_element_vectors_count``) that Z7 pulled out of the
``backfill-vectors`` admission path for exactly this reason: a cold database
can make either of those two COUNTs alone take tens of seconds. Run ``--deep``
outside production peak hours.

What this does, in order:

  1. Picks a target notebook (``--notebook-id``, or the one with the most
     ``knowledge_objects`` rows — a single read-only ``GROUP BY ... LIMIT 1``
     that is itself a full scan of ``knowledge_objects``, printed as a warning
     when ``--notebook-id`` is omitted so a slow first line isn't mistaken for
     a hang).
  2. Runs ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` over the hot statement
     family named below (``HOT_STATEMENTS``), plus, only with ``--deep``, the
     four heavier probes (``DEEP_STATEMENTS``) described above.
  3. Prints a plain ``SELECT COUNT(*)`` row-count overview of the same hot
     tables (``TABLE_ROW_COUNTS``, database-wide, not notebook-scoped) — see
     "Expected duration" above, this runs unconditionally, not just under
     ``--deep``.
  4. Checks index coverage for the composite/functional keys the statements
     above rely on (``INDEX_AUDIT_CANDIDATES``) via ``pg_indexes``, plus a
     generic "does every foreign key on these tables have a covering index"
     scan (unindexed FKs make cascade/consistency checks on the referenced
     table pay a full scan of the referencing table).
  5. Session safety: ``SET default_transaction_read_only = on`` is the very
     first statement issued on the connection (inside ``_connect()``, before
     notebook auto-selection or anything else runs); ``--deep`` statements
     optionally run under a scoped ``statement_timeout``
     (``--deep-statement-timeout-ms``, 0 = leave the database default). Every
     statement is independent (autocommit, one implicit transaction each) —
     one failure is summarized and diagnosis continues with the next
     statement, but the process exit code is 1 if any statement failed (0
     only when every statement returned a row cleanly).

SQL provenance — every predicate below is copied verbatim (not reinvented)
from the PostgreSQL store file/method named in its comment; see that file for
the full production statement (some carry extra JOINs/SELECT columns this
diagnostic strips because it only needs the WHERE/GROUP BY shape that decides
which index the planner can use):

  - ko_active_count            app/repositories/postgres/unified_kg_store.py
                                UnifiedKgStore.finish_rebuild_state
  - ko_type_status_group       app/repositories/postgres/knowledge_counts_cache.py
                                type_status_counts (the knowledge_counts_cache
                                cold-load path this diagnostic is timing)
  - chunks_count               app/repositories/postgres/knowledge_counts_cache.py
                                chunk_count
  - canonical_relations_count  app/repositories/postgres/unified_kg_store.py
                                UnifiedKgStore.canonical_relations_count
  - knowledge_relations_review_count
                                app/repositories/postgres/governance_store.py
                                edge review queue predicate
                                (``notebook_id=%s AND review_status!='rejected'``)
  - concept_clusters_canonical_id_probe
                                app/repositories/postgres/knowledge_store.py
                                (canonical_name lookup by (notebook_id, canonical_id))
  - concept_clusters_lower_name_probe
                                app/repositories/postgres/unified_kg_store.py
                                UnifiedKgStore.resolve_focal
  - sources_hidden_count       app/repositories/postgres/source_store.py
                                PostgresSourceStore.hidden_source_ids
                                (owner-scoped EXISTS clause dropped — this
                                diagnostic only needs the source_type IN (...)
                                predicate's index shape, not per-owner rows)
  - source_elements_join_sources_count
                                app/repositories/postgres/search.py
                                notebook_element_rows (search leg; ILIKE
                                clause dropped, this counts its input size)
  - missing_chunk_vectors_count (--deep only)
                                app/repositories/postgres/maintenance.py
                                PostgresMaintenanceAdapter.count_missing_chunk_vectors
                                (moved to DEEP_STATEMENTS: this is one of the
                                two anti-join COUNTs Z7 pulled out of the
                                backfill-vectors admission path for being a
                                cold-database, tens-of-seconds-plus query)
  - missing_element_vectors_count (--deep only)
                                app/repositories/postgres/maintenance.py
                                PostgresMaintenanceAdapter.count_missing_element_vectors
                                (moved to DEEP_STATEMENTS, same reason as
                                missing_chunk_vectors_count above)
  - chunks_exact_ilike_probe (--deep only)
                                app/repositories/postgres/search.py
                                chunk_exact_candidate_rows
  - knowledge_payload_ilike_probe (--deep only)
                                app/repositories/postgres/search.py
                                notebook_knowledge_rows

Index-audit note: the task briefing that produced this script referenced a
prior "production self-audit" naming six specific missing composite/functional
indexes plus three unindexed reverse foreign keys. That audit artifact could
not be located anywhere in this repository (docs/, git history, or open
PRs/plans) at implementation time, so rather than hard-code an unverifiable
number this script instead audits, live, every composite/functional index the
statements above actually rely on (``INDEX_AUDIT_CANDIDATES`` — however many
that is) and every foreign key on the hot tables (a generic scan, not a fixed
count) — see the module docstring of ``build_postgres_retrieval_indexes.py``
for the more rigorous catalog-shape matcher this heuristic deliberately does
not reuse (it only needs to report, not to build or verify DDL).
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.core.database_url import database_identity  # noqa: E402
from app.repositories.like_pattern import escape_like_pattern  # noqa: E402
from app.repositories.text_whitespace import PY_WHITESPACE  # noqa: E402


_EXPLAIN_PREFIX = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
_DEEP_ILIKE_LIMIT = 50


class DiagConfigError(RuntimeError):
    """Credential-free, operator-actionable configuration/connection failure."""


@dataclass(frozen=True)
class StatementSpec:
    name: str
    sql: str
    params: tuple[str, ...] = ()
    deep: bool = False
    provenance: str = ""


# ---------------------------------------------------------------------------
# Hot statement family — see module docstring "SQL provenance" for the exact
# source file/method each predicate was copied from.
# ---------------------------------------------------------------------------

HOT_STATEMENTS: tuple[StatementSpec, ...] = (
    StatementSpec(
        "ko_active_count",
        _EXPLAIN_PREFIX
        + "SELECT COUNT(*) AS c FROM knowledge_objects "
        "WHERE notebook_id=%(notebook_id)s AND status!='deprecated'",
        ("notebook_id",),
        provenance="unified_kg_store.py:finish_rebuild_state",
    ),
    StatementSpec(
        "ko_type_status_group",
        _EXPLAIN_PREFIX
        + "SELECT object_type, status, COUNT(*) AS c FROM knowledge_objects "
        "WHERE notebook_id=%(notebook_id)s GROUP BY object_type, status",
        ("notebook_id",),
        provenance="knowledge_counts_cache.py:type_status_counts",
    ),
    StatementSpec(
        "chunks_count",
        _EXPLAIN_PREFIX
        + "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id=%(notebook_id)s",
        ("notebook_id",),
        provenance="knowledge_counts_cache.py:chunk_count",
    ),
    StatementSpec(
        "canonical_relations_count",
        _EXPLAIN_PREFIX
        + "SELECT COUNT(*) AS c FROM canonical_relations WHERE notebook_id=%(notebook_id)s",
        ("notebook_id",),
        provenance="unified_kg_store.py:canonical_relations_count",
    ),
    StatementSpec(
        "knowledge_relations_review_count",
        _EXPLAIN_PREFIX
        + "SELECT COUNT(*) AS c FROM knowledge_relations "
        "WHERE notebook_id=%(notebook_id)s AND review_status!='rejected'",
        ("notebook_id",),
        provenance="governance_store.py: edge review queue predicate",
    ),
    StatementSpec(
        "concept_clusters_canonical_id_probe",
        _EXPLAIN_PREFIX
        + "SELECT canonical_name FROM concept_clusters "
        "WHERE notebook_id=%(notebook_id)s AND canonical_id=%(canonical_id)s LIMIT 1",
        ("notebook_id", "canonical_id"),
        provenance="knowledge_store.py: concept_clusters(notebook_id, canonical_id) lookup "
        "— expected Seq/whole-segment scan, evidencing the missing composite index",
    ),
    StatementSpec(
        "concept_clusters_lower_name_probe",
        _EXPLAIN_PREFIX
        + "SELECT canonical_id FROM concept_clusters "
        "WHERE notebook_id=%(notebook_id)s AND lower(canonical_name)=%(canonical_name_lower)s "
        "GROUP BY canonical_id ORDER BY COUNT(*) DESC, canonical_id COLLATE \"C\" ASC LIMIT 1",
        ("notebook_id", "canonical_name_lower"),
        provenance="unified_kg_store.py:resolve_focal "
        "— expected Seq/whole-segment scan, evidencing the missing functional index",
    ),
    StatementSpec(
        "sources_hidden_count",
        _EXPLAIN_PREFIX
        + "SELECT COUNT(*) AS c FROM sources "
        "WHERE notebook_id=%(notebook_id)s AND source_type IN ('memory','knowhow')",
        ("notebook_id",),
        provenance="source_store.py:hidden_source_ids "
        "(owner-scoped EXISTS clause dropped for this scale probe)",
    ),
    StatementSpec(
        "source_elements_join_sources_count",
        _EXPLAIN_PREFIX
        + "SELECT COUNT(*) AS c FROM source_elements se JOIN sources s ON s.id=se.source_id "
        "WHERE s.notebook_id=%(notebook_id)s AND s.source_type NOT IN ('memory','knowhow')",
        ("notebook_id",),
        provenance="search.py:notebook_element_rows (ILIKE clause dropped — this is its input size)",
    ),
)

DEEP_STATEMENTS: tuple[StatementSpec, ...] = (
    StatementSpec(
        "missing_chunk_vectors_count",
        _EXPLAIN_PREFIX
        + "SELECT COUNT(*) AS c FROM chunks c WHERE c.notebook_id=%(notebook_id)s AND NOT EXISTS "
        "(SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.id)",
        ("notebook_id",),
        deep=True,
        provenance="maintenance.py:count_missing_chunk_vectors "
        "(--deep only: this anti-join COUNT is one of the two Z7 pulled out of "
        "the backfill-vectors admission path — a cold database can make it "
        "alone take tens of seconds)",
    ),
    StatementSpec(
        "missing_element_vectors_count",
        _EXPLAIN_PREFIX
        + "SELECT COUNT(*) AS c FROM source_elements e "
        "JOIN sources s ON s.id = e.source_id "
        "WHERE s.notebook_id=%(notebook_id)s "
        "AND s.source_type NOT IN ('memory', 'knowhow') "
        "AND btrim(e.text, %(whitespace)s) != '' "
        "AND NOT EXISTS (SELECT 1 FROM element_embeddings v WHERE v.element_id = e.id)",
        ("notebook_id", "whitespace"),
        deep=True,
        provenance="maintenance.py:count_missing_element_vectors "
        "(--deep only, same reason as missing_chunk_vectors_count above; also "
        "forces a TOAST read for the per-row btrim non-empty check)",
    ),
    StatementSpec(
        "chunks_exact_ilike_probe",
        _EXPLAIN_PREFIX
        + "SELECT id AS candidate_id, source_id, section_path, "
        "public.similarity(text, %(needle)s) AS candidate_similarity "
        f"FROM chunks WHERE notebook_id=%(notebook_id)s AND text ILIKE %(pattern)s "
        f"ORDER BY candidate_similarity DESC, id COLLATE \"C\" LIMIT {_DEEP_ILIKE_LIMIT}",
        ("notebook_id", "needle", "pattern"),
        deep=True,
        provenance="search.py:chunk_exact_candidate_rows (exact_lookup shape)",
    ),
    StatementSpec(
        "knowledge_payload_ilike_probe",
        _EXPLAIN_PREFIX
        + "SELECT id, object_type, payload FROM knowledge_objects "
        "WHERE notebook_id=%(notebook_id)s AND status!='deprecated' AND "
        "((payload ->> 'name') COLLATE \"C\" ILIKE %(pattern)s OR "
        "(payload::text) COLLATE \"C\" ILIKE %(pattern)s) "
        f"ORDER BY ordinal LIMIT {_DEEP_ILIKE_LIMIT}",
        ("notebook_id", "pattern"),
        deep=True,
        provenance="search.py:notebook_knowledge_rows (search knowledge leg)",
    ),
)

# Database-wide (not notebook-scoped) row counts for the same hot tables —
# plain SELECT COUNT(*), no EXPLAIN wrapper.
TABLE_ROW_COUNTS: tuple[StatementSpec, ...] = tuple(
    StatementSpec(f"rowcount_{table}", f"SELECT COUNT(*) AS c FROM {table}")
    for table in (
        "knowledge_objects",
        "chunks",
        "canonical_relations",
        "knowledge_relations",
        "concept_clusters",
        "sources",
        "source_elements",
        "chunk_embeddings",
        "element_embeddings",
    )
)

ALL_STATEMENTS: tuple[StatementSpec, ...] = HOT_STATEMENTS + DEEP_STATEMENTS + TABLE_ROW_COUNTS


def select_statements(*, deep: bool) -> tuple[StatementSpec, ...]:
    """The statements a run should execute — deep heavy probes opt-in only."""
    return HOT_STATEMENTS + (DEEP_STATEMENTS if deep else ())


# ---------------------------------------------------------------------------
# Index-coverage audit — see the module docstring's "Index-audit note" for why
# this is a live, derived scan rather than a hard-coded list.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexAuditCandidate:
    label: str
    table: str
    key_fragments: tuple[str, ...]
    serves: str


INDEX_AUDIT_CANDIDATES: tuple[IndexAuditCandidate, ...] = (
    IndexAuditCandidate(
        "concept_clusters(notebook_id, canonical_id)", "concept_clusters",
        ("notebook_id", "canonical_id"), "concept_clusters_canonical_id_probe",
    ),
    IndexAuditCandidate(
        "concept_clusters(notebook_id, lower(canonical_name))", "concept_clusters",
        ("notebook_id", "lower(canonical_name)"), "concept_clusters_lower_name_probe",
    ),
    IndexAuditCandidate(
        "sources(notebook_id, source_type)", "sources",
        ("notebook_id", "source_type"), "sources_hidden_count",
    ),
    IndexAuditCandidate(
        "knowledge_objects(notebook_id, status)", "knowledge_objects",
        ("notebook_id", "status"), "ko_active_count",
    ),
    IndexAuditCandidate(
        "knowledge_objects(notebook_id, object_type, status)", "knowledge_objects",
        ("notebook_id", "object_type", "status"), "ko_type_status_group",
    ),
    IndexAuditCandidate(
        "chunks(notebook_id)", "chunks", ("notebook_id",), "chunks_count",
    ),
    IndexAuditCandidate(
        "canonical_relations(notebook_id, ...)", "canonical_relations",
        ("notebook_id",), "canonical_relations_count",
    ),
    IndexAuditCandidate(
        "knowledge_relations(notebook_id, review_status)", "knowledge_relations",
        ("notebook_id", "review_status"), "knowledge_relations_review_count",
    ),
    IndexAuditCandidate(
        "chunk_embeddings(chunk_id)", "chunk_embeddings",
        ("chunk_id",), "missing_chunk_vectors_count",
    ),
    IndexAuditCandidate(
        "element_embeddings(element_id)", "element_embeddings",
        ("element_id",), "missing_element_vectors_count",
    ),
)


def _extract_index_columns(indexdef: str) -> list[str]:
    """Parse the column-expression list out of a ``pg_indexes.indexdef``
    string, e.g. ``... USING btree (notebook_id, lower(canonical_name))`` ->
    ``["notebook_id", "lower(canonical_name)"]``.

    The ``WHERE`` predicate (partial indexes) is dropped first — a live smoke
    test caught a real false positive where a partial index's predicate
    happened to spell one of the key fragments (``idx_sources_visible_identity
    ON sources(notebook_id, created_at, id) WHERE source_type <> ALL(...)``
    mentions ``source_type`` in its predicate, not its key). Parenthesis
    depth is tracked (not a flat split) so functional/expression columns like
    ``((payload ->> 'name') COLLATE "C")`` are not shredded by their own
    internal commas/parens.
    """
    body = re.split(r"\bwhere\b", indexdef, maxsplit=1, flags=re.IGNORECASE)[0]
    start = body.find("(")
    if start < 0:
        return []
    depth = 0
    end = -1
    for i in range(start, len(body)):
        if body[i] == "(":
            depth += 1
        elif body[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return []
    inner = body[start + 1:end]
    columns: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            columns.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        columns.append("".join(current).strip())
    return columns


def _index_covers(indexdefs: list[str], key_fragments: tuple[str, ...]) -> bool:
    """Does any indexdef have ``key_fragments`` as its leading columns, in
    order? Each parsed column is compared by substring containment (not
    equality) so collation/cast noise like ``notebook_id`` vs
    ``(notebook_id)::text`` still matches.

    This checks a true **prefix** — unlike a plain ordered-substring scan of
    the whole indexdef text, ``(notebook_id, source_type)`` does NOT match
    ``(notebook_id, parse_status, source_type)``: ``parse_status`` sits
    between the two key columns, so PostgreSQL cannot use ``source_type`` as
    an index condition without also constraining ``parse_status`` — a real
    gap this diagnostic's first cut at this heuristic missed. Still advisory,
    not the strict catalog shape-matcher ``retrieval_indexes.py`` uses to
    decide whether it is safe to build/drop DDL (no opclass/collation check).
    """
    for indexdef in indexdefs:
        columns = [c.lower() for c in _extract_index_columns(indexdef)]
        if len(columns) < len(key_fragments):
            continue
        if all(
            fragment.lower() in columns[i] for i, fragment in enumerate(key_fragments)
        ):
            return True
    return False


def audit_indexes(connection) -> list[dict]:
    tables = sorted({candidate.table for candidate in INDEX_AUDIT_CANDIDATES})
    defs_by_table: dict[str, list[str]] = {}
    for table in tables:
        rows = connection.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=%(table)s",
            {"table": table},
        ).fetchall()
        defs_by_table[table] = [row["indexdef"] for row in rows]
    out = []
    for candidate in INDEX_AUDIT_CANDIDATES:
        covered = _index_covers(defs_by_table.get(candidate.table, []), candidate.key_fragments)
        out.append({
            "label": candidate.label,
            "serves": candidate.serves,
            "state": "存在" if covered else "缺失",
        })
    return out


# Tables this diagnostic cares about for the reverse-FK (unindexed foreign
# key) scan — a foreign key without a covering index on its own referencing
# column(s) means any DELETE/UPDATE on the *referenced* table pays a full
# scan of the *referencing* table to check/cascade.
_FK_AUDIT_TABLES: tuple[str, ...] = (
    "knowledge_objects", "chunks", "canonical_relations", "knowledge_relations",
    "concept_clusters", "sources", "source_elements", "chunk_embeddings",
    "element_embeddings",
)

_FK_SQL = (
    "SELECT c.conname AS name, c.conrelid::regclass::text AS referencing_table, "
    "c.confrelid::regclass::text AS referenced_table, "
    "ARRAY(SELECT att.attname FROM unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord) "
    "JOIN pg_attribute att ON att.attrelid=c.conrelid AND att.attnum=u.attnum "
    "ORDER BY u.ord) AS fk_columns "
    "FROM pg_constraint c "
    "WHERE c.contype='f' AND c.connamespace='public'::regnamespace "
    "AND c.conrelid::regclass::text = ANY(%(tables)s)"
)


def audit_reverse_fk_indexes(connection) -> list[dict]:
    fk_rows = connection.execute(_FK_SQL, {"tables": list(_FK_AUDIT_TABLES)}).fetchall()
    out = []
    for row in fk_rows:
        table = row["referencing_table"]
        fk_columns = tuple(row["fk_columns"] or ())
        if not fk_columns:
            continue
        index_rows = connection.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=%(table)s",
            {"table": table},
        ).fetchall()
        defs = [r["indexdef"] for r in index_rows]
        covered = _index_covers(defs, fk_columns)
        out.append({
            "name": row["name"],
            "table": table,
            "columns": ",".join(fk_columns),
            "state": "存在" if covered else "缺失",
        })
    return out


# ---------------------------------------------------------------------------
# EXPLAIN (FORMAT JSON) parsing / rendering — redaction lives here: only
# structural plan fields (Node Type, Relation Name, Index Name — all schema
# identifiers, never object content) and numeric counters are ever read out
# of the plan. Filter / Index Cond / Recheck Cond / Output / Sort Key (which
# embed the literal bound parameter values) are never inspected or printed.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanSummary:
    actual_rows: "int | None" = None
    execution_ms: "float | None" = None
    seq_scan_relations: tuple[str, ...] = ()
    index_names: tuple[str, ...] = ()
    shared_hit_blocks: int = 0
    shared_read_blocks: int = 0
    error: "str | None" = None


def _walk_plan_nodes(node: dict, acc: dict) -> None:
    """Recurse the whole plan tree for scan/index *labels* only. Buffers are
    deliberately **not** accumulated here — see ``parse_explain_json`` for why
    they are read once, off the root node."""
    node_type = str(node.get("Node Type", ""))
    if node_type == "Seq Scan":
        acc["seq_scan_relations"].append(str(node.get("Relation Name", "?")))
    index_name = node.get("Index Name")
    if index_name:
        acc["index_names"].append(str(index_name))
    for child in node.get("Plans", ()) or ():
        if isinstance(child, dict):
            _walk_plan_nodes(child, acc)


def parse_explain_json(raw) -> PlanSummary:
    """Parse one ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` result value
    (already-decoded list/dict, or its JSON text) into a redacted summary.

    Buffers come from the **root** plan node only, not a sum across the tree:
    PostgreSQL's BUFFERS output already reports each node's ``Shared Hit/Read
    Blocks`` as cumulative over that node's entire subtree (the root node's
    figure already counts every descendant's buffers), so summing every
    node's counters double-, triple-, ...-counts pages that were touched deep
    in a multi-join plan once per ancestor on the path back to the root."""
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            return PlanSummary(error="unexpected_explain_shape")
        plan = payload.get("Plan")
        if not isinstance(plan, dict):
            return PlanSummary(error="missing_plan_node")
        acc: dict = {"seq_scan_relations": [], "index_names": []}
        _walk_plan_nodes(plan, acc)
        execution_ms = payload.get("Execution Time")
        actual_rows = plan.get("Actual Rows")
        shared_hit = plan.get("Shared Hit Blocks")
        shared_read = plan.get("Shared Read Blocks")
        return PlanSummary(
            actual_rows=int(actual_rows) if isinstance(actual_rows, (int, float)) else None,
            execution_ms=float(execution_ms) if isinstance(execution_ms, (int, float)) else None,
            seq_scan_relations=tuple(dict.fromkeys(acc["seq_scan_relations"])),
            index_names=tuple(dict.fromkeys(acc["index_names"])),
            shared_hit_blocks=int(shared_hit) if isinstance(shared_hit, (int, float)) else 0,
            shared_read_blocks=int(shared_read) if isinstance(shared_read, (int, float)) else 0,
        )
    except Exception:  # noqa: BLE001 — diagnostics must fail closed, never raise
        return PlanSummary(error="explain_parse_failed")


def _fmt_ms(value: "float | None") -> str:
    if value is None:
        return "n/a"
    return f"{value/1000:.1f}s" if value >= 1000 else f"{value:.0f}ms"


def format_summary_line(name: str, summary: PlanSummary) -> str:
    if summary.error:
        return f"  {name:38} error:{summary.error}"
    if summary.seq_scan_relations:
        plan_desc = "SeqScan(" + ",".join(summary.seq_scan_relations) + ")"
    elif summary.index_names:
        plan_desc = "Index(" + ",".join(summary.index_names) + ")"
    else:
        plan_desc = "(no scan/index node found)"
    rows = summary.actual_rows if summary.actual_rows is not None else "n/a"
    return (
        f"  {name:38} {plan_desc:40} rows={rows:<10} "
        f"ms={_fmt_ms(summary.execution_ms):<8} "
        f"buffers(hit={summary.shared_hit_blocks},read={summary.shared_read_blocks})"
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _connect(database_url: str):
    """Open the connection and put the session into read-only mode as the
    **very first statement issued on it** — before ``_pick_notebook_id``'s
    auto-selection query (which ``main()`` runs immediately after this
    returns) or anything else. This used to be set inside ``run_diagnostics``
    instead, which ``main()`` only calls *after* auto-selecting the notebook —
    leaving that one query to run against a still-writable session."""
    if not database_url:
        raise DiagConfigError("database URL is required")
    try:
        connection = psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
            application_name="silicon-notebook-diag-pg-hotpaths",
            connect_timeout=10,
        )
    except Exception:
        raise DiagConfigError("postgres_connection_failed") from None
    connection.execute("SET default_transaction_read_only = on")
    return connection


def _pick_notebook_id(connection) -> "str | None":
    row = connection.execute(
        "SELECT notebook_id, COUNT(*) AS c FROM knowledge_objects "
        "GROUP BY notebook_id ORDER BY c DESC LIMIT 1"
    ).fetchone()
    return row["notebook_id"] if row else None


def _sample_canonical(connection, notebook_id: str) -> "tuple[str, str] | None":
    row = connection.execute(
        "SELECT canonical_id, canonical_name FROM concept_clusters "
        "WHERE notebook_id=%(notebook_id)s LIMIT 1",
        {"notebook_id": notebook_id},
    ).fetchone()
    if not row:
        return None
    return str(row["canonical_id"]), str(row["canonical_name"] or "")


def run_statement(connection, spec: StatementSpec, params: dict) -> PlanSummary:
    try:
        row = connection.execute(spec.sql, params).fetchone()
    except Exception as exc:  # noqa: BLE001 — one bad statement must not abort the run
        return PlanSummary(error=type(exc).__name__)
    if row is None:
        return PlanSummary(error="no_row_returned")
    # dict_row: EXPLAIN (FORMAT JSON) names its sole column "QUERY PLAN".
    raw = row.get("QUERY PLAN") if isinstance(row, dict) else None
    if raw is None:
        return PlanSummary(error="missing_query_plan_column")
    return parse_explain_json(raw)


def run_row_count(connection, spec: StatementSpec) -> "int | str":
    try:
        row = connection.execute(spec.sql).fetchone()
        return int(row["c"])
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}"


def _section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(1, 68 - len(title)))


def run_diagnostics(connection, *, notebook_id: str, deep: bool, deep_timeout_ms: int) -> int:
    """Run every hot-statement EXPLAIN + row-count + index-audit section for
    ``notebook_id`` and print a one-line-per-statement report. The read-only
    session guard is **not** set here — ``main()`` opens the connection via
    ``_connect()``, which sets it as the very first statement issued, before
    even the notebook auto-selection query this function's caller may have
    run. Returns ``1`` if any statement failed (even though the run continues
    through every remaining statement and the summary section always prints),
    ``0`` only when every statement returned a row cleanly."""
    _section("目标 notebook")
    print(f"  notebook_id={notebook_id!r} 已脱敏? 否 — 这是运维自选的诊断目标本身，"
          "不是对象内容；后续所有语句只打计数/计划/ms")

    params = {"notebook_id": notebook_id, "whitespace": PY_WHITESPACE}
    sample = _sample_canonical(connection, notebook_id)
    if sample is not None:
        canonical_id, canonical_name = sample
        params["canonical_id"] = canonical_id
        params["canonical_name_lower"] = canonical_name.lower()
    else:
        print("  该 notebook 无 concept_clusters 行 — 跳过两条 concept_clusters 探查")

    failures = 0
    _section("热语句族 EXPLAIN(ANALYZE, BUFFERS)")
    for spec in select_statements(deep=False):
        if not set(spec.params).issubset(params):
            print(f"  {spec.name:38} skip: 该 notebook 无法构造探查参数")
            continue
        summary = run_statement(connection, spec, {k: params[k] for k in spec.params})
        if summary.error:
            failures += 1
        print(format_summary_line(spec.name, summary))

    if deep:
        _section("--deep 重项 EXPLAIN(ANALYZE, BUFFERS)（4 条，单条可能真跑到 30s 级）")
        print("  ⚠ 正在执行可能全表扫描的探针（两条 ILIKE 全文探针 + 两条缺向量反连接 "
              "COUNT），请勿在生产高峰期运行")
        needle = "silicon-notebook-diag-probe"
        pattern = f"%{escape_like_pattern(needle)}%"
        deep_params = dict(params)
        deep_params["needle"] = needle
        deep_params["pattern"] = pattern
        if deep_timeout_ms > 0:
            connection.execute(
                "SELECT set_config('statement_timeout', %(ms)s, false)",
                {"ms": str(int(deep_timeout_ms))},
            )
        try:
            for spec in DEEP_STATEMENTS:
                if not set(spec.params).issubset(deep_params):
                    print(f"  {spec.name:38} skip: 缺探查参数")
                    continue
                summary = run_statement(
                    connection, spec, {k: deep_params[k] for k in spec.params}
                )
                if summary.error:
                    failures += 1
                print(format_summary_line(spec.name, summary))
        finally:
            if deep_timeout_ms > 0:
                connection.execute("SELECT set_config('statement_timeout', '0', false)")
    else:
        _section("--deep 重项（默认跳过）")
        print("  (未加 --deep — 两条 ILIKE 全表探针 + 两条缺向量反连接 COUNT，"
              "单条可能真跑到 30s 级，默认不跑)")

    _section("热表行数一览（库内全量，非按 notebook）")
    print("  ⚠ 以下均为无 notebook 过滤的全表 SELECT COUNT(*)——即便不加 --deep，"
          "9M 行级的表这一节也可能是分钟级，不是上面 EXPLAIN 探针那种秒级")
    for spec in TABLE_ROW_COUNTS:
        table = spec.name.removeprefix("rowcount_")
        value = run_row_count(connection, spec)
        if isinstance(value, str):
            failures += 1
        print(f"  {table:24} {value}")

    _section("关键索引存在性核对（来自 pg_indexes 的启发式覆盖检查，见模块 Index-audit note）")
    for row in audit_indexes(connection):
        print(f"  [{row['state']}] {row['label']:52} serves={row['serves']}")

    _section("反向外键（referencing-side）覆盖索引核对")
    fk_rows = audit_reverse_fk_indexes(connection)
    if not fk_rows:
        print("  (指定热表集合内未发现外键约束)")
    for row in fk_rows:
        print(f"  [{row['state']}] {row['table']}({row['columns']})  constraint={row['name']}")

    _section("汇总")
    print(f"  语句失败次数: {failures}（0 表示全部语句成功返回一行 EXPLAIN/COUNT 结果）")
    return 1 if failures else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only PostgreSQL hot-path diagnostics for a production self-audit. "
            "The database URL is read from an environment variable and never printed."
        )
    )
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument(
        "--notebook-id", default="",
        help="默认取 knowledge_objects 行数最大的 notebook（该自动选库本身是一次全表 "
        "GROUP BY，9M 行级库可能分钟级）",
    )
    parser.add_argument(
        "--deep", action="store_true",
        help="额外跑四条可能 30s 级的重探针（两条 ILIKE 全表探针 + 两条缺向量反连接 COUNT）",
    )
    parser.add_argument(
        "--deep-statement-timeout-ms", type=int, default=0,
        help="仅对 --deep 的四条语句局部设置 statement_timeout（毫秒）；0=沿用库默认值",
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.environ.get(args.database_url_env, "")
    if not database_url:
        print(f"error: environment variable {args.database_url_env} is required", file=sys.stderr)
        return 2
    try:
        if database_identity(database_url).scheme != "postgresql":
            print("error: target must be PostgreSQL", file=sys.stderr)
            return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.deep_statement_timeout_ms < 0:
        print("error: --deep-statement-timeout-ms must be >= 0", file=sys.stderr)
        return 2

    try:
        connection = _connect(database_url)
    except DiagConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if not args.notebook_id:
            print(
                "提示：未指定 --notebook-id，正在对 knowledge_objects 做一次全表 "
                "GROUP BY 自动选库（9M 行级库可能是分钟级，早于下面才开始的秒级探针）",
                file=sys.stderr,
            )
        notebook_id = args.notebook_id or _pick_notebook_id(connection)
        if not notebook_id:
            print("error: no notebook_id given and none could be auto-selected "
                  "(knowledge_objects is empty)", file=sys.stderr)
            return 1
        return run_diagnostics(
            connection,
            notebook_id=notebook_id,
            deep=bool(args.deep),
            deep_timeout_ms=int(args.deep_statement_timeout_ms),
        )
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
