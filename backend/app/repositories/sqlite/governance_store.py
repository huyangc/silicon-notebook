"""Governance persistence store (Task 13).

Owns the review / cluster / merge / conflict / promotion / whitelist and
knowledge-mutation primitives. Same composition rules as KnowledgeStore:
connection-taking primitives ride the FACADE's transaction boundary (frozen
``_write`` trace patches and SQL-matching failure injectors keep observing
every statement); only the plan-frozen ``get_conflict_candidate`` /
``decided_pairs`` / ``decided_seed_pairs`` readers open their own connection.
SQL text is moved verbatim from the facade.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, List, Optional

from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.services.knowledge_contracts import (
    KNOWLEDGE_STATUSES,
    USABLE_STATUSES,
    PromotionApproval,
)

_REVIEW_STATUSES = frozenset({"pending", "verified", "rejected"})


def seed_fn_for(object_type: str):
    """Return the kg_merge seed function for a KG object type."""
    from app.services.kg_merge import (
        seed_claim, seed_concept, seed_formula, seed_procedure,
    )
    return {
        "concept": seed_concept,
        "claim": seed_claim,
        "formula": seed_formula,
        "procedure": seed_procedure,
    }.get(object_type, seed_claim)


def find_base_dedup_match(
    object_type: str, src_payload: dict, base_objs: List[sqlite3.Row]
) -> str:
    """Exact-seed dedup (v1): return the id of an existing base object whose
    normalized seed matches the source payload, else ''. This works at cold
    start without vectors (the plan's v1 shortcut)."""
    seed_fn = seed_fn_for(object_type)
    src_seed = seed_fn({"name": src_payload.get("name", ""), "payload": src_payload})
    if not src_seed:
        return ""
    for b in base_objs:
        bp = json.loads(b["payload"] or "{}")
        b_seed = seed_fn({"name": bp.get("name", ""), "payload": bp})
        if b_seed and b_seed == src_seed:
            return b["id"]
    return ""


def merge_evidence_lists(base_ev: list, src_ev: list) -> list:
    """Union two evidence lists, deduping on (source_id, element_id, quoted_span)."""
    seen = set()
    merged: list = []
    for ev in [*base_ev, *src_ev]:
        if not isinstance(ev, dict):
            continue
        key = (ev.get("source_id"), ev.get("element_id"), ev.get("quoted_span"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(ev)
    return merged


class GovernanceStore:
    def __init__(self, database: SqliteDatabase, seams) -> None:
        self.database = database
        self.seams = seams

    @staticmethod
    def seed_for(object_type: str):
        return seed_fn_for(object_type)

    @staticmethod
    def find_base_match(object_type: str, payload: dict, rows) -> str:
        return find_base_dedup_match(object_type, payload, rows)

    @staticmethod
    def merge_evidence(base_evidence: list, source_evidence: list) -> list:
        return merge_evidence_lists(base_evidence, source_evidence)

    # ------------------------------------------------ lifecycle projections
    @staticmethod
    def sweep_orphan_clusters(db: sqlite3.Connection, notebook_id: str) -> int:
        cur = db.execute(
            "DELETE FROM concept_clusters WHERE notebook_id=? AND member_object_id NOT IN "
            "(SELECT id FROM knowledge_objects WHERE notebook_id=?)",
            (notebook_id, notebook_id),
        )
        return int(cur.rowcount)

    @staticmethod
    def incremental_cluster_rows(db: sqlite3.Connection, notebook_id: str, object_type: str):
        return db.execute(
            "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
            "WHERE notebook_id=? AND object_type=?", (notebook_id, object_type),
        ).fetchall()

    @staticmethod
    def merge_candidate_pairs(db: sqlite3.Connection, notebook_id: str, statuses):
        values = tuple(statuses)
        if values == ("pending",):
            return db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=? AND status='pending'", (notebook_id,),
            ).fetchall()
        if values == ("confirmed", "rejected", "deferred"):
            return db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=? AND status IN ('confirmed','rejected','deferred')",
                (notebook_id,),
            ).fetchall()
        if values == ("confirmed", "rejected", "deferred", "pending"):
            return db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=? AND status IN ('confirmed','rejected','deferred','pending')",
                (notebook_id,),
            ).fetchall()
        raise ValueError(f"unsupported lifecycle merge statuses: {values!r}")

    @staticmethod
    def valid_object_ids(db: sqlite3.Connection, object_ids):
        return KnowledgeStore.valid_object_ids(db, object_ids)

    # ------------------------------------------------------------- review
    @staticmethod
    def review_queue_rows(
        connection: sqlite3.Connection, notebook_id: str
    ) -> "tuple[List[sqlite3.Row], List[sqlite3.Row]]":
        relations = connection.execute(
            "SELECT kr.id, kr.source_object_id, kr.target_object_id, "
            "kr.edge_type, kr.evidence, kr.source_id, kr.review_status, "
            "ko_s.object_type AS src_type, ko_t.object_type AS tgt_type "
            "FROM knowledge_relations kr "
            "LEFT JOIN knowledge_objects ko_s ON ko_s.id = kr.source_object_id "
            "LEFT JOIN knowledge_objects ko_t ON ko_t.id = kr.target_object_id "
            "WHERE kr.notebook_id = ? AND kr.review_status != 'rejected'",
            (notebook_id,),
        ).fetchall()
        objects = connection.execute(
            "SELECT id, object_type, payload FROM knowledge_objects "
            "WHERE notebook_id = ?", (notebook_id,)
        ).fetchall()
        return relations, objects

    @staticmethod
    def update_edge_review(
        connection: sqlite3.Connection, notebook_id: str, relation_id: str, status: str
    ) -> None:
        cur = connection.execute(
            "UPDATE knowledge_relations SET review_status=? "
            "WHERE id=? AND notebook_id=?",
            (status, relation_id, notebook_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"relation {relation_id!r} not found in notebook {notebook_id!r}")

    # ------------------------------------------------------------ clusters
    @staticmethod
    def delete_clusters(
        connection: sqlite3.Connection, notebook_id: str, object_type: str
    ) -> None:
        connection.execute(
            "DELETE FROM concept_clusters WHERE notebook_id=? AND object_type=?",
            (notebook_id, object_type))

    def insert_clusters(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        rows: List[dict],
        now: str,
    ) -> int:
        """Insert cluster member rows, skipping member ids already present in
        the (notebook, type) slice — the append_clusters idempotency contract.
        After delete_clusters in the same transaction the existing set is
        empty, so the write_clusters path inserts every row unchanged."""
        existing = {r["member_object_id"] for r in connection.execute(
            "SELECT member_object_id FROM concept_clusters WHERE notebook_id=? AND object_type=?",
            (notebook_id, object_type)).fetchall()}
        added = 0
        for r in rows:
            if r["member_object_id"] in existing:
                continue
            connection.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.seams.new_id("cc"), notebook_id, r["canonical_id"],
                 r["member_object_id"], r["canonical_name"], object_type,
                 r.get("canonical_description", ""), now))
            added += 1
        return added

    # --------------------------------------------------------------- merge
    def insert_merge_candidate(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        a: str,
        b: str,
        score: float,
        now: str,
        *,
        id_prefix: str = "mc",
    ) -> None:
        # id_prefix keeps the historical surrogate prefixes byte-stable:
        # write_merge_candidate mints "mc-", incremental fusion minted "cm-".
        connection.execute(
            "INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) VALUES (?,?,?,?,?, 'pending', ?, ?)",
            (self.seams.new_id(id_prefix), notebook_id, a, b, score, now, now))

    def write_merge_candidate(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        a: str,
        b: str,
        score: float,
        now: str,
    ) -> None:
        self.insert_merge_candidate(connection, notebook_id, a, b, score, now)

    @staticmethod
    def pending_merges(connection: sqlite3.Connection, notebook_id: str) -> List[dict]:
        rows = connection.execute(
            "SELECT * FROM concept_merge_candidates WHERE notebook_id=? AND status='pending'",
            (notebook_id,)).fetchall()
        return [{"id": r["id"], "canonical_a": r["canonical_a"], "canonical_b": r["canonical_b"], "score": r["score"], "status": r["status"]} for r in rows]

    @staticmethod
    def pending_merges_batch(
        connection: sqlite3.Connection, notebook_id: str, limit: int
    ) -> List[dict]:
        """Bounded fetch of pending merge candidates, LIMITed in SQL. No ORDER
        BY — SQLite returns rows in rowid order by default absent one, matching
        the implicit order the old ``pending_merges(nb)[:limit]`` slice relied
        on (order-locked to the previous behavior for equal-size batches)."""
        rows = connection.execute(
            "SELECT * FROM concept_merge_candidates WHERE notebook_id=? AND status='pending' LIMIT ?",
            (notebook_id, limit),
        ).fetchall()
        return [{"id": r["id"], "canonical_a": r["canonical_a"], "canonical_b": r["canonical_b"], "score": r["score"], "status": r["status"]} for r in rows]

    @staticmethod
    def has_pending_merges(connection: sqlite3.Connection, notebook_id: str) -> bool:
        row = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM concept_merge_candidates WHERE notebook_id=? AND status='pending') AS e",
            (notebook_id,),
        ).fetchone()
        return bool(row["e"])

    @staticmethod
    def set_merge_decision(
        connection: sqlite3.Connection,
        notebook_id: str,
        candidate_id: str,
        status: str,
        now: str,
    ) -> None:
        if status not in ("confirmed", "rejected"):
            raise ValueError(f"invalid merge status: {status!r}")
        connection.execute(
            "UPDATE concept_merge_candidates SET status=?, updated_at=? WHERE id=? AND notebook_id=?",
            (status, now, candidate_id, notebook_id))

    @staticmethod
    def record_merge_review(
        connection: sqlite3.Connection,
        notebook_id: str,
        candidate_id: str,
        status: str,
        confidence: float,
        rationale: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE concept_merge_candidates
            SET status=?, confidence=?, rationale=?, reviewed_by='llm', updated_at=?
            WHERE id=? AND notebook_id=?
            """,
            (status, confidence, rationale, now, candidate_id, notebook_id),
        )

    @staticmethod
    def delete_pending_merges(connection: sqlite3.Connection, notebook_id: str) -> None:
        connection.execute(
            "DELETE FROM concept_merge_candidates WHERE notebook_id=? AND status='pending'",
            (notebook_id,))

    @staticmethod
    def insert_pending_merge_rows(
        connection: sqlite3.Connection, rows: List[tuple]
    ) -> None:
        """Refresh pending candidates in the caller's transaction. Rows carry
        seed_a/seed_b — the rebuild-stable decision keys (PR#145) — verbatim."""
        connection.executemany(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?, 'pending', ?, ?)",
            rows)

    def decided_pairs(self, notebook_id: str) -> Dict[tuple, str]:
        with self.database.connect() as db:
            rows = db.execute("SELECT canonical_a, canonical_b, status FROM concept_merge_candidates WHERE notebook_id=? AND status IN ('confirmed','rejected')", (notebook_id,)).fetchall()
        return {(r["canonical_a"], r["canonical_b"]): r["status"] for r in rows}

    def decided_seed_pairs(self, notebook_id: str) -> Dict[frozenset, str]:
        """{frozenset({seed_a, seed_b}): status} for confirmed/rejected/deferred.

        Seed-name keys are STABLE across rebuilds (canonical ids shift when a
        cluster's min-member changes; seed names don't). Legacy rows written
        before the seed_a/seed_b columns existed carry '' → fall back to
        strip-"K-"(canonical), matching the old decided_pairs key derivation."""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT canonical_a, canonical_b, seed_a, seed_b, status "
                "FROM concept_merge_candidates WHERE notebook_id=? "
                "AND status IN ('confirmed','rejected','deferred')",
                (notebook_id,),
            ).fetchall()
        def _strip(cid: str) -> str:
            return cid[2:] if cid.startswith("K-") else cid
        out: Dict[frozenset, str] = {}
        for r in rows:
            a = r["seed_a"] or _strip(r["canonical_a"])
            b = r["seed_b"] or _strip(r["canonical_b"])
            out[frozenset((a, b))] = r["status"]
        return out

    # ------------------------------------------------------ merge review job
    @staticmethod
    def merge_review_job_row(
        connection: sqlite3.Connection, notebook_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT status,total,done,error FROM merge_review_jobs WHERE notebook_id=?",
            (notebook_id,)).fetchone()

    @staticmethod
    def begin_merge_review_job(
        connection: sqlite3.Connection, notebook_id: str, now: str
    ) -> "int | None":
        """Single-flight start: returns None when a job is already running,
        else upserts the running row and returns the pending total — one
        atomic block inside the caller's write transaction."""
        row = connection.execute("SELECT status FROM merge_review_jobs WHERE notebook_id=?",
                                 (notebook_id,)).fetchone()
        if row is not None and row["status"] == "running":
            return None
        total = connection.execute(
            "SELECT COUNT(*) c FROM concept_merge_candidates "
            "WHERE notebook_id=? AND status='pending'", (notebook_id,)).fetchone()["c"]
        connection.execute(
            """
            INSERT INTO merge_review_jobs (notebook_id,status,total,done,started_at,updated_at,error)
            VALUES (?, 'running', ?, 0, ?, ?, '')
            ON CONFLICT(notebook_id) DO UPDATE SET
              status='running', total=excluded.total, done=0,
              started_at=excluded.started_at, updated_at=excluded.updated_at, error=''
            """,
            (notebook_id, total, now, now))
        return int(total)

    @staticmethod
    def set_merge_review_progress(
        connection: sqlite3.Connection, notebook_id: str, done: int, now: str
    ) -> None:
        connection.execute("UPDATE merge_review_jobs SET done=?, updated_at=? WHERE notebook_id=?",
                           (done, now, notebook_id))

    @staticmethod
    def finish_merge_review_job(
        connection: sqlite3.Connection, notebook_id: str, status: str, error: str, now: str
    ) -> None:
        connection.execute("UPDATE merge_review_jobs SET status=?, error=?, updated_at=? WHERE notebook_id=?",
                           (status, error, now, notebook_id))

    # ------------------------------------------------------------ conflicts
    def write_conflict_candidate(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        kind: str,
        left_ref: str,
        right_ref: str,
        conflict_type: Optional[str],
        resolution: Optional[str],
        winner_ref: Optional[str],
        resolved_payload: Optional[str],
        confidence: Optional[float],
        rationale: Optional[str],
        now: str,
    ) -> str:
        cid = self.seams.new_id("kcc")
        connection.execute(
            """
            INSERT INTO kg_conflict_candidates
              (id, notebook_id, kind, left_ref, right_ref,
               conflict_type, resolution, winner_ref, resolved_payload,
               confidence, rationale, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (cid, notebook_id, kind, left_ref, right_ref,
             conflict_type, resolution, winner_ref, resolved_payload,
             confidence, rationale, now, now),
        )
        return cid

    @staticmethod
    def pending_conflicts(
        connection: sqlite3.Connection, notebook_id: str
    ) -> List[dict]:
        rows = connection.execute(
            "SELECT * FROM kg_conflict_candidates WHERE notebook_id=? AND status='pending'",
            (notebook_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def set_conflict_status(
        connection: sqlite3.Connection,
        notebook_id: str,
        candidate_id: str,
        status: str,
        now: str,
    ) -> None:
        if status not in ("applied", "rejected"):
            raise ValueError(f"invalid conflict status: {status!r}")
        cur = connection.execute(
            "UPDATE kg_conflict_candidates SET status=?, updated_at=? "
            "WHERE id=? AND notebook_id=?",
            (status, now, candidate_id, notebook_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"conflict candidate {candidate_id!r} not found")

    def get_conflict_candidate(
        self, notebook_id: str, candidate_id: str
    ) -> Optional[dict]:
        """Fetch one conflict candidate inside its notebook authorization scope."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM kg_conflict_candidates WHERE id=? AND notebook_id=?",
                (candidate_id, notebook_id),
            ).fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------ whitelist
    @staticmethod
    def concept_whitelist_terms(connection: sqlite3.Connection) -> set:
        return {r["term"] for r in connection.execute("SELECT term FROM concept_whitelist").fetchall()}

    @staticmethod
    def concept_whitelist_rows(connection: sqlite3.Connection) -> List[sqlite3.Row]:
        return connection.execute(
            "SELECT term, note, created_at FROM concept_whitelist ORDER BY term"
        ).fetchall()

    @staticmethod
    def add_whitelist_term(
        connection: sqlite3.Connection, term: str, note: str, now: str
    ) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO concept_whitelist (term, note, created_at) VALUES (?, ?, ?)",
            (term, note, now),
        )

    @staticmethod
    def remove_whitelist_term(connection: sqlite3.Connection, term: str) -> None:
        connection.execute("DELETE FROM concept_whitelist WHERE term = ?", (term,))

    # ------------------------------------------------------------ promotion
    @staticmethod
    def promotion_object_type_row(
        connection: sqlite3.Connection, notebook_id: str, object_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT object_type FROM knowledge_objects WHERE id=? AND notebook_id=?",
            (object_id, notebook_id),
        ).fetchone()

    @staticmethod
    def notebook_tier_row(
        connection: sqlite3.Connection, notebook_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
        ).fetchone()

    @staticmethod
    def promotion_object_rows(
        connection: sqlite3.Connection, object_ids: List[str]
    ) -> List[sqlite3.Row]:
        if not object_ids:
            return []
        placeholders = ",".join("?" for _ in object_ids)
        return connection.execute(
            f"SELECT id, payload, evidence FROM knowledge_objects "
            f"WHERE id IN ({placeholders})",
            object_ids,
        ).fetchall()

    @staticmethod
    def safe_memory_evidence(
        connection: sqlite3.Connection, notebook_id: str, provenance: dict
    ) -> List[dict]:
        """Resolve trusted Ask citations to current source elements.

        Agent-supplied evidence references are intentionally ignored here:
        confirmation accepts the Memory text, not unverified source claims.
        Returned quotes come from stored elements rather than request payloads.
        """
        citations = provenance.get("citations")
        if not isinstance(citations, list):
            return []
        pairs: list[tuple[str, str]] = []
        for citation in citations[:50]:
            if not isinstance(citation, dict):
                continue
            source_id = str(citation.get("source_id") or "")
            element_id = str(citation.get("element_id") or "")
            if source_id and element_id:
                pairs.append((source_id, element_id))
        if not pairs:
            return []
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for source_id, element_id in pairs:
            if (source_id, element_id) in seen:
                continue
            seen.add((source_id, element_id))
            row = connection.execute(
                "SELECT s.id AS source_id,s.title AS source_title,e.id AS element_id,"
                "e.element_type,e.location_label,e.text FROM sources s "
                "JOIN source_elements e ON e.source_id=s.id "
                "WHERE s.id=? AND e.id=? AND s.notebook_id=?",
                (source_id, element_id, notebook_id),
            ).fetchone()
            if row is None:
                continue
            out.append(
                {
                    "source_id": row["source_id"],
                    "source_title": row["source_title"],
                    "element_id": row["element_id"],
                    "element_type": row["element_type"],
                    "location_label": row["location_label"],
                    "quoted_span": str(row["text"] or "")[:500],
                    "confidence": 1.0,
                }
            )
        return out

    @staticmethod
    def object_payload_row(
        connection: sqlite3.Connection, object_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT payload FROM knowledge_objects WHERE id=?", (object_id,)
        ).fetchone()

    @staticmethod
    def conflict_resolution_rows(
        connection: sqlite3.Connection, notebook_id: str
    ) -> "tuple[List[sqlite3.Row], List[sqlite3.Row], sqlite3.Row | None]":
        objects = connection.execute(
            "SELECT id, object_type, payload, evidence, status "
            "FROM knowledge_objects "
            "WHERE notebook_id=? AND status != 'deprecated'",
            (notebook_id,),
        ).fetchall()
        vectors = connection.execute(
            "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?",
            (notebook_id,),
        ).fetchall()
        notebook = connection.execute(
            "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
        ).fetchone()
        return objects, vectors, notebook

    @staticmethod
    def promotion_candidate_row(
        connection: sqlite3.Connection, candidate_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT * FROM promotion_candidates WHERE id=?", (candidate_id,)
        ).fetchone()

    @staticmethod
    def active_promotion_for_object(
        connection: sqlite3.Connection, object_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT * FROM promotion_candidates "
            "WHERE object_id=? AND status NOT IN ('approved','rejected')",
            (object_id,),
        ).fetchone()

    @staticmethod
    def insert_promotion_candidate(
        connection: sqlite3.Connection,
        cand_id: str,
        notebook_id: str,
        object_id: str,
        object_type: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO promotion_candidates
            (id, notebook_id, object_id, object_type, status, reason,
             reviewed_by, base_match_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'proposed', '', '', '', ?, ?)
            """,
            (cand_id, notebook_id, object_id, object_type, now, now),
        )

    @staticmethod
    def promotion_queue_rows(
        connection: sqlite3.Connection, status_filter: Optional[str]
    ) -> List[sqlite3.Row]:
        if status_filter:
            return connection.execute(
                "SELECT * FROM promotion_candidates WHERE status=? "
                "ORDER BY created_at ASC, id ASC",
                (status_filter,),
            ).fetchall()
        return connection.execute(
            "SELECT * FROM promotion_candidates "
            "WHERE status IN ('proposed','under_review') "
            "ORDER BY created_at ASC, id ASC"
        ).fetchall()

    @staticmethod
    def first_base_notebook_row(
        connection: sqlite3.Connection,
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT id FROM notebooks WHERE tier='base' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()

    @staticmethod
    def first_admin_user_id(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT id FROM users WHERE role='admin' ORDER BY created_at,id LIMIT 1"
        ).fetchone()
        return str(row["id"]) if row is not None else ""

    @staticmethod
    def approved_base_object_id(
        connection: sqlite3.Connection, base_notebook_id: str, candidate_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT id FROM knowledge_objects "
            "WHERE notebook_id=? AND source_candidate_id=? "
            "ORDER BY created_at ASC, id ASC LIMIT 1",
            (base_notebook_id, candidate_id),
        ).fetchone()

    @staticmethod
    def approved_memory_base_object_ids(
        connection: sqlite3.Connection, base_notebook_id: str, candidate_id: str
    ) -> List[str]:
        return [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM knowledge_objects "
                "WHERE notebook_id=? AND source_candidate_id=? ORDER BY id",
                (base_notebook_id, candidate_id),
            ).fetchall()
        ]

    def approve_memory_promotion_in_transaction(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        candidates: List[dict],
        evidence: List[dict],
        reviewer_id: str,
        now: str,
    ) -> dict:
        """Promote a sanitized Memory extraction through the normal base dedupe path."""
        cand = self.promotion_candidate_row(connection, candidate_id)
        if cand is None:
            raise KeyError(candidate_id)
        if cand["status"] == "rejected":
            raise ValueError("cannot approve a rejected promotion candidate")
        base_row = self.first_base_notebook_row(connection)
        if base_row is None:
            raise ValueError("no base notebook — mark one with mark_notebook_base() first")
        base_nb_id = str(base_row["id"])
        if cand["status"] == "approved":
            return {
                "base_notebook_id": base_nb_id,
                "base_object_ids": self.approved_memory_base_object_ids(
                    connection, base_nb_id, candidate_id
                ),
                "created_object_ids": [],
                "merged_object_ids": [],
            }

        base_object_ids: list[str] = []
        created_object_ids: list[str] = []
        merged_object_ids: list[str] = []
        for extracted in candidates:
            object_type = str(extracted.get("object_type") or "")
            payload = extracted.get("payload")
            if object_type not in {"concept", "claim", "formula", "procedure"}:
                continue
            if not isinstance(payload, dict) or not payload:
                continue
            base_objs = connection.execute(
                "SELECT id,payload,evidence FROM knowledge_objects "
                "WHERE notebook_id=? AND object_type=? AND status IN ({})".format(
                    ",".join("?" for _ in USABLE_STATUSES)
                ),
                (base_nb_id, object_type, *USABLE_STATUSES),
            ).fetchall()
            base_match_id = find_base_dedup_match(object_type, payload, base_objs)
            if base_match_id:
                matched = next(row for row in base_objs if row["id"] == base_match_id)
                merged_evidence = merge_evidence_lists(
                    json.loads(matched["evidence"] or "[]"), evidence
                )
                connection.execute(
                    "UPDATE knowledge_objects SET evidence=?,updated_at=? WHERE id=?",
                    (json.dumps(merged_evidence, ensure_ascii=False), now, base_match_id),
                )
                KnowledgeStore.replace_object_sources(
                    connection,
                    base_match_id,
                    base_nb_id,
                    json.dumps(merged_evidence, ensure_ascii=False),
                )
                base_object_id = base_match_id
                merged_object_ids.append(base_object_id)
            else:
                base_object_id = self.seams.new_id("ko")
                connection.execute(
                    "INSERT INTO knowledge_objects "
                    "(id,notebook_id,object_type,status,owner,payload,evidence,"
                    "source_candidate_id,source_id,created_at,updated_at) "
                    "VALUES (?,?,?,'approved','',?,?,?,'',?,?)",
                    (
                        base_object_id,
                        base_nb_id,
                        object_type,
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(evidence, ensure_ascii=False),
                        candidate_id,
                        now,
                        now,
                    ),
                )
                KnowledgeStore.replace_object_sources(
                    connection,
                    base_object_id,
                    base_nb_id,
                    json.dumps(evidence, ensure_ascii=False),
                )
                created_object_ids.append(base_object_id)
            base_object_ids.append(base_object_id)

        if not base_object_ids:
            raise ValueError("Memory produced no supported KG candidates")
        connection.execute(
            "UPDATE promotion_candidates SET status='approved',base_match_id=?,"
            "reviewed_by=?,updated_at=? WHERE id=?",
            (merged_object_ids[0] if merged_object_ids else "", reviewer_id, now, candidate_id),
        )
        return {
            "base_notebook_id": base_nb_id,
            "base_object_ids": base_object_ids,
            "created_object_ids": created_object_ids,
            "merged_object_ids": merged_object_ids,
        }

    def approve_promotion_in_transaction(
        self, connection: sqlite3.Connection, candidate_id: str, now: str
    ) -> PromotionApproval:
        """The in-transaction body of approve_promotion: copy the personal
        object into the base corpus, deduplicating against existing base
        objects of the same type via the kg_merge seed clustering. The caller
        owns the write transaction and the post-commit hooks (embed /
        invalidate / dirty)."""
        cand = self.promotion_candidate_row(connection, candidate_id)
        if cand is None:
            raise KeyError(candidate_id)
        if cand["status"] == "rejected":
            raise ValueError("cannot approve a rejected promotion candidate")
        object_type = cand["object_type"]

        base_row = self.first_base_notebook_row(connection)
        if base_row is None:
            raise ValueError("no base notebook — mark one with mark_notebook_base() first")
        base_nb_id = base_row["id"]

        # Idempotency: if already approved, return the existing base object.
        if cand["status"] == "approved":
            existing = self.approved_base_object_id(connection, base_nb_id, candidate_id)
            base_object_id = existing["id"] if existing else (cand["base_match_id"] or "")
            return PromotionApproval(
                candidate_id=candidate_id,
                source_notebook_id=cand["notebook_id"],
                source_object_id=cand["object_id"],
                base_notebook_id=base_nb_id,
                base_object_id=base_object_id,
                created_new_object=False,
            )

        # Fetch the personal object being promoted.
        src = connection.execute(
            "SELECT * FROM knowledge_objects WHERE id=?", (cand["object_id"],)
        ).fetchone()
        if src is None:
            raise KeyError(cand["object_id"])
        src_payload = json.loads(src["payload"] or "{}")
        src_evidence = json.loads(src["evidence"] or "[]")

        # Cross-corpus dedup against existing base objects of the same type.
        base_objs = connection.execute(
            "SELECT id, payload, evidence FROM knowledge_objects "
            "WHERE notebook_id=? AND object_type=? AND status IN ({})".format(
                ",".join("?" for _ in USABLE_STATUSES)
            ),
            (base_nb_id, object_type, *USABLE_STATUSES),
        ).fetchall()
        base_match_id = find_base_dedup_match(object_type, src_payload, base_objs)

        if base_match_id:
            # Merge: combine evidence into the matched base object; keep its id.
            matched = next(b for b in base_objs if b["id"] == base_match_id)
            merged_evidence = merge_evidence_lists(
                json.loads(matched["evidence"] or "[]"), src_evidence
            )
            connection.execute(
                "UPDATE knowledge_objects SET evidence=?, updated_at=? WHERE id=?",
                (json.dumps(merged_evidence, ensure_ascii=False), now, base_match_id),
            )
            base_object_id = base_match_id
            created_new_object = False
            KnowledgeStore.replace_object_sources(
                connection, base_object_id, base_nb_id,
                json.dumps(merged_evidence, ensure_ascii=False),
            )
        else:
            # No match: insert a fresh base object at status='approved'.
            base_object_id = self.seams.new_id("ko")
            connection.execute(
                """
                INSERT INTO knowledge_objects
                (id, notebook_id, object_type, status, owner, payload, evidence,
                 source_candidate_id, source_id, created_at, updated_at)
                VALUES (?, ?, ?, 'approved', '', ?, ?, ?, '', ?, ?)
                """,
                (
                    base_object_id,
                    base_nb_id,
                    object_type,
                    json.dumps(src_payload, ensure_ascii=False),
                    json.dumps(src_evidence, ensure_ascii=False),
                    candidate_id,
                    now,
                    now,
                ),
            )
            created_new_object = True
            KnowledgeStore.replace_object_sources(
                connection, base_object_id, base_nb_id,
                json.dumps(src_evidence, ensure_ascii=False),
            )

        connection.execute(
            "UPDATE promotion_candidates "
            "SET status='approved', base_match_id=?, reviewed_by='curator', updated_at=? "
            "WHERE id=?",
            (base_match_id, now, candidate_id),
        )
        return PromotionApproval(
            candidate_id=candidate_id,
            source_notebook_id=cand["notebook_id"],
            source_object_id=cand["object_id"],
            base_notebook_id=base_nb_id,
            base_object_id=base_object_id,
            created_new_object=created_new_object,
        )

    @staticmethod
    def set_promotion_rejected(
        connection: sqlite3.Connection,
        candidate_id: str,
        reason: str,
        now: str,
        reviewer_id: str = "curator",
    ) -> None:
        connection.execute(
            "UPDATE promotion_candidates "
            "SET status='rejected', reason=?, reviewed_by=?, updated_at=? "
            "WHERE id=?",
            (reason, reviewer_id, now, candidate_id),
        )

    # -------------------------------------------------- knowledge mutation
    @staticmethod
    def update_object_in_transaction(
        connection: sqlite3.Connection,
        notebook_id: str,
        object_id: str,
        payload,
        now: str,
    ) -> sqlite3.Row:
        """The in-transaction body of update_knowledge: validate, apply the
        partial update and return the refetched row. ``payload`` is the
        KnowledgeUpdate model (status/payload/owner partial edit)."""
        row = connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
            (object_id, notebook_id),
        ).fetchone()
        if row is None:
            raise KeyError(object_id)
        if payload.status is not None and payload.status not in KNOWLEDGE_STATUSES:
            raise ValueError(f"invalid status: {payload.status}")
        new_payload = (
            json.dumps(payload.payload, ensure_ascii=False)
            if payload.payload is not None
            else row["payload"]
        )
        new_status = payload.status if payload.status is not None else row["status"]
        new_owner = payload.owner if payload.owner is not None else row["owner"]
        # Stamp last_reviewed whenever a curator changes status.
        last_reviewed = now if payload.status is not None else (
            row["last_reviewed"] if "last_reviewed" in row.keys() else ""
        )
        connection.execute(
            "UPDATE knowledge_objects SET payload = ?, status = ?, owner = ?, "
            "last_reviewed = ?, updated_at = ? WHERE id = ? AND notebook_id = ?",
            (new_payload, new_status, new_owner, last_reviewed, now, object_id, notebook_id),
        )
        return connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
            (object_id, notebook_id),
        ).fetchone()

    @staticmethod
    def merge_objects_in_transaction(
        connection: sqlite3.Connection,
        notebook_id: str,
        source_id: str,
        into_id: str,
        now: str,
    ) -> sqlite3.Row:
        """The in-transaction body of merge_knowledge: fold source evidence
        into the target, maintain the reverse index, deprecate the source in
        place, and return the refetched target row."""
        src = connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
            (source_id, notebook_id),
        ).fetchone()
        tgt = connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
            (into_id, notebook_id),
        ).fetchone()
        if src is None or tgt is None:
            raise KeyError(source_id if src is None else into_id)
        if src["object_type"] != tgt["object_type"]:
            raise ValueError("can only merge knowledge objects of the same type")
        merged: List[dict] = json.loads(tgt["evidence"] or "[]")
        seen = {(e.get("element_id"), e.get("quoted_span")) for e in merged}
        for item in json.loads(src["evidence"] or "[]"):
            key = (item.get("element_id"), item.get("quoted_span"))
            if key not in seen:
                merged.append(item)
                seen.add(key)
        merged_json = json.dumps(merged, ensure_ascii=False)
        connection.execute(
            "UPDATE knowledge_objects SET evidence = ?, updated_at = ? WHERE id = ?",
            (merged_json, now, into_id),
        )
        # into_id's evidence gained items (possibly new source_ids) from source_id;
        # source_id itself only flips status (its own evidence/reverse-index rows
        # are unchanged and stay correct until the object is actually deleted).
        KnowledgeStore.replace_object_sources(connection, into_id, notebook_id, merged_json)
        connection.execute(
            "UPDATE knowledge_objects SET status = 'deprecated', last_reviewed = ?, updated_at = ? WHERE id = ?",
            (now, now, source_id),
        )
        return connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = ?", (into_id,)
        ).fetchone()
