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
import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence

from app.models.common import Evidence
from app.repositories.lexical_query import sqlite_fts_match_expression
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.mount_sql import (
    MOUNT_JOIN, MOUNT_VALID, MOUNTED_BASE_IDS_SUBQUERY,
)


_DELETE_OBJECT_BATCH_SIZE = 500


def _retrieval_evidence(raw: object) -> list[Evidence]:
    """Hydrate valid evidence cards while tolerating malformed legacy items."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw or "[]")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []
    if not isinstance(raw, list):
        return []
    evidence: list[Evidence] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            evidence.append(Evidence(**item))
        except (TypeError, ValueError):
            continue
    return evidence


def _completion_generation_is_current(
    connection: sqlite3.Connection,
    notebook_id: str,
    source_id: str,
    run_id: str,
) -> bool:
    row = connection.execute(
        "SELECT er.id FROM extraction_runs er JOIN sources s ON s.id=er.source_id "
        "WHERE er.notebook_id=? AND er.source_id=? "
        "AND s.source_type NOT IN ('memory','knowhow') "
        "ORDER BY er.created_at DESC, er.id DESC LIMIT 1",
        (notebook_id, source_id),
    ).fetchone()
    return bool(row and row["id"] == run_id)


# batch-3-W1 T-5a: the ordered (table, WHERE-predicate) registry the pre-reset
# DRAIN pages over — each entry mirrors, byte-for-byte on the predicate, one of
# ``delete_notebook_graph_rows``'s DELETE statements in the SAME order (the
# order is semantic: knowledge_object_sources / knowledge_embeddings /
# kg_objects_fts match rows whose OBJECT is already gone, so their sets only
# stabilize after knowledge_objects has drained; processing "first table with
# a backlog" therefore converges exactly like the final pass would).
# ``params`` counts how many times the notebook_id binds into the predicate.
# ``test_kg_graph_drain.py`` pins registry↔final-statement agreement by
# tracing the final function's SQL, so a predicate edited on one side without
# the other fails loudly instead of drifting.
_GRAPH_DRAIN_STEPS: tuple[tuple[str, str, int, bool], ...] = (
    (
        "knowledge_source_fact_backfills",
        "notebook_id=? AND NOT EXISTS (SELECT 1 FROM sources s "
        "WHERE s.id=knowledge_source_fact_backfills.source_id AND s.notebook_id=? "
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
        "notebook_id=? AND NOT EXISTS (SELECT 1 FROM sources s "
        "WHERE s.id=knowledge_source_fact_elements.source_id "
        "AND s.notebook_id=? AND s.source_type IN ('memory','knowhow'))",
        2,
        True,
    ),
    (
        "knowledge_source_facts",
        "notebook_id=? AND NOT EXISTS (SELECT 1 FROM sources s "
        "WHERE s.id=knowledge_source_facts.source_id AND s.notebook_id=? "
        "AND s.source_type IN ('memory','knowhow'))",
        2,
        True,
    ),
    (
        "knowledge_relations",
        "notebook_id = ? AND NOT EXISTS (SELECT 1 FROM sources s "
        "WHERE s.id=knowledge_relations.source_id "
        "AND s.notebook_id=? AND s.source_type IN ('memory','knowhow'))",
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
        "notebook_id=? AND member_object_id IN "
        "(SELECT ko.id FROM knowledge_objects ko WHERE ko.notebook_id=? AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=ko.source_id AND s.notebook_id=? AND s.source_type IN ('memory','knowhow')))",
        3,
        False,
    ),
    (
        "knowledge_object_sources",
        "notebook_id=? AND object_id IN "
        "(SELECT ko.id FROM knowledge_objects ko WHERE ko.notebook_id=? AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=ko.source_id AND s.notebook_id=? AND s.source_type IN ('memory','knowhow')))",
        3,
        False,
    ),
    (
        "knowledge_objects",
        "notebook_id = ? AND NOT EXISTS (SELECT 1 FROM sources s "
        "WHERE s.id=knowledge_objects.source_id "
        "AND s.notebook_id=? AND s.source_type IN ('memory','knowhow'))",
        2,
        True,
    ),
    (
        "knowledge_object_sources",
        "notebook_id=? AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
        "WHERE ko.notebook_id=? AND ko.id=knowledge_object_sources.object_id)",
        2,
        True,
    ),
    ("concept_clusters", "notebook_id = ?", 1, True),
    ("concept_merge_candidates", "notebook_id = ?", 1, True),
    ("kg_relation_completion_state", "notebook_id = ?", 1, True),
    ("kg_analysis_artifacts", "notebook_id = ?", 1, True),
    ("kg_community_edges", "notebook_id = ?", 1, True),
    ("kg_source_profiles", "notebook_id = ?", 1, True),
    (
        "knowledge_embeddings",
        "notebook_id=? AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
        "WHERE ko.notebook_id=? AND ko.id=knowledge_embeddings.object_id)",
        2,
        True,
    ),
    (
        "extraction_runs",
        "notebook_id=? AND NOT EXISTS (SELECT 1 FROM sources s "
        "WHERE s.id=extraction_runs.source_id "
        "AND s.notebook_id=? AND s.source_type IN ('memory','knowhow'))",
        2,
        True,
    ),
    (
        "kg_objects_fts",
        "notebook_id=? AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
        "WHERE ko.notebook_id=? AND ko.id=kg_objects_fts.object_id)",
        2,
        True,
    ),
)


class KnowledgeStore:
    # KNN 访问路径提示在 FTS5 上永远无事可做:声明不具备,让服务层的
    # `_lexical_knn_allowed` 在读取任何规模统计之前零成本短路——发行默认后端
    # 不该为一个 PostgreSQL 专属特性每次检索付一条版本查询(codex #464 R1 P2)。
    # 这是能力声明不是 dialect 分支:服务层问的是「绑定的适配器能不能」,不是
    # 「后端是什么」。
    lexical_knn_capable = False

    def __init__(self, database: SqliteDatabase, seams) -> None:
        self.database = database
        self.seams = seams

    def _connect(self):
        return self.database.connect()

    # ------------------------------------------------ lifecycle projections
    @staticmethod
    def begin_graph_reset_isolation(db) -> None:
        """batch-3-W1 T-5a (codex #663 R12 P1): SQLite no-op — the single-writer lock IS the isolation.

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
        # SQLite: the global write lock already makes the whole final
        # transaction atomic against every writer — nothing to pin.
        return None

    @staticmethod
    def graph_drain_backlog(
        db: sqlite3.Connection, notebook_id: str, threshold: int, start: int = 0
    ) -> "tuple[str, int] | None":
        """batch-3-W1 T-5a: the first ``_GRAPH_DRAIN_STEPS`` table at or
        after index ``start`` whose matching-row count still exceeds
        ``threshold`` — returned as ``(table, index)`` so the caller can
        resume the scan from the same table on the next round — or ``None``
        when every remaining table is at or under it (= the final
        single-transaction pass in ``delete_notebook_graph_rows`` is now
        bounded and the caller may stop draining). Read-only point probe per
        table (``LIMIT 1 OFFSET threshold`` — exists-at-offset, never a full
        COUNT), so calling it on an empty/small graph costs a handful of
        index probes and NO write transaction — that is what keeps the
        small-graph path of ``delete_notebook_kg`` byte-identical to the
        pre-T-5a shape (see ``test_kg_mutation_phase_matrix``'s P0-1 pin).

        The ``start`` cursor (T-5a review F4) is safe because convergence is
        strictly forward: a table's matching set only shrinks as the drain
        proceeds (later tables' sets GROW as their dependency drains — they
        sit after it in the registry), so a table probed clean never needs
        re-probing; rows a concurrent ingestion lands in an already-passed
        table are the final transaction's bounded residue, the same bucket
        as the ≤threshold remainder."""
        for index in range(max(0, int(start)), len(_GRAPH_DRAIN_STEPS)):
            table, predicate, params, _mirrored = _GRAPH_DRAIN_STEPS[index]
            row = db.execute(
                f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1 OFFSET {int(threshold)}",
                (notebook_id,) * params,
            ).fetchone()
            if row is not None:
                return table, index
        return None

    @staticmethod
    def drain_notebook_graph_rows_page(
        db: sqlite3.Connection, notebook_id: str, step: int, limit: int
    ) -> dict[str, int]:
        """batch-3-W1 T-5a: delete ONE bounded page of ``table``'s rows that
        match its ``_GRAPH_DRAIN_STEPS`` predicate (the byte-identical
        predicate ``delete_notebook_graph_rows`` will re-run unboundedly in
        the final transaction — after draining it matches only the ≤threshold
        remainder). ``rowid IN (SELECT rowid … LIMIT n)`` is §1.5's form-one
        shape for SQLite (works on the FTS5 virtual table too — FTS5 exposes
        rowid). Caller owns the transaction: one page per ``write()``, and
        the same transaction must also bump ``kg_mutation_seq`` (the caller
        does, via the ``mark_unified_kg_dirty_in_tx`` choke point —
        kg_mutation.py's FULL CENSUS discipline: graph-row mutations on a
        live notebook never commit without their seq bump).

        Returns per-table deleted-row counts (empty dict = nothing left for
        this table). A ``knowledge_objects`` page is special (T-5a review
        F3): it deletes the page's objects TOGETHER WITH their
        ``knowledge_embeddings`` / ``concept_clusters`` (by
        ``member_object_id``) / ``knowledge_object_sources`` rows in this
        same transaction — the exact ``_delete_object_id_batch`` shape every
        other online object-deletion path uses — so no commit ever exposes
        a cluster/membership row whose object is gone. Without this, each
        committed object page would leave orphan cluster rows visible until
        the ``concept_clusters`` drain step ran, and an interrupted drain
        would leave them PERMANENTLY (``incremental_fuse_source``'s orphan
        sweep is once-per-process — a poisoned canonical cluster would then
        swallow newly extracted concepts). ``kg_objects_fts`` is deliberately
        NOT cleaned per page, matching ``_delete_object_id_batch``'s own
        exclusion — its rows fall to the registry's own fts step / the final
        pass; an interrupted drain can leave dangling fts rows until the
        next delete/rebuild completes (registered in the design doc's
        acceptance-cost list)."""
        if not 0 <= int(step) < len(_GRAPH_DRAIN_STEPS):
            raise ValueError(f"unknown graph drain step: {step}")
        table, predicate, params, _mirrored = _GRAPH_DRAIN_STEPS[int(step)]
        if table == "knowledge_source_facts":
            # codex #663 R14 P2: fresh ksfe children can land AFTER the
            # drain cursor passed their own step but BEFORE this parent
            # page — the FK cascade would then delete them unbounded and
            # uncounted. Same shape as the knowledge_objects page below:
            # select the page's fact ids, delete their children explicitly
            # (sub-batched, per-statement bounded like the established
            # _delete_object_id_batch path), then the parents; the cascade
            # finds nothing and every row lands in counts.
            ids = [
                row["id"] for row in db.execute(
                    f"SELECT id FROM knowledge_source_facts WHERE {predicate} "
                    f"LIMIT {int(limit)}",
                    (notebook_id,) * params,
                ).fetchall()
            ]
            if not ids:
                return {}
            counts = {"knowledge_source_facts": 0,
                      "knowledge_source_fact_elements": 0}
            for offset in range(0, len(ids), _DELETE_OBJECT_BATCH_SIZE):
                batch = ids[offset : offset + _DELETE_OBJECT_BATCH_SIZE]
                placeholders = ",".join("?" for _ in batch)
                cur = db.execute(
                    f"DELETE FROM knowledge_source_fact_elements "
                    f"WHERE fact_id IN ({placeholders})",
                    batch,
                )
                counts["knowledge_source_fact_elements"] += cur.rowcount
                cur = db.execute(
                    f"DELETE FROM knowledge_source_facts WHERE id IN ({placeholders})",
                    batch,
                )
                counts["knowledge_source_facts"] += cur.rowcount
            return {name: n for name, n in counts.items() if n}
        if table == "knowledge_objects":
            ids = [
                row["id"] for row in db.execute(
                    f"SELECT id FROM knowledge_objects WHERE {predicate} "
                    f"LIMIT {int(limit)}",
                    (notebook_id,) * params,
                ).fetchall()
            ]
            if not ids:
                return {}
            # codex #663 R2 P2: dependent fan-out (cluster memberships /
            # source-index rows per object) is unbounded per object, so the
            # page is issued as _DELETE_OBJECT_BATCH_SIZE sub-batches —
            # every individual statement carries the SAME per-statement
            # bound as the established ``_delete_object_id_batch`` deletion
            # path; the page transaction still commits atomically. Counts
            # use the object DELETE's actual rowcount (not len(ids)): a
            # concurrent worker may have removed selected objects between
            # the SELECT and the DELETE, and reporting those as deleted
            # here would both inflate the documented aggregate-count
            # contract and let a no-op page masquerade as progress.
            counts = {
                "knowledge_objects": 0,
                "knowledge_embeddings": 0,
                "concept_clusters": 0,
                "knowledge_object_sources": 0,
            }
            for offset in range(0, len(ids), _DELETE_OBJECT_BATCH_SIZE):
                batch = ids[offset : offset + _DELETE_OBJECT_BATCH_SIZE]
                placeholders = ",".join("?" for _ in batch)
                cur = db.execute(
                    f"DELETE FROM knowledge_embeddings "
                    f"WHERE object_id IN ({placeholders})",
                    batch,
                )
                counts["knowledge_embeddings"] += cur.rowcount
                cur = db.execute(
                    f"DELETE FROM concept_clusters "
                    f"WHERE notebook_id=? AND member_object_id IN ({placeholders})",
                    (notebook_id, *batch),
                )
                counts["concept_clusters"] += cur.rowcount
                cur = db.execute(
                    f"DELETE FROM knowledge_object_sources "
                    f"WHERE object_id IN ({placeholders})",
                    batch,
                )
                counts["knowledge_object_sources"] += cur.rowcount
                cur = db.execute(
                    f"DELETE FROM knowledge_objects WHERE id IN ({placeholders})",
                    batch,
                )
                counts["knowledge_objects"] += cur.rowcount
            return {name: n for name, n in counts.items() if n}
        cur = db.execute(
            f"DELETE FROM {table} WHERE rowid IN ("
            f"SELECT rowid FROM {table} WHERE {predicate} LIMIT {int(limit)})",
            (notebook_id,) * params,
        )
        return {table: cur.rowcount} if cur.rowcount else {}

    @staticmethod
    def delete_notebook_graph_rows(
        db: sqlite3.Connection, notebook_id: str, now: str
    ) -> dict[str, int]:
        """Wipe user-document KG while preserving hidden projection lifecycles.

        Memory extraction is owned by confirmation/edit, and Knowhow projection
        is deterministic zero-LLM output. A document rebuild must not delete
        either source's objects/relations; Memory embeddings, extraction runs,
        and FTS rows are preserved with them. Notebook-wide derived cluster/state
        tables are rebuilt from the surviving plus newly extracted objects.

        ``now`` (batch-3-W1 PR-2) stamps the ``unified_kg_state`` reset's
        ``updated_at`` — the same caller-supplied-clock seam every other
        writer of this table already threads through (``mark_dirty`` /
        ``bump_cluster_seq``).
        """
        counts: dict[str, int] = {}
        cur = db.execute(
            "DELETE FROM knowledge_source_fact_backfills WHERE notebook_id=? "
            "AND NOT EXISTS (SELECT 1 FROM sources s "
            "WHERE s.id=knowledge_source_fact_backfills.source_id AND s.notebook_id=? "
            "AND s.source_type IN ('memory','knowhow'))",
            (notebook_id, notebook_id),
        )
        counts["knowledge_source_fact_backfills"] = cur.rowcount
        # codex #663 R9 P2: explicit, COUNTED child delete before the
        # parent facts — the FK cascade (fk_ksfe_fact) then finds
        # nothing, so the aggregate counts contract covers these rows
        # and the drain registry keeps a 1:1 mirrored statement.
        cur = db.execute(
            "DELETE FROM knowledge_source_fact_elements WHERE notebook_id=? "
            "AND NOT EXISTS (SELECT 1 FROM sources s "
            "WHERE s.id=knowledge_source_fact_elements.source_id "
            "AND s.notebook_id=? AND s.source_type IN ('memory','knowhow'))",
            (notebook_id, notebook_id),
        )
        counts["knowledge_source_fact_elements"] = cur.rowcount
        cur = db.execute(
            "DELETE FROM knowledge_source_facts WHERE notebook_id=? "
            "AND NOT EXISTS (SELECT 1 FROM sources s "
            "WHERE s.id=knowledge_source_facts.source_id AND s.notebook_id=? "
            "AND s.source_type IN ('memory','knowhow'))",
            (notebook_id, notebook_id),
        )
        counts["knowledge_source_facts"] = cur.rowcount
        # T-5a codex #663 R1 P2: relations BEFORE objects — inside this single
        # transaction the order is semantically free (krel's predicate does
        # not reference knowledge_objects), but the DRAIN mirrors this order
        # page-by-page across separate commits, and edges-before-nodes is
        # what keeps every drain commit boundary free of doc-source edges
        # pointing at already-deleted objects (hidden-source edges may still
        # dangle after their endpoint dies — the final state has always had
        # that shape; see the drain docstring).
        cur = db.execute(
            "DELETE FROM knowledge_relations WHERE notebook_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=knowledge_relations.source_id "
            "AND s.notebook_id=? AND s.source_type IN ('memory','knowhow'))",
            (notebook_id, notebook_id),
        )
        counts["knowledge_relations"] = cur.rowcount
        cur = db.execute(
            "DELETE FROM knowledge_objects WHERE notebook_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=knowledge_objects.source_id "
            "AND s.notebook_id=? AND s.source_type IN ('memory','knowhow'))",
            (notebook_id, notebook_id),
        )
        counts["knowledge_objects"] = cur.rowcount
        cur = db.execute(
            "DELETE FROM knowledge_object_sources WHERE notebook_id=? "
            "AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
            "WHERE ko.notebook_id=? AND ko.id=knowledge_object_sources.object_id)",
            (notebook_id, notebook_id),
        )
        counts["knowledge_object_sources"] = cur.rowcount
        for table in (
            "concept_clusters", "concept_merge_candidates",
            "kg_relation_completion_state",
            # kg_analysis_artifacts: blanket-deleted, same reasoning as the PG
            # twin's _GRAPH_RESET_TABLES comment (batch-3-W1 PR-2, design doc
            # Sec 3.2 table #15) — a ledger row's meaning is "built at this
            # seq", so a cleared graph must drop it rather than reset it.
            "kg_analysis_artifacts",
            # kg_community_edges / kg_source_profiles: the DETAIL tables the
            # ledger's two board-dependent kinds describe. R1 (P2-1, post-
            # review): must be deleted in the SAME transaction as the ledger
            # row — discard_board_dependent_kg_analysis_artifacts's own
            # docstring states the ledger row and its detail rows are one
            # unit; leaving the detail half behind dangles pointers to a
            # board partition that no longer has a governing ledger row.
            "kg_community_edges", "kg_source_profiles",
        ):
            cur = db.execute(f"DELETE FROM {table} WHERE notebook_id = ?", (notebook_id,))
            counts[table] = cur.rowcount
        cur = db.execute(
            "DELETE FROM knowledge_embeddings WHERE notebook_id=? "
            "AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
            "WHERE ko.notebook_id=? AND ko.id=knowledge_embeddings.object_id)",
            (notebook_id, notebook_id),
        )
        counts["knowledge_embeddings"] = cur.rowcount
        cur = db.execute(
            "DELETE FROM extraction_runs WHERE notebook_id=? "
            "AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=extraction_runs.source_id "
            "AND s.notebook_id=? AND s.source_type IN ('memory','knowhow'))",
            (notebook_id, notebook_id),
        )
        counts["extraction_runs"] = cur.rowcount
        cur = db.execute(
            "DELETE FROM kg_objects_fts WHERE notebook_id=? "
            "AND NOT EXISTS (SELECT 1 FROM knowledge_objects ko "
            "WHERE ko.notebook_id=? AND ko.id=kg_objects_fts.object_id)",
            (notebook_id, notebook_id),
        )
        counts["kg_objects_fts"] = cur.rowcount
        # unified_kg_state: RESET in place to its birth-row shape, never
        # deleted — see the PostgreSQL twin's matching UPSERT comment
        # (design doc batch-3-W1 Sec 3.3 option C, D-3; R1 P0-2) for the
        # full rationale, including WHY this must be an UPSERT rather than a
        # bare UPDATE (a merge_dbs.py KG_STATE_TABLES import can leave this
        # row absent for a notebook that already has real content). kg_reset_
        # epoch ONLY increases here — this is its one writer in the whole
        # codebase; the INSERT branch starts it at 1, not 0, for the same
        # reason the PG twin does. source_index_backfilled /
        # chunk_elements_indexed / indexing_pipeline_id /
        # indexing_pipeline_version are deliberately left untouched on the
        # UPDATE branch and left to their column DEFAULTs on the INSERT
        # branch (0 / 0 / '' / 'builtin.chunk.v1' — the conservative
        # "unknown/uncertified" shape, not create_notebook's own
        # source_index_backfilled=1).
        cur = db.execute(
            "INSERT INTO unified_kg_state ("
            "notebook_id, dirty, kg_mutation_seq, cluster_mutation_seq, "
            "cluster_input_version, last_rebuild_at, object_count, "
            "relation_count, cluster_count, community_seq, canonical_rel_seq, "
            "mention_seq, kg_reset_epoch, updated_at"
            ") VALUES (?, 0, 0, 0, '', '', 0, 0, 0, -1, -1, -1, 1, ?) "
            "ON CONFLICT(notebook_id) DO UPDATE SET "
            "dirty=0, kg_mutation_seq=0, cluster_mutation_seq=0, "
            "cluster_input_version='', last_rebuild_at='', "
            "object_count=0, relation_count=0, cluster_count=0, "
            "community_seq=-1, canonical_rel_seq=-1, mention_seq=-1, "
            "kg_reset_epoch=kg_reset_epoch+1, updated_at=excluded.updated_at",
            (notebook_id, now),
        )
        counts["unified_kg_state"] = cur.rowcount
        return counts

    @staticmethod
    def notebook_tier_row(db: sqlite3.Connection, notebook_id: str):
        return db.execute("SELECT tier FROM notebooks WHERE id=?", (notebook_id,)).fetchone()

    @staticmethod
    def relink_rows(db: sqlite3.Connection, notebook_id: str):
        """Whole-notebook relink input — REFERENCE ONLY, never on a live path.

        Three unbounded ``fetchall``s: every non-deprecated object WITH its
        payload+evidence JSON, every relation, every source id. On the 9.1M-object
        base that is ~6.8 GB hydrated into one request thread. Production relink
        pages by source instead (``relink_source_page`` +
        ``relink_orphan_source_ids`` + ``relink_object_rows_for_source`` +
        ``relink_relation_rows_for_objects``); this method survives only as the
        differential test's oracle, which recomputes the historical whole-graph
        answer to prove the paged one agrees. `test_kg_relink_paged_equivalence`
        fails closed if any production call site reappears.
        """
        objects = db.execute(
            "SELECT id, object_type, source_id, payload, evidence FROM knowledge_objects "
            "WHERE notebook_id = ? AND status != 'deprecated'", (notebook_id,),
        ).fetchall()
        relations = db.execute(
            "SELECT source_object_id, target_object_id, edge_type "
            "FROM knowledge_relations WHERE notebook_id = ?", (notebook_id,),
        ).fetchall()
        valid_sources = {
            row["id"] for row in db.execute(
                "SELECT id FROM sources WHERE notebook_id = ?", (notebook_id,)
            ).fetchall()
        }
        return objects, relations, valid_sources

    @staticmethod
    def relink_source_page(
        db: sqlite3.Connection,
        notebook_id: str,
        after_created_at: str | None,
        after_id: str,
        limit: int,
    ):
        """One ``(created_at, id)`` keyset page of the notebook's OWN sources.

        The relink loop is driven off ``sources``, not off
        ``SELECT DISTINCT source_id FROM knowledge_objects``. The DISTINCT form
        reads well but has no notebook-prefixed index to stand on: the only index
        that carries ``source_id`` first is ``idx_knowledge_objects_source_id``,
        which is ordered ACROSS notebooks, so every page walks other notebooks'
        objects and discards them (measured on the shared PostgreSQL base: the tail
        page filtered ~1.2M neighbour rows, 183 ms warm — per page, and each page
        pays it again). ``sources`` has ``idx_sources_notebook_created`` already, so
        this is the repository's ordinary bounded source keyset — the same shape
        ``source_build_state_page`` uses — and every page is notebook-local.

        Hidden synthetic sources (``memory`` / ``knowhow`` projections) are
        deliberately NOT excluded here, unlike the build-target page: their rows own
        knowledge objects too, and skipping them would drop those partitions from
        the pass. Sources that own no objects merely yield an empty (no-op)
        partition.

        The union with ``relink_orphan_source_ids`` is what makes the pair exact:
        objects carry no foreign key to ``sources``, so an object's ``source_id``
        may be ``''`` or name a deleted source, and those partitions have no row
        here to be found by.
        """
        return db.execute(
            "SELECT id, created_at FROM sources WHERE notebook_id = ? "
            "AND (? IS NULL OR (created_at, id) > (?, ?)) "
            "ORDER BY created_at, id LIMIT ?",
            (
                notebook_id,
                after_created_at,
                after_created_at,
                after_id,
                max(1, int(limit)),
            ),
        ).fetchall()

    @staticmethod
    def relink_orphan_source_ids(db: sqlite3.Connection, notebook_id: str):
        """The object ``source_id`` values that name NO source of this notebook.

        ``''`` (objects stored without a source) plus every id left behind by a
        deleted source. Run ONCE per relink pass, before the ``sources`` keyset, so
        the two together cover exactly the distinct ``source_id`` values present in
        ``knowledge_objects`` — no partition dropped, none visited twice.

        **Cost, stated plainly:** this is one bounded pass over THIS notebook's
        objects (``notebook_id=?`` equality on ``idx_knowledge_objects_nb_updated``,
        DISTINCT via a temp b-tree, one indexed ``sources`` probe per row). It is
        deliberately NOT hinted onto ``idx_knowledge_objects_source_id``: that index
        is ordered across notebooks and pinning it would trade one notebook-local
        scan for a whole-table one. On the 9.1M-object base that is a single scan
        per relink run — acceptable for a background pass, and strictly cheaper than
        the per-page cross-notebook filtering it replaces.

        The RESULT is small and bounded by deletions, not by object count: one row
        per distinct orphan source id (``''`` plus ids of deleted sources), so no
        limit is imposed — a cap here could only silently drop partitions.
        """
        return db.execute(
            "SELECT DISTINCT ko.source_id AS source_id FROM knowledge_objects ko "
            "WHERE ko.notebook_id = ? AND (ko.source_id = '' OR NOT EXISTS("
            " SELECT 1 FROM sources s "
            " WHERE s.id = ko.source_id AND s.notebook_id = ko.notebook_id)) "
            "ORDER BY ko.source_id",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def relink_object_rows_for_source(
        db: sqlite3.Connection, notebook_id: str, source_id: str
    ):
        """Every non-deprecated object of ONE source, in insertion (rowid) order.

        The order matters: ``complete_isolated_edges`` walks isolated nodes in
        input order under a per-node edge cap, so once that cap binds a different
        order can pick different — still valid — partners.

        ``ORDER BY rowid`` is a DELIBERATE pin, not a reproduction of what the
        whole-notebook query did. That query carried no ``ORDER BY`` at all; SQLite
        happened to satisfy it from ``idx_knowledge_objects_nb_updated``, so its de
        facto order was ``updated_at``. That was a planner accident, not a contract
        — an ``ANALYZE``, a new index or a different backend could change it
        without notice — so the replacement states its order instead of inheriting
        one. Consequence, registered and covered by the differential fixture: on a
        notebook whose ``updated_at`` order disagrees with insertion order AND whose
        per-node cap binds, the paged pass may bind an isolated node to a different
        equally valid partner than the historical pass would have. Edge COUNTS and
        isolation counts are unaffected.
        """
        return db.execute(
            "SELECT id, object_type, payload, evidence FROM knowledge_objects "
            "WHERE notebook_id = ? AND source_id = ? AND status != 'deprecated' "
            "ORDER BY rowid",
            (notebook_id, source_id),
        ).fetchall()

    @staticmethod
    def relink_relation_rows_for_objects(
        db: sqlite3.Connection, notebook_id: str, object_ids
    ):
        """Relations with EITHER endpoint among ``object_ids`` (caller batches).

        Two statements, not one ``OR``. On an un-ANALYZEd database — i.e. every
        deployed one — EXPLAIN QUERY PLAN collapses the combined
        ``source_object_id IN (...) OR target_object_id IN (...)`` form to
        ``idx_knowledge_relations_nb_target_id (notebook_id=?)``: the endpoint bound
        is dropped and every relation in the notebook is scanned, ONCE PER SOURCE.
        Measured on 100k relations: split 0.05 ms, combined 11.2 ms. Split, each
        side binds its own endpoint index. Rows may repeat across the two
        statements; all consumers build sets.

        Cross-source edges MUST come back here. A node whose only edge leaves the
        source is connected, and a per-source view that could not see that edge would
        re-link it as if isolated — the one way source partitioning could diverge from
        the whole-graph answer.
        """
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        rows = list(db.execute(
            f"SELECT source_object_id, target_object_id, edge_type "
            f"FROM knowledge_relations WHERE notebook_id=? "
            f"AND source_object_id IN ({ph})",
            (notebook_id, *ids),
        ).fetchall())
        rows.extend(db.execute(
            f"SELECT source_object_id, target_object_id, edge_type "
            f"FROM knowledge_relations WHERE notebook_id=? "
            f"AND target_object_id IN ({ph})",
            (notebook_id, *ids),
        ).fetchall())
        return rows

    @staticmethod
    def relink_source_is_live(
        db: sqlite3.Connection, notebook_id: str, source_id: str
    ) -> bool:
        """Does this partition key still name a row of the notebook's sources?

        ``knowledge_relations.source_id`` has an FK to ``sources``; new relink rows
        must store NULL when the object's source is gone (or ''), exactly as the
        whole-notebook version did with its ``valid_sources`` set.
        """
        if not source_id:
            return False
        row = db.execute(
            "SELECT 1 FROM sources WHERE id=? AND notebook_id=?",
            (source_id, notebook_id),
        ).fetchone()
        return row is not None

    @staticmethod
    def incremental_object_rows(
        db: sqlite3.Connection, notebook_id: str, source_id: str, object_type: str,
        *, exclude_source: bool = False,
    ):
        if object_type == "concept" and exclude_source:
            return db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? "
                "AND object_type='concept' AND status!='deprecated' AND source_id!=?",
                (notebook_id, source_id),
            ).fetchall()
        if object_type == "concept":
            return db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND source_id=? "
                "AND object_type='concept' AND status!='deprecated'",
                (notebook_id, source_id),
            ).fetchall()
        return db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND source_id=? "
            "AND object_type=? AND status!='deprecated'",
            (notebook_id, source_id, object_type),
        ).fetchall()

    @staticmethod
    def concept_embedding_rows(db: sqlite3.Connection, notebook_id: str):
        """This notebook's LIVE **concept** object vectors.

        The one consumer is incremental fusion's no-ANN brute-force bridge
        branch, whose two vector lookups (``new_objs`` and ``existing_items``)
        are both ``incremental_object_rows(..., 'concept')`` results — i.e.
        exactly ``object_type='concept' AND status!='deprecated'``. Every other
        row this used to return (claims dominate the table at roughly 70% of
        objects) was decoded, dimension-checked, truncated and then never read.
        The predicates here are a verbatim copy of that consumer's, so the
        surviving keys — and therefore the branch's output — are unchanged.
        """
        return db.execute(
            "SELECT e.object_id AS object_id, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN knowledge_objects o ON o.id = e.object_id "
            "WHERE e.notebook_id=? AND o.object_type='concept' "
            "AND o.status!='deprecated'",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def embedding_rows_for_objects(db: sqlite3.Connection, notebook_id: str, object_ids):
        ids = list(object_ids)
        if not ids:
            return []
        return db.execute(
            "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=? "
            "AND object_id IN ({})".format(",".join("?" for _ in ids)),
            (notebook_id, *ids),
        ).fetchall()

    @staticmethod
    def valid_object_ids(db: sqlite3.Connection, object_ids):
        ids = list(object_ids)
        if not ids:
            return set()
        ph = ",".join("?" for _ in ids)
        return {
            row["id"] for row in db.execute(
                f"SELECT id FROM knowledge_objects WHERE id IN ({ph}) AND status!='deprecated'",
                ids,
            ).fetchall()
        }

    @staticmethod
    def source_build_rows(db: sqlite3.Connection, notebook_id: str):
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
                "SELECT id FROM sources WHERE notebook_id = ? "
                "AND source_type NOT IN ('memory','knowhow')", (notebook_id,)
            ).fetchall()
        ]
        kg_source_ids = {
            row["source_id"] for row in db.execute(
                "SELECT DISTINCT ko.source_id FROM knowledge_objects ko "
                "WHERE ko.notebook_id = ? AND ko.source_id != '' "
                "AND COALESCE(("
                "  SELECT er.status FROM extraction_runs er "
                "  WHERE er.source_id=ko.source_id AND er.run_type='kg' "
                "  ORDER BY er.created_at DESC, er.rowid DESC LIMIT 1"
                "), 'completed')='completed'",
                (notebook_id,),
            ).fetchall()
        }
        return source_ids, kg_source_ids

    @staticmethod
    def source_build_state_page(
        db: sqlite3.Connection,
        notebook_id: str,
        after_created_at: str | None,
        after_id: str,
        limit: int,
    ):
        """Bounded source state using the existing notebook/created index."""
        return db.execute(
            "SELECT s.id,s.created_at,"
            "EXISTS(SELECT 1 FROM source_elements e WHERE e.source_id=s.id) "
            "AS has_elements,"
            "EXISTS(SELECT 1 FROM knowledge_objects ko "
            " WHERE ko.notebook_id=? AND ko.source_id=s.id AND ko.source_id!='') "
            "AS has_graph,"
            "EXISTS(SELECT 1 FROM knowledge_objects ko "
            " WHERE ko.notebook_id=? AND ko.source_id=s.id AND ko.source_id!='' "
            " AND COALESCE((SELECT er.status FROM extraction_runs er "
            "  WHERE er.source_id=s.id AND er.run_type='kg' "
            "  ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1),'completed')"
            " ='completed') AS has_kg,"
            "COALESCE((SELECT er.error_message FROM extraction_runs er "
            " WHERE er.source_id=s.id AND er.run_type='kg' "
            " ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1),'') "
            "AS latest_kg_error,"
            # 与 latest_kg_error 配对的那一行的状态。调用方(_kg_target_batches)拿
            # (status, error) 喂 models.sources.kg_analyzed_without_objects,把「已分析、
            # 但这篇没有可整理的知识」与「还没分析」分开——否则零对象来源每次「分析新增」
            # 都会被重新选中、重付一遍模型钱,而且永远选不完。判据留在 Python 那一份,
            # 这里只多取一列(同一条 LIMIT 1 索引探测的形状)。
            "COALESCE((SELECT er.status FROM extraction_runs er "
            " WHERE er.source_id=s.id AND er.run_type='kg' "
            " ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1),'') "
            "AS latest_kg_status "
            "FROM sources s WHERE s.notebook_id=? "
            "AND (? IS NULL OR (s.created_at,s.id)>(?,?)) "
            "AND s.source_type NOT IN ('memory','knowhow') "
            "ORDER BY s.created_at,s.id LIMIT ?",
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
    def sources_with_elements(db: sqlite3.Connection, notebook_id: str) -> set:
        """该 notebook 下已产出 source_elements(已成功 parse)的 source_id 集合。
        build_notebook_kg 用它把无 elements 的源(parse 未落地)排除出抽取 targets——
        否则接地校验(build_records)没有 element 可绑,抽出的节点被整源丢弃、objects=0。

        问的是「哪些来源有元素」,答案的规模是来源数;所以从 sources 驱动、每个来源
        用 EXISTS 探一次索引即可,那次探测命中第一行就停。等价的 DISTINCT-over-JOIN
        写法要先把该 notebook 的**每一行元素**都取出来再去重,代价随元素数增长——
        千万级元素的库为了得到几千个 id 扫全部元素行。集合本身逐字相同。"""
        return {
            row["source_id"] for row in db.execute(
                "SELECT s.id AS source_id FROM sources s WHERE s.notebook_id = ? "
                "AND EXISTS (SELECT 1 FROM source_elements e WHERE e.source_id = s.id)",
                (notebook_id,),
            ).fetchall()
        }

    @staticmethod
    def active_object_count(db: sqlite3.Connection, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects "
            "WHERE notebook_id=? AND status!='deprecated'", (notebook_id,),
        ).fetchone()["c"])

    @staticmethod
    def unified_graph_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT id, object_type, payload, status FROM knowledge_objects "
            "WHERE notebook_id=? AND status!='deprecated'", (notebook_id,),
        ).fetchall()

    @staticmethod
    def neighbor_relation_rows(db: sqlite3.Connection, notebook_id: str, object_ids):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT source_object_id, target_object_id, edge_type FROM knowledge_relations "
            f"WHERE notebook_id=? "
            f"AND (source_object_id IN ({ph}) OR target_object_id IN ({ph}))",
            (notebook_id, *ids, *ids),
        ).fetchall()

    @staticmethod
    def object_meta_rows_for_notebook(
        db: sqlite3.Connection, notebook_id: str, object_ids,
    ):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT id, object_type, payload FROM knowledge_objects "
            f"WHERE notebook_id=? AND id IN ({ph})", (notebook_id, *ids),
        ).fetchall()

    @staticmethod
    def community_context_rows(db: sqlite3.Connection, notebook_id: str, members):
        ids = list(members)
        if not ids:
            return [], []
        ph = ",".join("?" for _ in ids)
        objects = db.execute(
            f"SELECT id, object_type, payload FROM knowledge_objects WHERE id IN ({ph})", ids,
        ).fetchall()
        relations = db.execute(
            f"SELECT source_object_id, target_object_id, edge_type FROM knowledge_relations "
            f"WHERE notebook_id=? AND review_status!='rejected' "
            f"AND source_object_id IN ({ph}) AND target_object_id IN ({ph}) "
            f"ORDER BY id",
            [notebook_id, *ids, *ids],
        ).fetchall()
        return objects, relations

    def get_notebook(self, notebook_id: str) -> None:
        with self.database.connect() as db:
            if db.execute("SELECT 1 FROM notebooks WHERE id=?", (notebook_id,)).fetchone() is None:
                raise KeyError(notebook_id)

    def has_kg(self, notebook_id: str) -> bool:
        with self.database.connect() as db:
            return bool(db.execute(
                "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id=?)",
                (notebook_id,),
            ).fetchone()[0])

    @staticmethod
    def any_mounted_has_kg_on(db: sqlite3.Connection, notebook_id: str) -> bool:
        """本库挂载的参考库中是否有任一已建 KG —— 驱动前端严格推理门控。
        未挂载 → False(即便系统里存在有图的公共知识库)。"""
        return bool(db.execute(
            "SELECT EXISTS(SELECT 1 " + MOUNT_JOIN + MOUNT_VALID
            + " AND EXISTS(SELECT 1 FROM knowledge_objects ko WHERE ko.notebook_id = b.id))",
            (notebook_id,),
        ).fetchone()[0])

    def any_mounted_has_kg(self, notebook_id: str) -> bool:
        with self.database.connect() as db:
            return self.any_mounted_has_kg_on(db, notebook_id)

    def any_mounted_has_kg_compat(
        self, notebook_id: str, db: "sqlite3.Connection | None" = None
    ) -> bool:
        return (
            self.any_mounted_has_kg_on(db, notebook_id) if db is not None
            else self.any_mounted_has_kg(notebook_id)
        )

    def retrieval_objects_compat(
        self, db: sqlite3.Connection, notebook_id: str, object_type: str,
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
                "SELECT notebook_id FROM extraction_runs WHERE id=?",
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
            from app.repositories.sqlite import knowledge_counts_cache
            knowledge_counts_cache.invalidate(notebook_id)

    def add_relations_current(
        self, notebook_id: str, source_id: str, relations: List[dict]
    ) -> int:
        with self.database.write() as db:
            return self.add_relations(
                db, notebook_id, source_id, relations, self.seams.now()
            )

    @staticmethod
    def object_version_row(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS ts "
            "FROM knowledge_objects WHERE notebook_id = ?", (notebook_id,),
        ).fetchone()

    @staticmethod
    def relation_context_rows(db: sqlite3.Connection, notebook_id: str,
                              relation_ids=None, *, batch_size: int = 900):
        base_sql = (
            "SELECT r.id AS id, r.source_object_id AS s, r.target_object_id AS t, "
            "r.edge_type AS et, r.evidence AS ev, r.review_status AS review_status, "
            "so.payload AS sp, tp.payload AS tpl, "
            "so.object_type AS st, tp.object_type AS tt "
            "FROM knowledge_relations r "
            "JOIN knowledge_objects so ON so.id = r.source_object_id "
            "JOIN knowledge_objects tp ON tp.id = r.target_object_id "
            "WHERE r.notebook_id = ?"
        )
        if relation_ids is None:
            return db.execute(base_sql, (notebook_id,)).fetchall()
        ids = list(relation_ids)
        rows = []
        for offset in range(0, len(ids), batch_size):
            batch = ids[offset:offset + batch_size]
            ph = ",".join("?" for _ in batch)
            rows.extend(db.execute(
                base_sql + f" AND r.id IN ({ph})", (notebook_id, *batch),
            ).fetchall())
        return rows

    @staticmethod
    def relation_id_rows_for_objects(
        db: sqlite3.Connection,
        notebook_id: str,
        object_ids,
        limit: int,
        *,
        batch_size: int = 900,
    ):
        """Return a bounded set of live relations incident to lexical KG hits.

        Source and target probes are separate so each uses its covering endpoint
        index.  The caller supplies FTS-ranked object ids; once ``limit`` rows
        have been collected, no lower-ranked endpoint is probed.
        """
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
        # Four bind slots per indexed keyset probe.  The UNION materializes only
        # each stream's LIMIT page, never the complete adjacency of a hub.
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
                        f"WHERE notebook_id=? AND review_status!='rejected' "
                        f"AND {endpoint}=? AND id>? ORDER BY id LIMIT ?"
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
    def relation_exists(db: sqlite3.Connection, notebook_id: str) -> bool:
        return db.execute(
            "SELECT 1 FROM knowledge_relations WHERE notebook_id = ? LIMIT 1",
            (notebook_id,),
        ).fetchone() is not None

    @staticmethod
    def relation_endpoint_rows(db: sqlite3.Connection, notebook_id: str,
                               source_ids=None):
        if source_ids:
            values = list(source_ids)
            ph = ",".join("?" for _ in values)
            return db.execute(
                f"SELECT source_object_id, target_object_id FROM knowledge_relations "
                f"WHERE notebook_id=? AND source_id IN ({ph})",
                (notebook_id, *values),
            ).fetchall()
        return db.execute(
            "SELECT source_object_id, target_object_id FROM knowledge_relations "
            "WHERE notebook_id = ?", (notebook_id,),
        ).fetchall()

    @staticmethod
    def relation_connected_object_ids(
        db: sqlite3.Connection, notebook_id: str, object_ids
    ):
        """Return only candidates that have at least one incident relation.

        The correlated EXISTS probes use the notebook/endpoint covering indexes
        and short-circuit on the first edge.  Do not replace this with a query
        that returns relation endpoints: a single hub can have millions of
        incident rows while the caller only needs one boolean per candidate.
        """
        values = list(dict.fromkeys(object_ids))
        if not values:
            return []
        candidates = ",".join("(?)" for _ in values)
        return db.execute(
            f"WITH candidates(object_id) AS (VALUES {candidates}) "
            "SELECT object_id FROM candidates AS c "
            "WHERE EXISTS ("
            "SELECT 1 FROM knowledge_relations AS r "
            "WHERE r.notebook_id=? AND r.source_object_id=c.object_id LIMIT 1"
            ") OR EXISTS ("
            "SELECT 1 FROM knowledge_relations AS r "
            "WHERE r.notebook_id=? AND r.target_object_id=c.object_id LIMIT 1"
            ")",
            (*values, notebook_id, notebook_id),
        ).fetchall()

    @staticmethod
    def neighbor_ids(db: sqlite3.Connection, notebook_id: str, object_id: str,
                     *, endpoint: str, edge_type=None, limit: int | None = None,
                     usable_statuses: Sequence[str] | None = None):
        # `limit` 非 None 时按 `r.id` 定序取前 N 行:排序键是
        # `idx_knowledge_relations_nb_source_id`/`_nb_target_id`
        # `(notebook_id, source/target_object_id, id)` 的第三列,前两列都是等值
        # 谓词,索引本身即给出该序 → 有界读取不引入排序开销。limit=None 逐位保持
        # 历史行为(其余调用方不受影响)。
        #
        # `usable_statuses` 非空时对**邻居那一侧**的别名加 status 谓词——它必须
        # 落在 LIMIT **之前**:事后再按 status 丢弃,等于让 deprecated 之类的对象
        # 白占有界读取窗口,行序靠后的可用邻居被整个漏掉(可用邻居明明存在却可能
        # 返回很少甚至零)。这条查询本就 JOIN 了 src/tgt 两张 knowledge_objects,
        # 加谓词零额外成本。None/空 → SQL 逐字回到历史形状。
        if endpoint not in {"source_object_id", "target_object_id"}:
            raise ValueError("invalid relation endpoint")
        edge_clause = " AND edge_type=?" if edge_type else ""
        params = [notebook_id, object_id] + ([edge_type] if edge_type else [])
        status_clause = ""
        if usable_statuses:
            # endpoint 指的是**锚点**那一端,邻居在另一端。
            neighbour_alias = (
                "tgt" if endpoint == "source_object_id" else "src"
            )
            statuses = list(usable_statuses)
            ph = ",".join("?" for _ in statuses)
            status_clause = f" AND {neighbour_alias}.status IN ({ph})"
            params.extend(statuses)
        bound_clause = ""
        if limit is not None:
            bound_clause = " ORDER BY r.id LIMIT ?"
            params.append(int(limit))
        return db.execute(
            "SELECT r.source_object_id,r.target_object_id,r.edge_type,"
            "src.object_type AS source_type,tgt.object_type AS target_type "
            "FROM knowledge_relations AS r "
            "JOIN knowledge_objects AS src ON src.id=r.source_object_id "
            "JOIN knowledge_objects AS tgt ON tgt.id=r.target_object_id "
            f"WHERE r.notebook_id=? AND r.{endpoint}=? "
            f"AND r.review_status!='rejected'{edge_clause}{status_clause}"
            f"{bound_clause}", params,
        ).fetchall()

    def usable_object_rows(self, notebook_id: str, object_ids: Sequence[str]):
        with self.database.connect() as db:
            rows = self.usable_object_rows_on(
                db, object_ids, ("reviewed", "approved", "project_specific", "conflict"),
            )
        return [dict(row) for row in rows if row["notebook_id"] == notebook_id]

    @staticmethod
    def usable_object_rows_on(db: sqlite3.Connection, object_ids, statuses,
                              *, batch_size: int = 500):
        # 分片 IN(仓库既有做法,见 `relation_context_rows`):调用方传进来的 id 数
        # 由各自的上限决定,而 `SQLITE_MAX_VARIABLE_NUMBER` 在老构建上低到 999——
        # 一条平铺的 IN 会在最需要它的那种库上直接抛错。
        ids = list(object_ids)
        if not ids:
            return []
        status_ph = ",".join("?" for _ in statuses)
        rows = []
        for offset in range(0, len(ids), batch_size):
            batch = ids[offset:offset + batch_size]
            ph = ",".join("?" for _ in batch)
            rows.extend(db.execute(
                f"SELECT * FROM knowledge_objects WHERE id IN ({ph}) "
                f"AND status IN ({status_ph})", [*batch, *statuses],
            ).fetchall())
        return rows

    @staticmethod
    def graph_version_rows(db: sqlite3.Connection, notebook_id: str):
        rel = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts, "
            "COALESCE(SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END), 0) AS n_rej, "
            "COALESCE(SUM(CASE WHEN review_status = 'verified' THEN 1 ELSE 0 END), 0) AS n_ver "
            "FROM knowledge_relations WHERE notebook_id = ?", (notebook_id,),
        ).fetchone()
        obj = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS ts "
            "FROM knowledge_objects WHERE notebook_id = ?", (notebook_id,),
        ).fetchone()
        return rel, obj

    @staticmethod
    def graph_object_rows(db: sqlite3.Connection, notebook_id: str, statuses):
        ph = ",".join("?" for _ in statuses)
        return db.execute(
            "SELECT id, object_type, payload FROM knowledge_objects "
            f"WHERE notebook_id = ? AND status IN ({ph}) ORDER BY rowid, id",
            (notebook_id, *statuses),
        ).fetchall()

    @staticmethod
    def graph_relation_rows(db: sqlite3.Connection, notebook_id: str,
                            *, include_id_evidence: bool = True):
        columns = ("id, source_object_id, target_object_id, edge_type, evidence"
                   if include_id_evidence else
                   "source_object_id, target_object_id")
        return db.execute(
            f"SELECT {columns} FROM knowledge_relations "
            "WHERE notebook_id = ? AND review_status != 'rejected' ORDER BY id",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def object_evidence_rows(db: sqlite3.Connection, object_ids):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT id, evidence FROM knowledge_objects WHERE id IN ({ph})", ids,
        ).fetchall()

    @staticmethod
    def notebook_object_evidence_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT id, evidence FROM knowledge_objects WHERE notebook_id=?",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def follow_start_row(db: sqlite3.Connection, object_id: str,
                         active_notebook_id: str, statuses):
        """起点授权门:只有 active 自己的对象、或 active 挂载的参考库里的对象,
        才能作为 follow_chain 的合法起点(未挂载的 tier='base' 库不算,即便它已发布)。"""
        ph = ",".join("?" for _ in statuses)
        return db.execute(
            f"SELECT ko.*, n.tier AS notebook_tier "
            f"FROM knowledge_objects ko JOIN notebooks n ON n.id=ko.notebook_id "
            f"WHERE ko.id=? AND ko.status IN ({ph}) "
            "AND (ko.notebook_id=? OR ko.notebook_id IN ("
            + MOUNTED_BASE_IDS_SUBQUERY + "))",
            (object_id, *statuses, active_notebook_id, active_notebook_id),
        ).fetchone()

    @staticmethod
    def follow_endpoint_rows(db: sqlite3.Connection, notebook_id: str, object_id: str,
                             endpoint: str, limit: int):
        index_name = ("idx_knowledge_relations_nb_source"
                      if endpoint == "source_object_id"
                      else "idx_knowledge_relations_nb_target")
        return db.execute(
            f"SELECT r.id, r.notebook_id, r.source_id, "
            f"r.source_object_id, r.target_object_id, r.edge_type, r.review_status "
            f"FROM knowledge_relations AS r INDEXED BY {index_name} "
            f"WHERE r.notebook_id=? AND r.{endpoint}=? LIMIT ?",
            (notebook_id, object_id, limit),
        ).fetchall()

    @staticmethod
    def follow_relation_evidence_rows(db: sqlite3.Connection, relation_ids):
        ids = list(relation_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT r.id, r.evidence, s.title AS source_title "
            f"FROM knowledge_relations r LEFT JOIN sources s ON s.id=r.source_id "
            f"WHERE r.id IN ({ph})", tuple(ids),
        ).fetchall()

    @staticmethod
    def follow_object_rows(db: sqlite3.Connection, notebook_id: str,
                           object_ids, statuses):
        ids = list(object_ids)
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        status_ph = ",".join("?" for _ in statuses)
        return db.execute(
            f"SELECT * FROM knowledge_objects WHERE notebook_id=? "
            f"AND id IN ({ph}) AND status IN ({status_ph})",
            (notebook_id, *ids, *statuses),
        ).fetchall()

    @staticmethod
    def in_network_relation_rows(db: sqlite3.Connection, notebook_id: str,
                                 object_ids):
        """T2(批 1 热点整改):``DISTINCT`` 下推同一 (src,et,tgt) 跨多个来源的
        重复原始行——此前它们全部原样回传,靠 Python 侧 identity 去重循环兜底
        (仍保留,防御性)。``ORDER BY`` 是把此前 planner 相关、无定义的行序钉成
        确定行为:旧 SQL 没有 ORDER BY,支持数并列时 identity 去重"谁先到谁被
        记入 seen_relations"的 tie 由存储顺序决定,双后端/同库两次运行都不必
        一致;补上确定序让这类并列在跨后端/跨执行下稳定,不是语义变更。"""
        ids = list(object_ids)
        if len(ids) < 2:
            return []
        ph = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT DISTINCT r.source_object_id, r.target_object_id, r.edge_type, "
            f"src.object_type AS source_type, tgt.object_type AS target_type "
            f"FROM knowledge_relations AS r "
            f"JOIN knowledge_objects AS src ON src.id=r.source_object_id "
            f"JOIN knowledge_objects AS tgt ON tgt.id=r.target_object_id "
            f"WHERE r.notebook_id=? AND r.review_status!='rejected' "
            f"AND r.source_object_id IN ({ph}) "
            f"AND r.target_object_id IN ({ph}) "
            f"ORDER BY r.source_object_id, r.edge_type, r.target_object_id",
            [notebook_id, *ids, *ids],
        ).fetchall()

    @staticmethod
    def retrieval_objects(
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        statuses: Optional[Iterable[str]],
        id_filter: Optional[Iterable[str]],
        *,
        batch_size: int = 900,
    ) -> List[dict]:
        base_query = "SELECT * FROM knowledge_objects WHERE notebook_id=? AND object_type=?"
        params: List[object] = [notebook_id, object_type]
        if statuses is not None:
            values = list(statuses)
            base_query += f" AND status IN ({','.join('?' for _ in values)})"
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
                    base_query + f" AND id IN ({','.join('?' for _ in batch)})",
                    (*params, *batch),
                ).fetchall())
            rows.sort(key=lambda row: (row["created_at"], row["id"]))
        else:
            rows = db.execute(base_query + " ORDER BY created_at ASC, id ASC", params).fetchall()
        return [{
            "id": row["id"],
            "payload": json.loads(row["payload"] or "{}"),
            "evidence": _retrieval_evidence(row["evidence"]),
            "status": row["status"],
            "owner": row["owner"],
            "last_reviewed": row["last_reviewed"] if "last_reviewed" in row.keys() else "",
        } for row in rows]

    @staticmethod
    def duplicate_seed_rows(
        db: sqlite3.Connection, notebook_id: str, object_type: str,
    ) -> List[dict]:
        """R3 T-B1 (KG-3) dedup pass 1 -- SQLite twin of the PostgreSQL
        implementation; see ``KnowledgeStorePort`` for the shared rationale
        (evidence-free thin BLOCKING projection, ``status`` pushdown, ORDER
        BY parity with ``retrieval_objects``).

        ``json_extract(payload, '$.name')`` yields SQLite's own native type
        for the JSON scalar (str/int/float/None, and 0/1 for a JSON bool) --
        NOT text like the PostgreSQL twin's ``->>``. ``find_duplicates``
        applies the SAME ``str(name) if name is not None else ""`` coercion
        used by the review-queue endpoint read
        (``KnowledgeGovernanceService.review_queue``) to reconcile the two
        dialects onto the same text for a string/integer name; see that call
        site's docstring for the registered (pre-existing, non-regressing)
        divergence on other JSON scalar shapes."""
        if object_type == "procedure":
            rows = db.execute(
                "SELECT id, status, payload FROM knowledge_objects "
                "WHERE notebook_id=? AND object_type=? AND status != 'deprecated' "
                "ORDER BY created_at ASC, id ASC",
                (notebook_id, object_type),
            ).fetchall()
            return [{
                "id": row["id"],
                "status": row["status"],
                "payload": json.loads(row["payload"] or "{}"),
            } for row in rows]
        rows = db.execute(
            "SELECT id, status, json_extract(payload, '$.name') AS name "
            "FROM knowledge_objects "
            "WHERE notebook_id=? AND object_type=? AND status != 'deprecated' "
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
        db: sqlite3.Connection, notebook_id: str, object_ids: Sequence[str],
        *, batch_size: int = 900,
    ) -> List[dict]:
        """R3 T-B1 (KG-3) dedup pass 2 -- see ``KnowledgeStorePort`` for the
        shared rationale (no ``evidence`` column; unspecified row order).

        ⚠ Bare ``id IN (...)``, NOT ``notebook_id=? AND id IN (...)`` -- same
        planner hazard as ``GovernanceStore.review_queue_rows``'s endpoint
        lookup (``sqlite/governance_store.py``): without ``ANALYZE`` (never
        run on production databases here), SQLite plans the combined
        predicate as a per-batch notebook-wide scan instead of a primary-key
        seek, and pass 1's blocks are exactly the "many ids out of one big
        notebook" shape that triggers it (measured elsewhere in this
        repository: 0.138s -> 14.155s on a 200k-row database for the same
        recipe). ``notebook_id`` is projected instead and checked in
        Python -- there is no "read a lot then filter" risk because the
        result set is already capped by the id list."""
        ids = list(dict.fromkeys(object_ids))
        rows: List[sqlite3.Row] = []
        for offset in range(0, len(ids), batch_size):
            batch = ids[offset:offset + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows.extend(
                row for row in db.execute(
                    "SELECT id, payload, notebook_id FROM knowledge_objects "
                    f"WHERE id IN ({placeholders})",
                    tuple(batch),
                ).fetchall()
                if row["notebook_id"] == notebook_id
            )
        return [{
            "id": row["id"],
            "payload": json.loads(row["payload"] or "{}"),
        } for row in rows]

    def _element_texts(self, db, element_ids, *, with_ordinal: bool = False):
        ids = [e for e in element_ids if e]
        if not ids:
            return {}, {}
        ph = ",".join("?" for _ in ids)
        rows = db.execute(f"SELECT id, text FROM source_elements WHERE id IN ({ph})", ids).fetchall()
        texts = {r["id"]: r["text"] for r in rows}
        if not with_ordinal:
            return texts, {}
        order_rows = db.execute(
            "SELECT se.id FROM source_elements se JOIN sources s ON se.source_id=s.id "
            "WHERE s.notebook_id=(SELECT notebook_id FROM sources WHERE id=("
            "SELECT source_id FROM source_elements WHERE id=? LIMIT 1)) "
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
            ph = ",".join("?" for _ in element_ids)
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
            row = db.execute("SELECT id, object_type, payload, evidence FROM knowledge_objects WHERE id=? AND notebook_id=?", (object_id, notebook_id)).fetchone()
            if row is None:
                raise KeyError(object_id)
            obj_type = row["object_type"]
            payload = json.loads(row["payload"] or "{}")
            section = payload.get("section_path", "")
            occurrences = self._enrich_evidence(db, json.loads(row["evidence"] or "[]"))
            result = {"id": object_id, "object_type": obj_type, "name": payload.get("name", ""),
                      "section_path": section, "occurrences": occurrences, "definition": None, "steps": None}
            if obj_type == "concept":
                # prefer the unified cluster's fused description when present
                crow = db.execute(
                    "SELECT canonical_description FROM concept_clusters "
                    "WHERE notebook_id=? AND member_object_id=? AND canonical_description!='' LIMIT 1",
                    (notebook_id, object_id)).fetchone()
                if crow and crow["canonical_description"]:
                    result["definition"] = crow["canonical_description"]
                else:
                    drow = db.execute(
                        "SELECT ko.payload, ko.evidence FROM knowledge_relations r JOIN knowledge_objects ko ON ko.id=r.source_object_id "
                        "WHERE r.notebook_id=? AND r.target_object_id=? AND r.edge_type='defines' LIMIT 1", (notebook_id, object_id)).fetchone()
                    if drow is not None:
                        dpay = json.loads(drow["payload"] or "{}")
                        den = self._enrich_evidence(db, json.loads(drow["evidence"] or "[]"))
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
                            "WHERE notebook_id=? AND object_type='procedure' AND status!='deprecated' "
                            "AND json_extract(payload,'$.section_path')=?",
                            (notebook_id, section)).fetchall()
                    else:
                        prows = db.execute(
                            "SELECT id, payload, evidence FROM knowledge_objects "
                            "WHERE notebook_id=? AND object_type='procedure' AND status!='deprecated' "
                            "LIMIT 500",
                            (notebook_id,)).fetchall()
                    candidate_steps = []
                    for pr in prows:
                        ppay = json.loads(pr["payload"] or "{}")
                        if ppay.get("section_path", "") != section:
                            continue
                        ev = json.loads(pr["evidence"] or "[]")
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
        db: sqlite3.Connection, notebook_id: str, object_type: str, statuses
    ) -> int:
        placeholders = ",".join("?" for _ in statuses)
        row = db.execute(
            f"SELECT COUNT(*) AS count FROM knowledge_objects "
            f"WHERE notebook_id = ? AND object_type = ? AND status IN ({placeholders})",
            (notebook_id, object_type, *statuses),
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def count_active_objects(db: sqlite3.Connection, notebook_id: str) -> int:
        from app.repositories.sqlite import knowledge_counts_cache
        return knowledge_counts_cache.active_object_count(db, notebook_id)

    @staticmethod
    def type_counts(
        db: sqlite3.Connection, notebook_id: str
    ) -> "tuple[Dict[str, int], Dict[str, str]]":
        from app.repositories.sqlite import knowledge_counts_cache
        counts = knowledge_counts_cache.type_counts(db, notebook_id)  # non-deprecated
        # Labels are resolved by SchemaRegistryService so global + notebook
        # overlay semantics have one implementation rather than dialect SQL
        # duplicated in both knowledge stores.
        return counts, {}

    # --------------------------------------------------------------- list
    @staticmethod
    def knowledge_object_page_rows(
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        after: tuple[object, str] | None,
        limit: int,
    ) -> "List[sqlite3.Row]":
        """One RAW keyset page of one type — the enumeration twin of
        ``list_knowledge_page``'s OFFSET paging.

        OFFSET is fine for a UI page jump but wrong for enumeration: page N
        costs O(N·page) and a concurrent insert shifts every later page.  The
        keyset rides ``idx_knowledge_objects_nb_type_created``
        (notebook_id, object_type, created_at, id) with a row-value comparison
        so each page is O(limit) and stable under inserts.

        No status predicate, on purpose (see the port docstring): that index
        does not carry ``status``, so filtering here would make one page walk
        an unbounded number of deprecated index entries on a notebook with a
        long governance history.  ``status`` travels on every row instead, and
        the enumeration executor applies the counting path's own
        ``USABLE_STATUSES`` over a bounded over-scan.

        ``source_id`` travels for exactly the same reason: an object extracted
        from a private Memory synthetic source must not be listed to a
        notebook's other members, and a ``NOT EXISTS`` against ``sources`` in
        this query would be a second unindexed residual with the same
        unbounded-skip hazard.  The executor filters it against
        ``memory_source_ids`` inside the same over-scan ceiling.
        """
        params: List[object] = [notebook_id, object_type]
        clause = ""
        if after is not None:
            clause = "AND (created_at, id) > (?, ?) "
            params.extend([after[0], after[1]])
        params.append(max(1, int(limit)))
        return db.execute(
            "SELECT id, object_type, source_id, payload, evidence, status, created_at "
            "FROM knowledge_objects "
            f"WHERE notebook_id = ? AND object_type = ? {clause}"
            "ORDER BY created_at, id LIMIT ?",
            params,
        ).fetchall()

    @staticmethod
    def list_knowledge_page(
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        status: Optional[str],
        offset: int,
        limit: int,
    ) -> "tuple[int, List[dict]]":
        base_query = (
            "FROM knowledge_objects "
            "WHERE notebook_id = ? AND object_type = ?"
        )
        params: List[object] = [notebook_id, object_type]
        if status:
            base_query += " AND status = ?"
            params.append(status)

        # Pagination total = a slice of the seq-gated type/status count memo
        # (from_row already warmed it this request), not a fresh per-page COUNT.
        from app.repositories.sqlite import knowledge_counts_cache
        total = knowledge_counts_cache.object_type_total(
            db, notebook_id, object_type, status
        )
        rows = db.execute(
            f"SELECT * {base_query} ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()

        objects: List[dict] = []
        for row in rows:
            keys = row.keys()
            objects.append(
                {
                    "id": row["id"],
                    "payload": json.loads(row["payload"] or "{}"),
                    "evidence": [
                        Evidence(**item)
                        for item in json.loads(row["evidence"] or "[]")
                    ],
                    "status": row["status"],
                    "owner": row["owner"],
                    "last_reviewed": row["last_reviewed"] if "last_reviewed" in keys else "",
                }
            )
        return int(total), objects

    # -------------------------------------------------------------- graph
    @staticmethod
    def graph_node_rows(db: sqlite3.Connection, notebook_id: str) -> List[sqlite3.Row]:
        return db.execute(
            "SELECT id, object_type, status, payload FROM knowledge_objects "
            "WHERE notebook_id = ? AND status != 'deprecated'", (notebook_id,)
        ).fetchall()

    @staticmethod
    def relations_for_notebook(db: sqlite3.Connection, notebook_id: str) -> List[dict]:
        rows = db.execute(
            "SELECT * FROM knowledge_relations WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchall()
        return [
            {
                "id": r["id"], "source_id": r["source_id"],
                "source_object_id": r["source_object_id"],
                "target_object_id": r["target_object_id"], "edge_type": r["edge_type"],
                "evidence": json.loads(r["evidence"] or "[]"),
            }
            for r in rows
        ]

    def add_relations(
        self,
        db: sqlite3.Connection,
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.seams.new_id("rel"), notebook_id, source_id,
                    rel["source_object_id"], rel["target_object_id"],
                    rel["edge_type"],
                    json.dumps(rel.get("evidence", []), ensure_ascii=False),
                    now,
                ),
            )
        return len(relations)

    # ------------------------------------------------- store_kg chunk writes
    @staticmethod
    def insert_object_chunk(
        connection: sqlite3.Connection, rows: Sequence[tuple]
    ) -> None:
        connection.executemany(
            "INSERT INTO knowledge_objects "
            "(id, notebook_id, object_type, status, owner, payload, evidence, "
            "source_candidate_id, source_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '', ?, ?, NULL, ?, ?, ?)",
            rows,
        )

    @staticmethod
    def validate_source_fact_publish(
        connection: sqlite3.Connection,
        notebook_id: str,
        source_id: str,
        source_generation: str,
        element_ids: Sequence[str],
    ) -> None:
        """Fail closed unless this is the current running source generation."""
        row = connection.execute(
            "SELECT er.id, er.status FROM extraction_runs er "
            "JOIN sources s ON s.id=er.source_id AND s.notebook_id=er.notebook_id "
            "WHERE er.notebook_id=? AND er.source_id=? "
            "ORDER BY er.created_at DESC, er.id DESC LIMIT 1",
            (notebook_id, source_id),
        ).fetchone()
        if not row or row["id"] != source_generation or row["status"] != "running":
            raise RuntimeError("stale source fact generation")
        expected = list(dict.fromkeys(str(value) for value in element_ids if value))
        if not expected:
            return
        payload = json.dumps(expected, ensure_ascii=False)
        owned = int(connection.execute(
            "WITH requested(id) AS (SELECT CAST(value AS TEXT) FROM json_each(?)) "
            "SELECT COUNT(*) AS c FROM requested CROSS JOIN source_elements se "
            "ON se.id=requested.id WHERE se.source_id=?",
            (payload, source_id),
        ).fetchone()["c"])
        if owned != len(expected):
            raise RuntimeError("source fact evidence crosses source generation boundary")

    @staticmethod
    def validate_stage_source_elements(
        connection: sqlite3.Connection,
        notebook_id: str,
        source_id: str,
        element_ids: Sequence[str],
    ) -> None:
        source = connection.execute(
            "SELECT 1 FROM sources WHERE id=? AND notebook_id=?",
            (source_id, notebook_id),
        ).fetchone()
        if source is None:
            raise RuntimeError("stale indexing stage source")
        expected = list(dict.fromkeys(str(value) for value in element_ids if value))
        if not expected:
            return
        payload = json.dumps(expected, ensure_ascii=False)
        owned = int(connection.execute(
            "WITH requested(id) AS (SELECT CAST(value AS TEXT) FROM json_each(?)) "
            "SELECT COUNT(*) AS c FROM requested JOIN source_elements se "
            "ON se.id=requested.id WHERE se.source_id=?",
            (payload, source_id),
        ).fetchone()["c"])
        if owned != len(expected):
            raise RuntimeError("staged evidence crosses source boundary")

    @staticmethod
    def insert_source_fact_rows(
        connection: sqlite3.Connection,
        rows: Sequence[tuple],
        element_rows: Sequence[tuple],
        *,
        projection_origin: str = "live",
    ) -> None:
        if projection_origin not in {"live", "historical"}:
            raise ValueError("invalid source-fact projection origin")
        connection.executemany(
            "INSERT INTO knowledge_source_facts "
            "(id, notebook_id, source_id, source_generation, local_object_id, global_object_id, "
            "object_type, payload, evidence, projection_version, created_at, updated_at, "
            "projection_origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            [(*row, projection_origin) for row in rows],
        )
        connection.executemany(
            "INSERT INTO knowledge_source_fact_elements "
            "(fact_id, notebook_id, source_id, source_generation, element_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(fact_id, element_id) DO NOTHING",
            element_rows,
        )

    @staticmethod
    def insert_relation_chunk(
        connection: sqlite3.Connection, rows: Sequence[tuple]
    ) -> None:
        connection.executemany(
            "INSERT INTO knowledge_relations "
            "(id, notebook_id, source_id, source_object_id, target_object_id, "
            "edge_type, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    @staticmethod
    def completion_generation_is_current(
        connection: sqlite3.Connection, notebook_id: str, source_id: str, run_id: str
    ) -> bool:
        return _completion_generation_is_current(
            connection, notebook_id, source_id, run_id
        )

    @staticmethod
    def completion_validate_scope(
        connection: sqlite3.Connection, notebook_id: str, source_id: str,
        run_id: str, object_ids: Sequence[str], element_ids: Sequence[str]
    ) -> bool:
        if not _completion_generation_is_current(
            connection, notebook_id, source_id, run_id
        ):
            return False
        objects = list(dict.fromkeys(object_ids))
        elements = list(dict.fromkeys(element_ids))
        if not objects:
            return False
        oph = ",".join("?" for _ in objects)
        object_count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=? AND source_id=? "
            f"AND status IN ('approved','reviewed','project_specific','conflict') AND id IN ({oph})",
            (notebook_id, source_id, *objects),
        ).fetchone()[0]
        if int(object_count) != len(objects):
            return False
        if elements:
            eph = ",".join("?" for _ in elements)
            element_count = connection.execute(
                "SELECT COUNT(*) FROM source_elements WHERE source_id=? "
                f"AND id IN ({eph})", (source_id, *elements),
            ).fetchone()[0]
            if int(element_count) != len(elements):
                return False
        return True

    @staticmethod
    def completion_existing_keys(
        connection: sqlite3.Connection, notebook_id: str, object_ids: Sequence[str]
    ) -> set[tuple[str, str, str]]:
        ids = list(dict.fromkeys(object_ids))
        if not ids:
            return set()
        ph = ",".join("?" for _ in ids)
        rows = connection.execute(
            "SELECT source_object_id,target_object_id,edge_type FROM knowledge_relations "
            f"WHERE notebook_id=? AND source_object_id IN ({ph}) "
            f"AND target_object_id IN ({ph})",
            (notebook_id, *ids, *ids),
        ).fetchall()
        return {(r["source_object_id"], r["target_object_id"], r["edge_type"]) for r in rows}

    @staticmethod
    def completion_page(
        connection: sqlite3.Connection, notebook_id: str, source_id: str,
        run_id: str, mode: str, schema_version: int,
        reasoning_edge_types: Sequence[str],
        edge_contract_rows: Sequence[tuple[str, str, str]],
        known_edge_types: Sequence[str], core_node_types: Sequence[str],
        limit: int, now: str
    ) -> dict:
        """Return one stable source-object page without retaining a DB cursor."""
        if not _completion_generation_is_current(
            connection, notebook_id, source_id, run_id
        ):
            return {"rows": [], "cursor": "", "next_cursor": "", "exhausted": False,
                    "generation_conflict": True}
        connection.execute(
            "INSERT OR IGNORE INTO kg_relation_completion_state "
            "(notebook_id,source_id,source_generation,mode,next_object_id,status,"
            "schema_version,updated_at) VALUES (?,?,?,?,?,'pending',?,?)",
            (notebook_id, source_id, run_id, mode, "", schema_version, now),
        )
        connection.execute(
            "UPDATE kg_relation_completion_state SET next_object_id='',status='pending',"
            "schema_version=?,updated_at=? WHERE source_id=? AND source_generation=? "
            "AND mode=? AND schema_version!=?",
            (schema_version, now, source_id, run_id, mode, schema_version),
        )
        state = connection.execute(
            "SELECT next_object_id,status FROM kg_relation_completion_state "
            "WHERE source_id=? AND source_generation=? AND mode=?",
            (source_id, run_id, mode),
        ).fetchone()
        cursor = str(state["next_object_id"] or "")
        if state["status"] == "completed":
            return {"rows": [], "cursor": cursor, "next_cursor": cursor,
                    "exhausted": True, "already_completed": True}
        page_limit = max(1, int(limit))
        reasoning_types = tuple(dict.fromkeys(reasoning_edge_types))
        contract_rows = tuple(dict.fromkeys(edge_contract_rows))
        known_types = tuple(dict.fromkeys(known_edge_types))
        core_types = tuple(dict.fromkeys(core_node_types))
        if not reasoning_types or not contract_rows or not known_types or not core_types:
            raise ValueError("completion edge contract must not be empty")
        type_placeholders = ",".join("?" for _ in reasoning_types)
        pair_sql = " OR ".join(
            "(r.edge_type=? AND rs.object_type=? AND rt.object_type=?)"
            for _ in contract_rows
        )
        known_placeholders = ",".join("?" for _ in known_types)
        core_placeholders = ",".join("?" for _ in core_types)
        queryable_sql = (
            f"(({pair_sql}) OR (r.edge_type IN ({known_placeholders}) AND "
            f"(rs.object_type NOT IN ({core_placeholders}) OR "
            f"rt.object_type NOT IN ({core_placeholders}))))"
        )
        contract_params = tuple(
            value for row in contract_rows for value in row
        ) + known_types + core_types + core_types
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
            f" AND r.edge_type IN ({type_placeholders}) AND {queryable_sql}) "
            "AS has_reasoning_relation FROM knowledge_objects ko "
            "WHERE ko.notebook_id=? AND ko.source_id=? AND ko.id>? "
            "AND ko.status IN ('approved','reviewed','project_specific','conflict') "
            "ORDER BY ko.id LIMIT ?",
            (*contract_params, *reasoning_types, *contract_params,
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
        connection: sqlite3.Connection, notebook_id: str, source_id: str,
        object_ids: Sequence[str]
    ) -> list:
        ids = list(dict.fromkeys(object_ids))
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        return connection.execute(
            "SELECT id,object_type,payload,evidence,status FROM knowledge_objects "
            f"WHERE notebook_id=? AND source_id=? AND id IN ({ph}) "
            "AND status IN ('approved','reviewed','project_specific','conflict')",
            (notebook_id, source_id, *ids),
        ).fetchall()

    @staticmethod
    def completion_element_rows(
        connection: sqlite3.Connection, source_id: str,
        element_ids: Sequence[str]
    ) -> list:
        ids = list(dict.fromkeys(element_ids))
        if not ids:
            return []
        rows = []
        # Older SQLite builds cap one statement at 999 bound variables. Keep
        # each hydration query below that rail while preserving bounded input.
        for offset in range(0, len(ids), 900):
            chunk = ids[offset:offset + 900]
            ph = ",".join("?" for _ in chunk)
            rows.extend(connection.execute(
                "SELECT id,source_id,element_type,location_label,text "
                f"FROM source_elements WHERE source_id=? AND id IN ({ph})",
                (source_id, *chunk),
            ).fetchall())
        return rows

    @staticmethod
    def completion_pending_states(
        connection: sqlite3.Connection, after_source_id: str,
        after_mode: str, limit: int
    ) -> list:
        return connection.execute(
            "SELECT st.notebook_id,st.source_id,st.source_generation,st.mode,s.title "
            "FROM kg_relation_completion_state AS st "
            "JOIN sources AS s ON s.id=st.source_id "
            "WHERE st.status='pending' AND (st.source_id>? OR "
            "(st.source_id=? AND st.mode>?)) "
            "AND st.source_generation=(SELECT er.id FROM extraction_runs AS er "
            "WHERE er.notebook_id=st.notebook_id AND er.source_id=st.source_id "
            "ORDER BY er.created_at DESC,er.id DESC LIMIT 1) "
            "ORDER BY st.source_id,st.mode LIMIT ?",
            (after_source_id, after_source_id, after_mode, max(1, int(limit))),
        ).fetchall()

    @staticmethod
    def completion_mark_state_stale(
        connection: sqlite3.Connection, notebook_id: str, source_id: str,
        run_id: str, mode: str, now: str
    ) -> bool:
        cursor = connection.execute(
            "UPDATE kg_relation_completion_state SET status='stale',updated_at=? "
            "WHERE notebook_id=? AND source_id=? AND source_generation=? "
            "AND mode=? AND status='pending'",
            (now, notebook_id, source_id, run_id, mode),
        )
        return cursor.rowcount == 1

    @staticmethod
    def completion_transition_mode_state(
        connection: sqlite3.Connection, notebook_id: str, source_id: str,
        run_id: str, old_mode: str, new_mode: str, schema_version: int, now: str
    ) -> bool:
        """Atomically publish the new recoverable cursor before retiring the old."""
        if new_mode not in {"shadow", "write"} or not _completion_generation_is_current(
            connection, notebook_id, source_id, run_id
        ):
            return False
        connection.execute(
            "INSERT OR IGNORE INTO kg_relation_completion_state "
            "(notebook_id,source_id,source_generation,mode,next_object_id,status,"
            "schema_version,updated_at) VALUES (?,?,?,?,?,'pending',?,?)",
            (notebook_id, source_id, run_id, new_mode, "", schema_version, now),
        )
        connection.execute(
            "UPDATE kg_relation_completion_state SET status='pending',"
            "next_object_id=CASE WHEN schema_version!=? THEN '' ELSE next_object_id END,"
            "schema_version=?,updated_at=? WHERE notebook_id=? AND source_id=? "
            "AND source_generation=? AND mode=? AND status='stale'",
            (schema_version, schema_version, now, notebook_id, source_id, run_id,
             new_mode),
        )
        connection.execute(
            "UPDATE kg_relation_completion_state SET status='stale',updated_at=? "
            "WHERE notebook_id=? AND source_id=? AND source_generation=? "
            "AND mode=? AND status='pending'",
            (now, notebook_id, source_id, run_id, old_mode),
        )
        return True

    @staticmethod
    def completion_advance_state(
        connection: sqlite3.Connection, notebook_id: str, source_id: str,
        run_id: str, mode: str, schema_version: int, expected_cursor: str,
        next_cursor: str, status: str, now: str
    ) -> bool:
        if status not in {"pending", "completed"} or not _completion_generation_is_current(
            connection, notebook_id, source_id, run_id
        ):
            return False
        cursor = connection.execute(
            "UPDATE kg_relation_completion_state SET next_object_id=?,status=?,updated_at=? "
            "WHERE notebook_id=? AND source_id=? AND source_generation=? AND mode=? "
            "AND schema_version=? AND next_object_id=? AND status='pending'",
            (next_cursor, status, now, notebook_id, source_id, run_id, mode,
             schema_version, expected_cursor),
        )
        return cursor.rowcount == 1

    @staticmethod
    def insert_completion_relations(
        connection: sqlite3.Connection, rows: Sequence[tuple]
    ) -> int:
        before = connection.total_changes
        connection.executemany(
            "INSERT OR IGNORE INTO knowledge_relations "
            "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,"
            "evidence,created_at,review_status) VALUES (?,?,?,?,?,?,?,?, 'pending')",
            rows,
        )
        return connection.total_changes - before

    @staticmethod
    def insert_kg_fts_rows(
        connection: sqlite3.Connection, rows: Sequence[tuple]
    ) -> None:
        connection.executemany(
            "INSERT INTO kg_objects_fts(object_id, notebook_id, name) "
            "VALUES (?, ?, ?)",
            rows,
        )

    @staticmethod
    def insert_object_source_rows(
        connection: sqlite3.Connection, rows: Sequence[tuple]
    ) -> None:
        """Forward maintenance (P0-4 reverse index) for FRESH inserts — rows
        never had prior entries, so a plain batched INSERT suffices (no
        DELETE-first)."""
        connection.executemany(
            "INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id) "
            "VALUES (?, ?, ?)",
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
        connection: sqlite3.Connection, source_id: str, row_id: str
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
            "SELECT id FROM knowledge_objects WHERE source_id = ? "
            "AND json_extract(payload, '$.row_id') = ?)",
            (source_id, row_id),
        )
        connection.execute(
            "DELETE FROM knowledge_objects WHERE source_id = ? "
            "AND json_extract(payload, '$.row_id') = ?",
            (source_id, row_id),
        )

    @staticmethod
    def delete_objects_by_source(
        connection: sqlite3.Connection, source_id: str
    ) -> None:
        """Full wipe of every object (case+procedure+tool) a knowhow table's
        hidden source has ever produced — project_table's escape-hatch
        rebuild and delete_table_projection's cleanup both use this rather
        than the row-scoped variant above, so a stale/orphaned tool (or a
        procedure whose column has since been deleted) never survives a full
        rebuild."""
        connection.execute(
            "DELETE FROM knowledge_object_sources WHERE object_id IN ("
            "SELECT id FROM knowledge_objects WHERE source_id = ?)",
            (source_id,),
        )
        connection.execute(
            "DELETE FROM knowledge_objects WHERE source_id = ?", (source_id,)
        )

    @classmethod
    def prune_cluster_rows_for_source(
        cls,
        connection: sqlite3.Connection,
        notebook_id: str,
        source_id: str,
        keep_object_ids: Iterable[str] = (),
    ) -> int:
        """Drop this source's ``concept_clusters`` membership rows EXCEPT the
        ones whose object survives — the knowhow projector's companion to
        ``delete_objects_by_source``, run in that same write transaction.

        The projector deletes-and-reinserts under STABLE hashed ids, so a plain
        "delete every membership row of this source" would strip the cluster
        membership of objects that are about to be written straight back
        (that is why ``_delete_object_id_batch``'s own cleanup is deliberately
        NOT reused here). ``keep_object_ids`` is the set the caller is about to
        reinsert; only the difference — objects the reprojection genuinely
        dropped (a deleted column, a deleted row, a renamed cell) — loses its
        membership rows. ``delete_table_projection`` passes nothing and so
        cleans all of them.

        Must be called BEFORE the objects are deleted: the stale set is derived
        from the rows still in ``knowledge_objects`` for this source
        (``idx_knowledge_objects_source``), which is exactly the scan
        ``delete_objects_by_source`` performs a statement later. Bounded by one
        knowhow table's projection, the same scale the caller already holds in
        memory as ``object_rows``. Returns the number of membership rows
        deleted.

        With this in place the repository has ZERO paths that leave a dangling
        membership row behind, which is what lets the fusion-side sweep be a
        pure per-process legacy backstop (see ``incremental_fuse_source``).
        """
        keep = set(keep_object_ids)
        stale = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM knowledge_objects WHERE source_id = ?",
                (source_id,),
            ).fetchall()
            if row["id"] not in keep
        ]
        deleted = 0
        for offset in range(0, len(stale), _DELETE_OBJECT_BATCH_SIZE):
            batch = stale[offset : offset + _DELETE_OBJECT_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            cursor = connection.execute(
                f"DELETE FROM concept_clusters "
                f"WHERE notebook_id=? AND member_object_id IN ({placeholders})",
                (notebook_id, *batch),
            )
            deleted += int(cursor.rowcount or 0)
        return deleted

    @staticmethod
    def delete_relations_by_source_object(
        connection: sqlite3.Connection, notebook_id: str, source_object_id: str
    ) -> None:
        """Delete every edge OUT of one case object (identified_by/
        diagnosed_by/fixed_by/requires_tool all have the case as source, per
        the knowhow projection spec) — one call cleans all of a row's prior
        edges regardless of which cell changed. Uses idx_knowledge_relations_
        nb_source (notebook_id, source_object_id)."""
        connection.execute(
            "DELETE FROM knowledge_relations WHERE notebook_id = ? "
            "AND source_object_id = ?",
            (notebook_id, source_object_id),
        )

    @staticmethod
    def delete_relations_by_source(
        connection: sqlite3.Connection, source_id: str
    ) -> None:
        """Full wipe of every relation a knowhow table's hidden source has
        ever produced (project_table / delete_table_projection). Uses
        idx_knowledge_relations_source."""
        connection.execute(
            "DELETE FROM knowledge_relations WHERE source_id = ?", (source_id,)
        )

    @staticmethod
    def insert_object_if_missing(
        connection: sqlite3.Connection, row: tuple
    ) -> None:
        """Upsert-by-absence for tool objects: a tool's id is a stable hash of
        (table_id, normalized name), so the SAME tool referenced by multiple
        rows always maps to the SAME id — the first row to mention it creates
        the row, later rows are a no-op (INSERT OR IGNORE on the id PRIMARY
        KEY) rather than a second, redundant insert attempt."""
        connection.execute(
            "INSERT OR IGNORE INTO knowledge_objects "
            "(id, notebook_id, object_type, status, owner, payload, evidence, "
            "source_candidate_id, source_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '', ?, ?, NULL, ?, ?, ?)",
            row,
        )

    @staticmethod
    def legacy_typed_table_ids(
        connection: sqlite3.Connection, object_types: Sequence[str], id_prefix: str
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
        placeholders = ",".join("?" for _ in object_types)
        rows = connection.execute(
            f"SELECT DISTINCT json_extract(payload, '$.table_id') AS table_id "
            f"FROM knowledge_objects WHERE object_type IN ({placeholders}) "
            f"AND id LIKE ?",
            (*object_types, f"{id_prefix}%"),
        ).fetchall()
        return [r["table_id"] for r in rows if r["table_id"]]

    def get_object_row(
        self, notebook_id: str, object_id: str
    ) -> "sqlite3.Row | None":
        with self.database.connect() as db:
            return db.execute(
                "SELECT * FROM knowledge_objects WHERE id=? AND notebook_id=?",
                (object_id, notebook_id),
            ).fetchone()

    # --------------------------------------------------------- provenance
    @staticmethod
    def source_ids_from_evidence(evidence_json: str | list | None) -> set:
        """PURE: parse an evidence JSON TEXT column value into the set of distinct
        source_ids it references (Evidence.source_id is present on every item —
        confirmed in app/models/schemas.py; a merged object's evidence can span
        multiple sources, which is exactly why a per-object single source_id
        column is insufficient and this reverse table exists). Accepts either
        the serialized TEXT column value (DB-row / maintenance-scan shape) or
        an already-parsed evidence list — a caller holding both the Python
        object and its serialized form passes the list directly and skips the
        redundant dumps-then-loads round trip (mirrors the Postgres adapter,
        whose JSONB rows already arrive parsed).

        Explicit whitelist, not a blanket `except`: list is the fast path;
        str/bytes/None go through json.loads (only a malformed-JSON string
        is swallowed into an empty result — that is a real "no evidence"
        shape callers already tolerate); anything else (e.g. a tuple) is a
        caller bug and must raise loudly rather than be silently treated as
        "no source ids" — that used to hide behind a bare `except (...,
        TypeError)` which caught both "not valid JSON" and "not a string at
        all" the same way."""
        if isinstance(evidence_json, list):
            items = evidence_json
        elif evidence_json is None or isinstance(evidence_json, (str, bytes)):
            try:
                items = json.loads(evidence_json or "[]")
            except json.JSONDecodeError:
                items = []
        else:
            raise TypeError(
                "source_ids_from_evidence: expected list, str, bytes or None, "
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
        connection: sqlite3.Connection,
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
            "DELETE FROM knowledge_object_sources WHERE object_id = ?", (object_id,)
        )
        source_ids = cls.source_ids_from_evidence(evidence_json)
        if source_ids:
            connection.executemany(
                "INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id) "
                "VALUES (?, ?, ?)",
                [(object_id, sid, notebook_id) for sid in source_ids],
            )

    @staticmethod
    def delete_object_sources(
        connection: sqlite3.Connection, object_ids: List[str]
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
            placeholders = ",".join("?" for _ in batch)
            connection.execute(
                f"DELETE FROM knowledge_object_sources "
                f"WHERE object_id IN ({placeholders})",
                batch,
            )

    @staticmethod
    def source_index_backfilled(db: sqlite3.Connection, notebook_id: str) -> bool:
        row = db.execute(
            "SELECT source_index_backfilled FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,),
        ).fetchone()
        return bool(row and row["source_index_backfilled"])

    def mark_source_index_backfilled(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> None:
        now = self.seams.now()
        db.execute(
            """
            INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, source_index_backfilled, updated_at)
            VALUES (?, 0, 0, 1, ?)
            ON CONFLICT(notebook_id) DO UPDATE SET
              source_index_backfilled=1,
              updated_at=excluded.updated_at
            """,
            (notebook_id, now),
        )

    @staticmethod
    def chunk_elements_indexed(db: sqlite3.Connection, notebook_id: str) -> bool:
        """Has this notebook's element -> chunk reverse index been backfilled?

        One indexed single-row read on ``unified_kg_state`` — the same shape
        (and cost) as ``source_index_backfilled``. False keeps the legacy
        whole-notebook chunk scan, byte-for-byte."""
        row = db.execute(
            "SELECT chunk_elements_indexed FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,),
        ).fetchone()
        return bool(row and row["chunk_elements_indexed"])

    def mark_chunk_elements_indexed(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> None:
        now = self.seams.now()
        db.execute(
            """
            INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, chunk_elements_indexed, updated_at)
            VALUES (?, 0, 0, 1, ?)
            ON CONFLICT(notebook_id) DO UPDATE SET
              chunk_elements_indexed=1,
              updated_at=excluded.updated_at
            """,
            (notebook_id, now),
        )

    def stale_object_ids_for_source(
        self, db: sqlite3.Connection, source_id: str, notebook_id: str
    ) -> List[str]:
        """Return knowledge_objects.id values whose evidence references source_id.

        Fast path (backfilled notebooks): a single indexed SQL lookup against
        knowledge_object_sources — O(matches), not O(notebook size).

        Legacy path (not yet backfilled): filter the JSON evidence in keyset-
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
        db: sqlite3.Connection,
        source_id: str,
        notebook_id: str,
        *,
        after_id: str = "",
        limit: int = _DELETE_OBJECT_BATCH_SIZE,
    ) -> List[str]:
        """Return one stable, bounded source-reference batch.

        ``clear_source_graph_state`` advances ``after_id`` after deleting each
        page, so online delete/reparse never materializes all matching ids or
        rescans a previously visited key range. The compatibility list API
        above uses the same cursor while retaining its complete-list contract.
        """
        page_limit = max(1, min(int(limit), _DELETE_OBJECT_BATCH_SIZE))
        if self.source_index_backfilled(db, notebook_id):
            rows = db.execute(
                "SELECT DISTINCT object_id FROM knowledge_object_sources "
                "WHERE source_id = ? AND notebook_id = ? AND object_id > ? "
                "ORDER BY object_id LIMIT ?",
                (source_id, notebook_id, after_id, page_limit),
            ).fetchall()
            return [row["object_id"] for row in rows]

        # json_each must receive only a valid top-level ARRAY.  The legacy
        # Python parser ignored a top-level object/scalar/null, and PostgreSQL's
        # array-containment query has the same boundary.  Nesting the CASEs
        # avoids calling json_type on malformed historical TEXT evidence.
        rows = db.execute(
            "SELECT DISTINCT ko.id AS object_id "
            "FROM knowledge_objects AS ko "
            "JOIN json_each(CASE WHEN json_valid(ko.evidence) THEN "
            "CASE WHEN json_type(ko.evidence) = 'array' "
            "THEN ko.evidence ELSE '[]' END ELSE '[]' END) AS item "
            "WHERE ko.notebook_id = ? AND ko.id > ? AND item.type = 'object' "
            "AND json_extract(CASE WHEN item.type = 'object' "
            "THEN item.value ELSE '{}' END, '$.source_id') = ? "
            "ORDER BY ko.id LIMIT ?",
            (notebook_id, after_id, source_id, page_limit),
        ).fetchall()
        return [row["object_id"] for row in rows]

    @staticmethod
    def _direct_object_ids_for_source_batch(
        db: sqlite3.Connection,
        source_id: str,
        limit: int = _DELETE_OBJECT_BATCH_SIZE,
    ) -> List[str]:
        page_limit = max(1, min(int(limit), _DELETE_OBJECT_BATCH_SIZE))
        rows = db.execute(
            "SELECT id FROM knowledge_objects WHERE source_id = ? LIMIT ?",
            (source_id, page_limit),
        ).fetchall()
        return [row["id"] for row in rows]

    @classmethod
    def _delete_object_id_batch(
        cls, db: sqlite3.Connection, notebook_id: str, object_ids: Sequence[str]
    ) -> None:
        """Delete one already-bounded object batch and its derived rows.

        The ``concept_clusters`` membership rows go with the objects, in this
        same transaction, keyed by ``member_object_id`` (``idx_clusters_member``
        — one indexed seek per id, no notebook-wide work). Previously nothing
        removed them here and every dangling row waited for
        ``incremental_fuse_source``'s notebook-wide orphan anti-join, which a
        multi-million-object library re-paid on EVERY extracted source. Deleting
        them at the source keeps that sweep a rare backstop (see the gate in
        ``incremental_fuse_source``).

        The ``notebook_id`` predicate rides along so this statement is the
        byte-for-byte same SEMANTICS as the ``sweep_orphan_clusters_page`` it
        replaces, rather than resting on three remote facts (ids are a global
        primary key, deep copy remints them, the batch came from a
        notebook-scoped query). ``idx_clusters_member`` still drives the seek —
        the notebook column is a free residual filter on the handful of rows a
        member id can match.

        ⚠ This is safe HERE and deliberately NOT done in the knowhow projector's
        ``delete_objects_by_source`` / ``delete_objects_by_source_and_row``.
        Every caller of this path (source delete, reparse,
        ``store_kg(replace_source=True)``) either drops the objects for good or
        re-mints FRESH ids, so the membership rows really are dead. The knowhow
        projector deletes and re-inserts under STABLE hashed ids in one
        transaction — cleaning its cluster rows would strip a still-live
        object's membership until the next full rebuild. Its dangling rows stay
        the deferred sweep's job (it runs after the re-insert has committed, so
        it correctly sees those objects alive), and its callers bump
        ``kg_mutation_seq``, which is exactly what re-opens that sweep's gate.

        Deliberately NO ``cluster_mutation_seq`` bump here (the orphan sweep
        does bump). Reasons, registered: (a) that counter's consumers are cache
        identities that also carry ``kg_mutation_seq``, and every caller of this
        path bumps the latter at the service layer — ``store_kg`` /
        ``delete_source`` / reparse-then-extract; (b) the one caller that does
        not (reparse with KG extraction disabled) also deletes the OBJECTS
        without a bump today, and no fusion runs in that configuration at all,
        so those cluster rows used to linger indefinitely — removing them
        earlier is strictly closer to the truth, never further from it.
        """
        batch = list(object_ids[:_DELETE_OBJECT_BATCH_SIZE])
        if not batch:
            return
        placeholders = ",".join("?" for _ in batch)
        db.execute(
            f"DELETE FROM knowledge_embeddings "
            f"WHERE object_id IN ({placeholders})",
            batch,
        )
        db.execute(
            f"DELETE FROM concept_clusters "
            f"WHERE notebook_id=? AND member_object_id IN ({placeholders})",
            (notebook_id, *batch),
        )
        db.execute(
            f"DELETE FROM knowledge_objects WHERE id IN ({placeholders})",
            batch,
        )
        cls.delete_object_sources(db, batch)

    def clear_source_graph_state(
        self,
        db: sqlite3.Connection,
        source_id: str,
        notebook_id: str,
    ) -> None:
        """Delete one source's graph rows without touching its extraction history."""
        db.execute(
            "DELETE FROM kg_relation_completion_state WHERE source_id = ?",
            (source_id,),
        )
        db.execute(
            "DELETE FROM knowledge_source_facts "
            "WHERE source_id=? AND notebook_id=?",
            (source_id, notebook_id),
        )
        db.execute(
            "DELETE FROM knowledge_source_fact_backfills "
            "WHERE source_id=? AND notebook_id=?",
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
        db: sqlite3.Connection,
        source_id: str,
        notebook_id: str,
        *,
        clear_embeddings: bool,
    ) -> None:
        self.clear_source_graph_state(db, source_id, notebook_id)
        db.execute("DELETE FROM extraction_runs WHERE source_id = ?", (source_id,))
        if clear_embeddings:
            db.execute("DELETE FROM element_embeddings WHERE source_id = ?", (source_id,))

    @staticmethod
    def delete_relations_for_source(db: sqlite3.Connection, source_id: str) -> None:
        db.execute("DELETE FROM knowledge_relations WHERE source_id = ?", (source_id,))

    def begin_extraction_run(
        self,
        db: sqlite3.Connection,
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
        if not preserve_existing:
            self.clear_source_extraction_state(
                db, source_id, notebook_id, clear_embeddings=False
            )
        db.execute(
            """INSERT INTO extraction_runs
               (id, notebook_id, source_id, run_type, status, error_message,
                indexing_pipeline_id, indexing_pipeline_version, created_at, updated_at)
               VALUES (?, ?, ?, 'kg', 'running', '', ?, ?, ?, ?)""",
            (
                run_id,
                notebook_id,
                source_id,
                indexing_pipeline_id,
                indexing_pipeline_version,
                created_at,
                created_at,
            ))

    @staticmethod
    def finish_extraction_run(
        db: sqlite3.Connection, run_id: str, status: str, message: str, now: str
    ) -> None:
        db.execute(
            "UPDATE extraction_runs SET status=?, error_message=?, updated_at=? WHERE id=?",
            (status, message, now, run_id),
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
        """FTS5 MATCH(kg_objects_fts, trigram)。notebook 维度过滤。返回
        [{object_id, name, score, match:'lexical'}]。q 空 → []。

        `corpus_langs` 是调用方已探得的语料语言(`_notebook_langs`);缺省 None
        = 未探测 = 不过滤,行为逐位不变。见 `corpus_gated_recall_terms`。

        `allow_knn`、`knn_max_term_chars` 与 `routing_stats` 接受并忽略:它们是
        PostgreSQL 适配器的 GiST 访问路径提示与内容无关的内部诊断载体,
        FTS5 的候选查询本来就是有界的。收下这个 kwarg 是签名对等——service 层
        不判 dialect,同一调用必须两侧都合法。"""
        del allow_knn, knn_max_term_chars, routing_stats
        match_query = sqlite_fts_match_expression(q, corpus_langs)
        if not match_query:
            return []
        if allowed_source_ids is not None:
            source_ids = list(dict.fromkeys(allowed_source_ids))
            if not source_ids:
                return []
            placeholders = ",".join("?" for _ in source_ids)
            if authoritative_source_filter:
                rows = db.execute(
                    "SELECT f.object_id,f.name,bm25(kg_objects_fts) AS rank "
                    "FROM kg_objects_fts f JOIN knowledge_objects ko ON ko.id=f.object_id "
                    "WHERE f.notebook_id=? AND kg_objects_fts MATCH ? AND EXISTS ("
                    "SELECT 1 FROM json_each(CASE WHEN json_valid(ko.evidence) "
                    "THEN CASE WHEN json_type(ko.evidence)='array' "
                    "THEN ko.evidence ELSE '[]' END ELSE '[]' END) ev "
                    "WHERE ev.type='object' AND json_extract("
                    "CASE WHEN ev.type='object' THEN ev.value ELSE '{}' END,"
                    f"'$.source_id') IN ({placeholders})) "
                    "ORDER BY rank LIMIT ?",
                    (notebook_id, match_query, *source_ids, k),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT object_id,name,bm25(kg_objects_fts) AS rank "
                    "FROM kg_objects_fts WHERE notebook_id=? "
                    "AND kg_objects_fts MATCH ? AND EXISTS ("
                    "SELECT 1 FROM knowledge_object_sources kos "
                    "WHERE kos.notebook_id=? AND kos.object_id=kg_objects_fts.object_id "
                    f"AND kos.source_id IN ({placeholders})) "
                    "ORDER BY rank LIMIT ?",
                    (notebook_id, match_query, notebook_id, *source_ids, k),
                ).fetchall()
        else:
            rows = db.execute(
            "SELECT object_id, name, bm25(kg_objects_fts) AS rank "
            "FROM kg_objects_fts WHERE notebook_id=? AND kg_objects_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (notebook_id, match_query, k)).fetchall()
        return [{"object_id": r["object_id"], "name": r["name"],
                 "score": -float(r["rank"]), "match": "lexical"} for r in rows]

    @staticmethod
    def chunk_fts_search(
        db, notebook_id: str, q: str, k: int = 30, *,
        allowed_source_ids: Sequence[str] | None = None,
        corpus_langs: Sequence[str] | None = None,
    ) -> List[Dict]:
        """FTS5 MATCH(chunks_fts, trigram)。notebook 维度过滤。返回
        [{chunk_id, score, match:'lexical'}]。q 空 → []。

        `corpus_langs` 同 `fts_search`:两个后端必须探同一组词项,否则同一个库
        的召回口径会随适配器分叉。"""
        match_query = sqlite_fts_match_expression(q, corpus_langs)
        if not match_query:
            return []
        if allowed_source_ids is not None:
            source_ids = list(dict.fromkeys(allowed_source_ids))
            if not source_ids:
                return []
            source_payload = json.dumps(source_ids, ensure_ascii=False)
            rows = db.execute(
                "SELECT f.chunk_id,bm25(chunks_fts) AS rank FROM chunks_fts f "
                "JOIN chunks c ON c.id=f.chunk_id "
                "WHERE f.notebook_id=? AND chunks_fts MATCH ? "
                "AND c.source_id IN (SELECT CAST(value AS TEXT) "
                "FROM json_each(?)) ORDER BY rank LIMIT ?",
                (notebook_id, match_query, source_payload, k),
            ).fetchall()
        else:
            rows = db.execute(
            "SELECT chunk_id, bm25(chunks_fts) AS rank FROM chunks_fts "
            "WHERE notebook_id=? AND chunks_fts MATCH ? ORDER BY rank LIMIT ?",
            (notebook_id, match_query, k)).fetchall()
        return [{"chunk_id": r["chunk_id"], "score": -float(r["rank"]),
                 "match": "lexical"} for r in rows]

    @staticmethod
    def chunk_exact_search(db, notebook_id: str, needle: str, k: int = 50) -> List[Dict]:
        """EXACT substring chunk hits — deliberately NOT lexical_recall_terms.

        `chunk_fts_search` above decomposes its query into an OR-union of
        phrase / word / CJK-trigram terms, which is what recall wants and
        precisely what the identifier fast path must not do: `set_db` has to
        mean `set_db`, not `set` OR `db`. FTS5's trigram tokenizer makes a
        single quoted phrase a literal (case-folded) substring match, so one
        phrase term is the whole query. `"` is doubled so a needle can never
        become FTS5 syntax.

        Returns `[{chunk_id, source_id, section_path, score, match}]` — the
        section coordinates ride along because the caller's next move is always
        "group these hits by section", and a second round-trip to look them up
        would cost one query per identifier for nothing. `score` is native bm25
        ranking; it orders this backend's own hits and is never compared across
        backends.
        """
        term = (needle or "").strip()
        if len(term) < 3 or k <= 0:
            # FTS5 trigram cannot index a shorter term at all; identifier_terms
            # already guarantees >= 4, so this is a defensive floor.
            return []
        match_query = '"' + term.replace('"', '""') + '"'
        rows = db.execute(
            "SELECT chunks_fts.chunk_id AS chunk_id, c.source_id AS source_id, "
            "c.section_path AS section_path, bm25(chunks_fts) AS rank "
            "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.chunk_id "
            "WHERE chunks_fts.notebook_id=? AND chunks_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (notebook_id, match_query, k)).fetchall()
        return [{"chunk_id": r["chunk_id"], "source_id": r["source_id"],
                 "section_path": r["section_path"] or "",
                 "score": -float(r["rank"]), "match": "lexical"} for r in rows]

    @staticmethod
    def backfill_fts(db: sqlite3.Connection, notebook_id: str) -> int:
        """Re-populate kg_objects_fts from knowledge_objects for this notebook.
        Idempotent: deletes existing FTS rows first, then re-inserts from
        knowledge_objects (non-deprecated, non-empty name). Returns the number
        of rows inserted."""
        db.execute("DELETE FROM kg_objects_fts WHERE notebook_id=?", (notebook_id,))
        rows = db.execute(
            "SELECT id, payload FROM knowledge_objects "
            "WHERE notebook_id=? AND status != 'deprecated'",
            (notebook_id,),
        ).fetchall()
        fts_rows = []
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
            except Exception:
                payload = {}
            name = (payload.get("name") or "").strip()
            if name:
                fts_rows.append((r["id"], notebook_id, name))
        if fts_rows:
            db.executemany(
                "INSERT INTO kg_objects_fts(object_id, notebook_id, name) VALUES (?,?,?)",
                fts_rows,
            )
        return len(fts_rows) if fts_rows else 0

    @staticmethod
    def object_meta_rows(db: sqlite3.Connection, ids: List[str]) -> List[sqlite3.Row]:
        placeholders = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT id, object_type, status, payload FROM knowledge_objects "
            f"WHERE id IN ({placeholders})",
            ids,
        ).fetchall()

    # ------------------------------------------------------------- schemas
    @staticmethod
    def lock_schema_registry(db: sqlite3.Connection) -> None:
        # SqliteDatabase.write() already owns the process write seat and starts
        # the write transaction before this hook is called.
        db.execute("SELECT 1")

    @staticmethod
    def schema_rows(db: sqlite3.Connection) -> List[sqlite3.Row]:
        return db.execute("SELECT * FROM object_schemas").fetchall()

    @staticmethod
    def active_schema_rows(db: sqlite3.Connection) -> List[sqlite3.Row]:
        return db.execute(
            "SELECT * FROM object_schemas WHERE status = 'active'"
        ).fetchall()

    @staticmethod
    def schema_row(db: sqlite3.Connection, object_type: str) -> "sqlite3.Row | None":
        return db.execute(
            "SELECT * FROM object_schemas WHERE object_type = ?", (object_type,)
        ).fetchone()

    @staticmethod
    def schema_exists(db: sqlite3.Connection, object_type: str) -> bool:
        return db.execute(
            "SELECT 1 FROM object_schemas WHERE object_type = ?", (object_type,)
        ).fetchone() is not None

    @staticmethod
    def existing_schema_types(db: sqlite3.Connection) -> set:
        return {
            r["object_type"]
            for r in db.execute("SELECT object_type FROM object_schemas").fetchall()
        }

    @staticmethod
    def insert_custom_schema(
        db: sqlite3.Connection,
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
            VALUES (?, ?, ?, ?, ?, ?, ?, 'custom', 'active', '', '', ?, ?)
            """,
            (object_type, plural, fields_json, primary, description, label,
             list_fields_json, now, now),
        )

    @staticmethod
    def insert_induced_schema(
        db: sqlite3.Connection,
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
            VALUES (?, ?, ?, ?, ?, ?, '[]', 'induced', 'proposed', ?, ?, ?, ?)
            """,
            (object_type, plural, fields_json, primary, description, label,
             rationale, notebook_id, now, now),
        )

    @staticmethod
    def update_schema_columns(
        db: sqlite3.Connection,
        object_type: str,
        updates: List[str],
        values: List[object],
    ) -> None:
        db.execute(
            f"UPDATE object_schemas SET {', '.join(updates)} WHERE object_type = ?",
            values,
        )

    @staticmethod
    def delete_schema_row(db: sqlite3.Connection, object_type: str) -> None:
        db.execute(
            "DELETE FROM object_schemas WHERE object_type = ?", (object_type,)
        )

    @staticmethod
    def notebook_schema_rows(
        db: sqlite3.Connection, notebook_id: str
    ) -> List[sqlite3.Row]:
        return db.execute(
            "SELECT * FROM notebook_object_schemas WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def notebook_schema_row(
        db: sqlite3.Connection, notebook_id: str, object_type: str
    ) -> "sqlite3.Row | None":
        return db.execute(
            "SELECT * FROM notebook_object_schemas "
            "WHERE notebook_id = ? AND object_type = ?",
            (notebook_id, object_type),
        ).fetchone()

    @staticmethod
    def insert_notebook_schema(
        db: sqlite3.Connection,
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                notebook_id, object_type, plural, fields_json, primary,
                description, label, list_fields_json, source, status,
                rationale, created_by, now, now,
            ),
        )

    @staticmethod
    def update_notebook_schema_columns(
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        updates: List[str],
        values: List[object],
    ) -> None:
        db.execute(
            f"UPDATE notebook_object_schemas SET {', '.join(updates)} "
            "WHERE notebook_id = ? AND object_type = ?",
            (*values, notebook_id, object_type),
        )

    @staticmethod
    def delete_notebook_schema_row(
        db: sqlite3.Connection, notebook_id: str, object_type: str
    ) -> None:
        db.execute(
            "DELETE FROM notebook_object_schemas "
            "WHERE notebook_id = ? AND object_type = ?",
            (notebook_id, object_type),
        )

    @staticmethod
    def notebook_schema_has_objects(
        db: sqlite3.Connection, notebook_id: str, object_type: str
    ) -> bool:
        return db.execute(
            "SELECT 1 FROM knowledge_objects "
            "WHERE notebook_id = ? AND object_type = ? LIMIT 1",
            (notebook_id, object_type),
        ).fetchone() is not None

    # ------------------------------------------------- Task 26 primitives
    # The last facade SQL bodies, moved verbatim.  All connection-taking —
    # the facade keeps its `_connect`/`_write` boundaries (and the frozen
    # patch seats on them) and passes the possibly-wrapped connection down.

    @staticmethod
    def source_has_kg(db: sqlite3.Connection, source_id: str) -> bool:
        """True iff the source has a complete KG graph.

        Direct/governance rows without extraction history remain compatible.
        When extraction history exists, the latest KG run must be completed so
        a failed legacy partial write cannot masquerade as resumable completion.
        """
        row = db.execute(
            "SELECT EXISTS("
            "  SELECT 1 FROM knowledge_objects ko "
            "  WHERE ko.source_id = ? AND ko.source_id != '' "
            "  AND COALESCE(("
            "    SELECT er.status FROM extraction_runs er "
            "    WHERE er.source_id=ko.source_id AND er.run_type='kg' "
            "    ORDER BY er.created_at DESC, er.rowid DESC LIMIT 1"
            "  ), 'completed')='completed'"
            ")",
            (source_id,),
        ).fetchone()
        return bool(row[0])

    def insert_test_object(
        self,
        db: sqlite3.Connection,
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
               VALUES (?, ?, ?, 'approved', '', ?, '[]', NULL, ?, ?, ?)""",
            (object_id, notebook_id, object_type,
             json.dumps(payload, ensure_ascii=False), source_id, now, now),
        )
        return object_id

    @staticmethod
    def edge_centrality_source_rows(
        db: sqlite3.Connection, notebook_id: str, max_nodes: int
    ) -> "tuple[List[str], List[dict]]":
        """Bounded (top-K by SQL degree) node ids + live relation dicts for the
        edge-betweenness loader (P0-3 semantics moved verbatim):

        1. Degree ranking via GROUP BY over non-rejected knowledge_relations —
           bounded by the distinct node count touched by an edge (isolated
           nodes cannot be edge endpoints and never rank).
        2. When bounded, only relations with BOTH endpoints in the top-K id
           set survive, loaded via json_each(?) (pure read-side join — no
           thousand-placeholder IN and no temp-table write).
        3. Under-K graphs load every live relation plus the full object id
           set — identical result to the unbounded path.
        """
        degree: Dict[str, int] = {}
        for row in db.execute(
            "SELECT source_object_id AS n, COUNT(*) AS c FROM knowledge_relations "
            "WHERE notebook_id = ? AND review_status != 'rejected' "
            "GROUP BY source_object_id", (notebook_id,),
        ).fetchall():
            degree[row["n"]] = degree.get(row["n"], 0) + row["c"]
        for row in db.execute(
            "SELECT target_object_id AS n, COUNT(*) AS c FROM knowledge_relations "
            "WHERE notebook_id = ? AND review_status != 'rejected' "
            "GROUP BY target_object_id", (notebook_id,),
        ).fetchall():
            degree[row["n"]] = degree.get(row["n"], 0) + row["c"]

        if len(degree) > max_nodes:
            # Deterministic top-K: sort by (-degree, id) so ties break on a
            # stable, reproducible key.
            top_ids = [n for n, _ in sorted(
                degree.items(), key=lambda kv: (-kv[1], kv[0])
            )[:max_nodes]]
            top_ids_json = json.dumps(top_ids)
            rel_rows = db.execute(
                "SELECT r.id, r.source_object_id, r.target_object_id, "
                "r.edge_type, r.evidence FROM knowledge_relations r "
                "JOIN json_each(?) s ON s.value = r.source_object_id "
                "JOIN json_each(?) t ON t.value = r.target_object_id "
                "WHERE r.notebook_id = ? AND r.review_status != 'rejected'",
                (top_ids_json, top_ids_json, notebook_id),
            ).fetchall()
            node_ids = top_ids
        else:
            rel_rows = db.execute(
                "SELECT id, source_object_id, target_object_id, edge_type, "
                "evidence FROM knowledge_relations "
                "WHERE notebook_id = ? AND review_status != 'rejected'",
                (notebook_id,),
            ).fetchall()
            obj_rows = db.execute(
                "SELECT id, object_type FROM knowledge_objects WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()
            node_ids = [dict(row) for row in obj_rows]

        if len(degree) > max_nodes:
            object_types = {
                row["id"]: row["object_type"]
                for row in db.execute(
                    "SELECT id, object_type FROM knowledge_objects "
                    "WHERE notebook_id = ? AND id IN (SELECT value FROM json_each(?))",
                    (notebook_id, top_ids_json),
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
            "evidence": json.loads(row["evidence"] or "[]"),
        } for row in rel_rows]
        return node_ids, relations

    @staticmethod
    def concept_cluster_detail_rows(
        db: sqlite3.Connection,
        notebook_id: str,
        canonical_id: str,
        *,
        limit: Optional[int] = None,
        after: str = "",
    ) -> "tuple[List[sqlite3.Row], str]":
        """Cluster member rows (joined onto live knowledge_objects) plus the
        canonical name for one concept cluster. Keyset-paginated by
        ``member_object_id`` (KG-4 hub-cluster fix, R3·T-B2) — mirrors the
        PostgreSQL side's ``ORDER BY ... COLLATE "C"``: SQLite's default TEXT
        collation (BINARY) already compares UTF-8 text byte-for-byte, the
        same ordering, so no explicit COLLATE clause is needed here.
        ``limit=None`` keeps the legacy unbounded (but now deterministically
        ordered) read for internal callers that still need the full member
        set in one shot.

        R3 PR-B P2-1: mirrors the PostgreSQL side's redundant seek predicate
        on ``ko.id`` when ``after`` is set — same equivalence proof (the join
        condition ``ko.id=cc.member_object_id`` makes ``ko.id > after`` and
        ``cc.member_object_id > after`` logically the same set of rows), same
        motivation (give SQLite's planner a seek condition on the
        ``knowledge_objects`` side too, so a mid-cluster page does not have to
        scan that table from its start up to the cursor)."""
        query = (
            "SELECT cc.member_object_id, cc.canonical_name, ko.object_type, ko.payload, ko.evidence "
            "FROM concept_clusters cc "
            "JOIN knowledge_objects ko ON ko.id=cc.member_object_id "
            "WHERE cc.notebook_id=? AND cc.canonical_id=? AND ko.status!='deprecated'"
        )
        params: list = [notebook_id, canonical_id]
        if after:
            query += " AND cc.member_object_id > ? AND ko.id > ?"
            params.append(after)
            params.append(after)
        query += " ORDER BY cc.member_object_id"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cluster_rows = db.execute(query, tuple(params)).fetchall()
        name_row = db.execute(
            "SELECT canonical_name FROM concept_clusters WHERE notebook_id=? AND canonical_id=? LIMIT 1",
            (notebook_id, canonical_id),
        ).fetchone()
        return cluster_rows, (name_row["canonical_name"] if name_row else "")

    @staticmethod
    def concept_cluster_member_total(db: sqlite3.Connection, notebook_id: str, canonical_id: str) -> int:
        """COUNT with the SAME predicate shape as concept_cluster_detail_rows
        (JOIN knowledge_objects ... AND ko.status!='deprecated') — design
        review B8 (hard): a bare ``COUNT(*) FROM concept_clusters`` would
        count deprecated members and make pagination look like it never
        reaches the end."""
        row = db.execute(
            "SELECT COUNT(*) AS c FROM concept_clusters cc "
            "JOIN knowledge_objects ko ON ko.id=cc.member_object_id "
            "WHERE cc.notebook_id=? AND cc.canonical_id=? AND ko.status!='deprecated'",
            (notebook_id, canonical_id),
        ).fetchone()
        return int(row["c"]) if row else 0

    @staticmethod
    def concept_neighbor_rows(
        db: sqlite3.Connection, notebook_id: str, canonical_id: str,
        member_ids: "List[str]", *, batch_size: int = 900,
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

        Membership-probe query shape and safety: ``SELECT member_object_id
        FROM concept_clusters WHERE notebook_id=? AND canonical_id=? AND
        member_object_id IN (batch)``, batched at ``batch_size`` (SQLite has
        no expression-count cap this low, but batching keeps memory/latency
        bounded and matches the PostgreSQL twin's pacing). Empirically
        verified (EXPLAIN QUERY PLAN, 1500-row concept_clusters fixture, 900
        placeholders): ``SEARCH concept_clusters USING COVERING INDEX
        idx_clusters_nb_canonical_member (notebook_id=? AND canonical_id=?
        AND member_object_id=?)`` — the three-column composite index
        (migration 0043) is an exact match for all three predicate columns,
        so SQLite's no-ANALYZE planner picks it as a *covering* index scan
        (no table row lookup at all) rather than falling back to a
        residual-filter scan the way ``GovernanceStore.
        _existing_cluster_members``'s sibling ``duplicate_member_rows``
        precedent warns about (that precedent's hazard is a bare-``id``
        equality competing against a *different*, less-specific
        ``notebook_id`` index; here every predicate column lives together in
        ONE index that is a strict superset of every other index touching
        this table, so there is no less-specific competitor for the planner
        to prefer instead).

        The attached-candidate hydration query below is unconditionally
        batched too (same ``batch_size``) — SQLite's compiled-in
        ``SQLITE_MAX_VARIABLE_NUMBER`` bounds how many ``?`` placeholders one
        statement can carry, so an un-batched ``id IN (...)`` over a hub's
        full attached-candidate set (which used to be unbounded before this
        fix) could exceed it."""
        member_set = set(member_ids)
        placeholders = ",".join("?" for _ in member_set)
        member_list = list(member_set)
        rels_out = db.execute(
            f"SELECT source_object_id, target_object_id, edge_type "
            f"FROM knowledge_relations WHERE notebook_id=? AND source_object_id IN ({placeholders})",
            [notebook_id] + member_list,
        ).fetchall()
        rels_in = db.execute(
            f"SELECT source_object_id, target_object_id, edge_type "
            f"FROM knowledge_relations WHERE notebook_id=? AND target_object_id IN ({placeholders})",
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
                batch_placeholders = ",".join("?" for _ in batch)
                same_cluster_ids.update(
                    row["member_object_id"] for row in db.execute(
                        f"SELECT member_object_id FROM concept_clusters "
                        f"WHERE notebook_id=? AND canonical_id=? "
                        f"AND member_object_id IN ({batch_placeholders})",
                        [notebook_id, canonical_id, *batch],
                    ).fetchall()
                )
            attached_ids -= same_cluster_ids

        by_other: Dict[str, dict] = {}
        if attached_ids:
            attached_list = list(attached_ids)
            attached_rows: List[sqlite3.Row] = []
            for offset in range(0, len(attached_list), batch_size):
                batch = attached_list[offset:offset + batch_size]
                attached_placeholders = ",".join("?" for _ in batch)
                attached_rows.extend(db.execute(
                    f"SELECT id, object_type, payload, evidence FROM knowledge_objects "
                    f"WHERE id IN ({attached_placeholders}) AND status!='deprecated'",
                    batch,
                ).fetchall())
            by_other = {
                row["id"]: {
                    "id": row["id"],
                    "object_type": row["object_type"],
                    "payload": json.loads(row["payload"] or "{}"),
                    "evidence": json.loads(row["evidence"] or "[]"),
                }
                for row in attached_rows
            }
        return rel_edges, by_other
