"""Knowledge governance orchestration (Task 15 seed, Task 16 full surface).

Owns the governance surface the facade previously carried inline: the edge
trust review queue (review_queue / set_edge_review), concept-merge candidates
and their LLM review job (write/pending/decide/confirm/reject,
review_pending_merges / run_merge_review_job), KG conflict candidates and the
compound conflict resolution (write/pending/status/get,
apply_conflict_resolution / confirm_conflict / reject_conflict /
resolve_notebook_conflicts), the promotion state machine (propose / list /
approve / reject), knowledge mutations (update_knowledge / merge_knowledge /
find_duplicates) and the concept whitelist.

Composition rules (Gate 5): no facade import. Persistence goes through the
injected Task-13 stores (GovernanceStore / KnowledgeStore); transactions ride
the injected ``write``/``connect`` seats (the facade's ``_write``/``_connect``
compatibility seams, resolved at call time so transaction-counting /
failure-injection monkeypatches keep observing every commit boundary).
Post-commit KG side effects keep funnelling through the facade's
``_invalidate_unified_cache`` / ``_mark_unified_kg_dirty`` wrappers (injected
late) — the Task-14 coordinator stays the single dirty entry and the frozen
per-operation mutation phase matrix (candidate transaction; discard/modify
double dirty bumps; dirty-then-invalidate vs invalidate-then-dirty
asymmetries) is unchanged.  The retrieval-owned helpers the governance flows
compose (edge-centrality cache, payload embed, RetrievedKnowledge/RuleCard
formatting, the knowledge-objects reader) stay facade-owned until their own
domain moves — injected as late-bound ports.

One compound port survives Task 16: ``set_conflict_status`` resolves the
FACADE method at call time, because the frozen confirm_conflict phase
contract patches that facade wrapper and must keep intercepting the
mutation-commits-before-candidate-status boundary.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Dict, List, Optional

from app.core.config import Settings
from app.core.event_logging import EventLogger
from app.models.schemas import (
    DuplicateGroup,
    Evidence,
    KnowledgeRef,
    KnowledgeUpdate,
    MergeRequest,
    RuleCard,
)
from app.repositories.sqlite.governance_store import GovernanceStore
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.services.retrieval import cosine, keyword_score


# SQLite default SQLITE_MAX_VARIABLE_NUMBER-safe chunk size for `IN (...)`
# placeholder lists (mirrors the facade's frozen `_IN_CHUNK` convention).
_IN_CHUNK = 900


def promotion_row_to_dict(row: sqlite3.Row, *, payload=None, evidence=None) -> dict:
    """Map a promotion_candidates row to the PromotionCandidate-shaped dict.
    payload/evidence are denormalised from knowledge_objects when listing."""
    return {
        "id": row["id"],
        "notebook_id": row["notebook_id"],
        "object_id": row["object_id"],
        "object_type": row["object_type"],
        "status": row["status"],
        "reason": row["reason"],
        "reviewed_by": row["reviewed_by"],
        "base_match_id": row["base_match_id"],
        "created_at": row["created_at"],
        "payload": payload if payload is not None else {},
        "evidence": evidence if evidence is not None else [],
    }


def knowledge_headline(object_type: str, payload: dict) -> str:
    keys = {
        "rule": ("title", "statement"),
        "method": ("name", "use_when"),
        "risk": ("title", "description"),
        "glossary": ("term", "definition"),
        "case": ("symptom", "context"),
        "checklist": ("question",),
        # KG node types: text lives in payload["name"]
        "claim": ("name", "statement"),
        "formula": ("name", "statement"),
        "procedure": ("name", "title"),
        "concept": ("name", "term", "definition"),
        "finding": ("name", "statement", "metric"),
        "principle": ("statement", "rationale"),
        "example": ("title", "problem"),
    }.get(object_type, ("name", "title", "statement", "term", "question"))
    for key in keys:
        value = str(payload.get(key, "")).strip()
        if value:
            return value[:120]
    return object_type


def payload_join(payload: dict) -> str:
    parts: List[str] = []
    for key, value in payload.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
    return " ".join(parts)


class KnowledgeGovernanceService:
    # --- Track E: edge trust review statuses (frozen vocabulary) -----------
    _REVIEW_STATUSES = frozenset({"pending", "verified", "rejected"})

    def __init__(
        self,
        *,
        settings: Settings,
        event_log: EventLogger,
        governance_store: GovernanceStore,
        knowledge: KnowledgeStore,
        new_id: Callable[[str], str],
        now: Callable[[], str],
        connect: Callable[[], sqlite3.Connection],
        write: Callable[[], Any],
        get_notebook: Callable[[str], Any],
        invalidate_unified_cache: Callable[[str], None],
        mark_unified_kg_dirty: Callable[[str], None],
        llm: Callable[[], Any],
        kg_llm: Callable[[], Any],
        relations_for_notebook: Callable[[str], List[dict]],
        edge_centrality_map: Callable[[str], Dict[str, float]],
        embed_knowledge: Callable[[str, str, dict], None],
        knowledge_objects: Callable[..., List[dict]],
        as_retrieved: Callable[[dict, str], Any],
        rule_card: Callable[[Any], RuleCard],
        set_conflict_status: Callable[[str, str, str], None],
    ) -> None:
        self.settings = settings
        self.event_log = event_log
        self.governance_store = governance_store
        self.knowledge = knowledge
        self._new_id = new_id
        self._now = now
        self._connect = connect
        self._write = write
        self.get_notebook = get_notebook
        self._invalidate_unified_cache = invalidate_unified_cache
        self._mark_unified_kg_dirty = mark_unified_kg_dirty
        self._llm = llm
        self._kg_llm = kg_llm
        self._relations_for_notebook = relations_for_notebook
        self._edge_centrality_map = edge_centrality_map
        self._embed_knowledge = embed_knowledge
        self._knowledge_objects = knowledge_objects
        self._as_retrieved = as_retrieved
        self._rule_card = rule_card
        self._set_conflict_status = set_conflict_status

    # Late-bound model client: resolved per call through the facade's frozen
    # property, so class-property monkeypatches and the mutable llm_client
    # setter keep being observed — and the per-user ContextVar resolution
    # stays on the calling thread.
    @property
    def llm_client(self):
        return self._llm()

    # ------------------------------------------------------------------
    # Track E: edge trust review queue + curation feedback loop
    # ------------------------------------------------------------------

    def review_queue(self, notebook_id: str, limit: int = 200) -> List[dict]:
        """Return edges ranked by review priority = edge_centrality * (1 - trust_score).

        Only edges with review_status != 'rejected' are included (rejected edges are
        excluded from reasoning and need no further review).
        Centrality is computed over the FULL graph (including non-rejected edges),
        version-cached via the facade's _edge_centrality_map — see that method's
        docstring for the degree-top-K bounding behavior above
        edge_centrality_max_nodes.
        trust_score combines evidence anchoring + cross-doc corroboration + type validity.
        """
        import json as _json
        from app.services.kg.edge_trust import (
            compute_trust_score, corroboration_counts,
            corroboration_score_from_count,
        )

        self.get_notebook(notebook_id)
        with self._connect() as db:
            rel_rows = db.execute(
                "SELECT kr.id, kr.source_object_id, kr.target_object_id, "
                "kr.edge_type, kr.evidence, kr.source_id, kr.review_status, "
                "ko_s.object_type AS src_type, ko_t.object_type AS tgt_type "
                "FROM knowledge_relations kr "
                "LEFT JOIN knowledge_objects ko_s ON ko_s.id = kr.source_object_id "
                "LEFT JOIN knowledge_objects ko_t ON ko_t.id = kr.target_object_id "
                "WHERE kr.notebook_id = ? AND kr.review_status != 'rejected'",
                (notebook_id,),
            ).fetchall()
            # Build node types + names for trust signals
            obj_rows = db.execute(
                "SELECT id, object_type, payload FROM knowledge_objects "
                "WHERE notebook_id = ?", (notebook_id,)
            ).fetchall()

        node_types: dict = {}
        node_names: dict = {}
        for r in obj_rows:
            node_types[r["id"]] = r["object_type"]
            p = _json.loads(r["payload"] or "{}")
            node_names[r["id"]] = p.get("name", "")

        rels = []
        for r in rel_rows:
            rels.append({
                "id": r["id"],
                "source_object_id": r["source_object_id"],
                "target_object_id": r["target_object_id"],
                "edge_type": r["edge_type"],
                "evidence": _json.loads(r["evidence"] or "[]"),
                "source_id": r["source_id"],
                "review_status": r["review_status"],
                "_src_type": r["src_type"] or "",
                "_tgt_type": r["tgt_type"] or "",
                "_src_name": node_names.get(r["source_object_id"], ""),
                "_tgt_name": node_names.get(r["target_object_id"], ""),
            })

        # Corroboration counts (batched over all edges)
        corr_counts = corroboration_counts(rels, node_names)

        # Edge centrality — version-cached, see _edge_centrality_map docstring.
        edge_centrality = self._edge_centrality_map(notebook_id)

        items = []
        for rel in rels:
            rid = rel["id"]
            corr_score = corroboration_score_from_count(corr_counts.get(rid, 1))
            trust = compute_trust_score(rel, node_types, corr_score)
            ec = edge_centrality.get(rid, 0.0)
            # review_priority = high centrality × low trust
            priority = ec * (1.0 - trust)
            items.append({
                "rel_id": rid,
                "notebook_id": notebook_id,
                "edge_type": rel["edge_type"],
                "source_object_id": rel["source_object_id"],
                "target_object_id": rel["target_object_id"],
                "source_name": rel["_src_name"],
                "target_name": rel["_tgt_name"],
                "source_type": rel["_src_type"],
                "target_type": rel["_tgt_type"],
                "trust_score": trust,
                "edge_centrality": ec,
                "review_priority": priority,
                "review_status": rel["review_status"],
            })

        items.sort(key=lambda x: x["review_priority"], reverse=True)
        return items[:limit]

    def set_edge_review(self, notebook_id: str, rel_id: str, status: str) -> None:
        """Persist review_status on a knowledge_relation.

        Allowed statuses: 'pending', 'verified', 'rejected'.
        Raises ValueError for unknown statuses.
        Raises KeyError if the relation does not exist in this notebook.
        Invalidates the federated reasoning graph cache so the next
        graph-reasoning call sees the updated set of active edges.
        """
        if status not in self._REVIEW_STATUSES:
            raise ValueError(
                f"review_status must be one of {sorted(self._REVIEW_STATUSES)}, got {status!r}")
        with self._write() as db:
            self.governance_store.update_edge_review(db, notebook_id, rel_id, status)
        # review_status flips in place (relation COUNT unchanged) — bump the
        # monotonic seq so seq-keyed fast paths (_scale_index_version /
        # _cluster_input_version) don't serve a stale version for this edit.
        self._mark_unified_kg_dirty(notebook_id)
        # Invalidate cached graph so _federated_rx_graph rebuilds on next access
        # (belt-and-braces: its per-status-count version key would also catch
        # the flip on its own).
        self._invalidate_unified_cache(notebook_id)

    # ------------------------------------------------------------------
    # Concept-cluster merge candidates + decisions
    # ------------------------------------------------------------------

    def write_merge_candidate(self, notebook_id: str, a: str, b: str, score: float) -> None:
        now = self._now()
        with self._write() as db:
            self.governance_store.write_merge_candidate(
                db, notebook_id, a, b, score, now
            )

    def pending_merges(self, notebook_id: str) -> List[dict]:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            return self.governance_store.pending_merges(db, notebook_id)

    def _pending_merges_batch(self, notebook_id: str, limit: int) -> List[dict]:
        """Bounded fetch of pending merge candidates, LIMITed in SQL instead of
        materializing the whole pending set and Python-slicing it (perf-audit
        P1-1)."""
        self.get_notebook(notebook_id)
        with self._connect() as db:
            return self.governance_store.pending_merges_batch(
                db, notebook_id, limit
            )

    def _has_pending_merges(self, notebook_id: str) -> bool:
        """Cheap continuation test for the merge-review drain loop — EXISTS
        instead of materializing all pending rows just to check non-emptiness
        (perf-audit P1-1)."""
        with self._connect() as db:
            return self.governance_store.has_pending_merges(db, notebook_id)

    def set_merge_decision(self, notebook_id: str, candidate_id: str, status: str) -> None:
        if status not in ("confirmed", "rejected"):
            raise ValueError(f"invalid merge status: {status!r}")
        with self._write() as db:
            self.governance_store.set_merge_decision(
                db, notebook_id, candidate_id, status, self._now()
            )

    def confirm_merge(self, notebook_id: str, candidate_id: str) -> None:
        self.get_notebook(notebook_id)
        self.set_merge_decision(notebook_id, candidate_id, "confirmed")
        self._invalidate_unified_cache(notebook_id)
        self._mark_unified_kg_dirty(notebook_id)

    def reject_merge(self, notebook_id: str, candidate_id: str) -> None:
        self.get_notebook(notebook_id)
        self.set_merge_decision(notebook_id, candidate_id, "rejected")
        self._invalidate_unified_cache(notebook_id)
        self._mark_unified_kg_dirty(notebook_id)

    # ------------------------------------------------------------------
    # kg_conflict_candidates — storage primitives (T1)
    # Mirrors the concept_merge_candidates pattern above.
    # Detection lives in conflict_detect.py (T2); adjudication in
    # conflict_review.py (T3); write-back in apply_conflict_resolution (T4);
    # orchestration in resolve_notebook_conflicts (T5).
    # ------------------------------------------------------------------

    def write_conflict_candidate(
        self,
        notebook_id: str,
        kind: str,
        left_ref: str,
        right_ref: str,
        conflict_type: Optional[str] = None,
        resolution: Optional[str] = None,
        winner_ref: Optional[str] = None,
        resolved_payload: Optional[str] = None,
        confidence: Optional[float] = None,
        rationale: Optional[str] = None,
    ) -> str:
        """Insert one conflict candidate into the queue and return its id.

        resolution, winner_ref, resolved_payload, confidence, and rationale
        are normally NULL at detection time and only populated after
        adjudication (set_conflict_status in T1, write-back in apply_conflict_resolution T4).
        """
        now = self._now()
        with self._write() as db:
            return self.governance_store.write_conflict_candidate(
                db, notebook_id, kind, left_ref, right_ref,
                conflict_type, resolution, winner_ref, resolved_payload,
                confidence, rationale, now,
            )

    def pending_conflicts(self, notebook_id: str) -> List[dict]:
        """Return all conflict candidates with status='pending' for a notebook."""
        self.get_notebook(notebook_id)
        with self._connect() as db:
            return self.governance_store.pending_conflicts(db, notebook_id)

    def set_conflict_status(self, notebook_id: str, candidate_id: str, status: str) -> None:
        """Update status to 'applied' or 'rejected' (+ updated_at).

        Both identifiers are required even though candidate ids are UUID-like:
        authorization is notebook-scoped, so object lookup must use the same
        scope rather than trusting a caller-controlled URL notebook id.
        """
        if status not in ("applied", "rejected"):
            raise ValueError(f"invalid conflict status: {status!r}")
        with self._write() as db:
            self.governance_store.set_conflict_status(
                db, notebook_id, candidate_id, status, self._now()
            )

    def get_conflict_candidate(self, notebook_id: str, candidate_id: str) -> Optional[dict]:
        """Fetch one conflict candidate inside its notebook authorization scope."""
        return self.governance_store.get_conflict_candidate(
            notebook_id, candidate_id
        )

    def apply_conflict_resolution(
        self,
        notebook_id: str,
        *,
        kind: str,
        left_ref: str,
        right_ref: str,
        resolution: str,
        winner_ref: Optional[str] = None,
        resolved_payload: Optional[dict] = None,
    ) -> dict:
        """Execute ONE adjudicated conflict resolution against the KG.

        Mechanics only — policy (which side wins) is decided by the caller and
        passed in via ``winner_ref``.  This method just executes the decided
        outcome and keeps caches consistent.

        Parameters
        ----------
        notebook_id:
            The notebook that owns the conflicting objects / relations.
        kind:
            ``"edge"`` (refs are relation ids) or ``"node"`` (refs are
            knowledge_object ids).
        left_ref / right_ref:
            The two competing entity ids.
        resolution:
            ``"keep"`` | ``"discard"`` | ``"modify"``.
        winner_ref:
            For ``"discard"``: the ref that survives; the other is the loser.
            For ``"modify"``/``"node"``: the target object to update (falls back
            to ``left_ref`` when None or not in {left_ref, right_ref}).
            Ignored for ``"keep"``.
        resolved_payload:
            For ``"modify"``/``"node"``: the new payload dict to write.
        """
        if kind not in ("edge", "node"):
            raise ValueError(f"kind must be 'edge' or 'node', got {kind!r}")
        if resolution not in ("keep", "discard", "modify"):
            raise ValueError(
                f"resolution must be 'keep', 'discard', or 'modify', got {resolution!r}")

        # ── keep ────────────────────────────────────────────────────────────
        if resolution == "keep":
            return {"action": "keep"}

        # ── discard ─────────────────────────────────────────────────────────
        if resolution == "discard":
            if winner_ref not in (left_ref, right_ref):
                self.event_log.logger.warning(
                    "apply_conflict_resolution: discard skipped — winner_ref %r is not one of "
                    "(%r, %r) in notebook %s",
                    winner_ref, left_ref, right_ref, notebook_id,
                )
                return {"action": "skipped", "reason": "no valid winner_ref for discard"}
            loser_ref = right_ref if winner_ref == left_ref else left_ref
            if kind == "edge":
                self.set_edge_review(notebook_id, loser_ref, "rejected")
                # set_edge_review already marks dirty + invalidates cache; this
                # extra mark is a harmless belt-and-suspenders (seq is monotonic).
                self._mark_unified_kg_dirty(notebook_id)
            else:  # kind == "node"
                self.update_knowledge(
                    notebook_id, loser_ref, KnowledgeUpdate(status="conflict")
                )
                # update_knowledge already calls _invalidate_unified_cache; mark dirty too.
                self._mark_unified_kg_dirty(notebook_id)
            return {"action": "discard", "loser": loser_ref}

        # ── modify ──────────────────────────────────────────────────────────
        # resolution == "modify"
        if kind == "edge":
            self.event_log.logger.warning(
                "apply_conflict_resolution: edge modify is unsupported in v1 "
                "(notebook %s, left=%r, right=%r) — no-op",
                notebook_id, left_ref, right_ref,
            )
            return {"action": "skipped", "reason": "edge modify unsupported in v1"}

        # kind == "node"
        if not isinstance(resolved_payload, dict):
            self.event_log.logger.warning(
                "apply_conflict_resolution: modify skipped — resolved_payload is not a dict "
                "(got %r) in notebook %s",
                type(resolved_payload).__name__, notebook_id,
            )
            return {"action": "skipped", "reason": "modify without payload"}

        target = winner_ref if winner_ref in (left_ref, right_ref) else left_ref
        # Fetch the current payload so we can merge rather than replace.
        # update_knowledge replaces the entire payload column, so we must
        # preserve fields (section_path, validity_scope, steps, …) not
        # included in the adjudicator's resolved_payload.
        _row = self.knowledge.get_object_row(notebook_id, target)
        existing_payload: dict = json.loads(_row["payload"] or "{}") if _row else {}
        merged_payload = {**existing_payload, **resolved_payload}
        self.update_knowledge(
            notebook_id, target, KnowledgeUpdate(payload=merged_payload)
        )
        # update_knowledge already calls _invalidate_unified_cache; mark dirty too.
        self._mark_unified_kg_dirty(notebook_id)
        return {"action": "modify", "target": target}

    def confirm_conflict(self, notebook_id: str, candidate_id: str) -> dict:
        """Apply a pending conflict candidate and mark it as 'applied'.

        Composes existing T1/T4 primitives — no new detection or adjudication
        logic.  Raises KeyError if the candidate does not exist; raises
        ValueError if it is already decided (not 'pending').
        """
        row = self.get_conflict_candidate(notebook_id, candidate_id)
        if row is None:
            raise KeyError(f"conflict candidate {candidate_id!r} not found")
        if row["status"] != "pending":
            raise ValueError(
                f"conflict candidate {candidate_id!r} is already decided "
                f"(status={row['status']!r})"
            )
        if not row.get("resolution"):
            # Detected but not yet adjudicated — nothing to apply. Clearer than
            # letting apply_conflict_resolution raise a generic ValueError.
            raise ValueError(
                f"conflict candidate {candidate_id!r} has no resolution "
                f"(not yet adjudicated)"
            )
        resolved_payload: Optional[dict] = None
        if row.get("resolved_payload") is not None:
            try:
                resolved_payload = json.loads(row["resolved_payload"])
            except (TypeError, ValueError):
                resolved_payload = None

        apply_result = self.apply_conflict_resolution(
            notebook_id,
            kind=row["kind"],
            left_ref=row["left_ref"],
            right_ref=row["right_ref"],
            resolution=row["resolution"],
            winner_ref=row["winner_ref"],
            resolved_payload=resolved_payload,
        )
        # The candidate-status transaction rides the facade wrapper (port) —
        # the frozen phase contract patches that facade method and must keep
        # observing the mutation-commits-before-status boundary.
        self._set_conflict_status(notebook_id, candidate_id, "applied")
        return {**apply_result, "status": "applied", "candidate_id": candidate_id}

    def reject_conflict(self, notebook_id: str, candidate_id: str) -> None:
        """Reject a pending conflict candidate (no KG mutation).

        Raises KeyError if the candidate does not exist; raises ValueError if
        it is already decided.
        """
        row = self.get_conflict_candidate(notebook_id, candidate_id)
        if row is None:
            raise KeyError(f"conflict candidate {candidate_id!r} not found")
        if row["status"] != "pending":
            raise ValueError(
                f"conflict candidate {candidate_id!r} is already decided "
                f"(status={row['status']!r})"
            )
        self._set_conflict_status(notebook_id, candidate_id, "rejected")

    # ------------------------------------------------------------------
    # resolve_notebook_conflicts — Task T5: orchestration
    # Ties detection (T2) → adjudication (T3) → write-back (T4) and
    # records everything in the queue (T1).
    # ------------------------------------------------------------------

    def resolve_notebook_conflicts(self, notebook_id: str) -> dict:
        """Detect, adjudicate, and (optionally) auto-apply KG conflicts for a notebook.

        Steps
        -----
        1. Guard: if LLM is not configured, return a summary noting skipped.
        2. Load objects + relations; build lookup dicts.
        3. Build an {object_id: vector} embeddings dict for the semantic strategy
           (reads knowledge_embeddings; passes None if unavailable).
        4. Run detect_conflict_candidates.
        5. Materialise T3 input items (text / source_text / object_type / tier).
        6. Call review_conflict_candidates (LLM adjudicator).
        7. For each verdict: record in queue; auto-apply when
           conflict_type != "none" AND resolution != "keep" AND
           confidence >= kg_conflict_auto_apply_threshold.
        8. Return summary dict.

        Cross-tier base-wins (base-notebook overrides personal-notebook claims)
        is FUTURE WORK — it belongs when cross-notebook / federated candidate
        recall is added.  In v1, all sides share one tier within the notebook
        so the LLM's winner_ref is trusted directly.
        """
        # 1. Guard — no LLM, skip gracefully
        if not getattr(self._llm(), "configured", False):
            return {
                "detected": 0,
                "auto_applied": 0,
                "queued": 0,
                "skipped_llm": True,
            }

        # 2. Load objects + relations
        with self._connect() as db:
            # Fetch all non-deprecated objects for this notebook
            obj_rows = db.execute(
                "SELECT id, object_type, payload, evidence, status "
                "FROM knowledge_objects "
                "WHERE notebook_id=? AND status != 'deprecated'",
                (notebook_id,),
            ).fetchall()

            # Fetch embeddings for the semantic strategy
            vec_rows = db.execute(
                "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?",
                (notebook_id,),
            ).fetchall()

            # Fetch the notebook tier (same for all objects in v1)
            nb_row = db.execute(
                "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
            ).fetchone()

        # Build objects list in detect_conflict_candidates format
        objects = []
        object_map: dict = {}  # object_id → row dict
        for row in obj_rows:
            payload = json.loads(row["payload"] or "{}")
            obj = {
                "id": row["id"],
                "object_type": row["object_type"],
                "payload": payload,
                "evidence": json.loads(row["evidence"] or "[]"),
                "status": row["status"],
            }
            objects.append(obj)
            object_map[row["id"]] = obj

        relations = self._relations_for_notebook(notebook_id)

        # Build name lookup for edge-text rendering: object_id → name
        obj_name_map: dict = {
            obj["id"]: (obj["payload"].get("name", "") if isinstance(obj["payload"], dict) else "")
            for obj in objects
        }

        # 3. Build embeddings dict; log + skip on any error
        # 运行时截断旁路(计划 §1.2,conflict 同步接线):此处原先连存储维过滤都
        # 没有 —— conflict_detect._cosine_sim 虽已改混维零容忍,这里仍须①先按
        # 存储原生维过滤(异维残留出局)②通过后截断到运行时空间,保证语义策略
        # 收到的向量同维可比(而非靠下游把混维对静默判 0 丢召回)。
        embeddings: dict | None = None
        if vec_rows:
            try:
                from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec
                _dim = self.settings.embed_dim
                _rd = resolve_runtime_dim(self.settings)
                embeddings = {}
                for r in vec_rows:
                    if not r["vector"]:
                        continue
                    arr = decode_vector(r["vector"])
                    if arr is None or arr.size != _dim:
                        continue
                    if _rd:
                        arr = truncate_vec(arr, _rd)
                    embeddings[r["object_id"]] = arr.tolist()
            except Exception:  # noqa: BLE001
                self.event_log.logger.debug(
                    "resolve_notebook_conflicts: failed to load embeddings for %s; "
                    "semantic strategy will be skipped",
                    notebook_id,
                )
                embeddings = None

        # 4. Detect candidates
        from app.services.kg.conflict_detect import detect_conflict_candidates
        notebook_tier = (nb_row["tier"] if nb_row else "personal")

        candidates = detect_conflict_candidates(
            objects,
            relations,
            embeddings=embeddings,
            sim_threshold=self.settings.kg_conflict_sim_threshold,
        )

        if not candidates:
            return {
                "detected": 0,
                "auto_applied": 0,
                "queued": 0,
                "skipped_llm": False,
            }

        # 5. Materialise T3 input items
        # Build relation lookup: rel_id → relation dict
        rel_map: dict = {r["id"]: r for r in relations}

        items = []
        for cand in candidates:
            kind = cand["kind"]
            left_ref = cand["left_ref"]
            right_ref = cand["right_ref"]

            if kind == "edge":
                # text: "src_name —edge_type→ tgt_name"
                left_rel = rel_map.get(left_ref, {})
                right_rel = rel_map.get(right_ref, {})

                def _edge_text(rel: dict) -> str:
                    src_name = obj_name_map.get(rel.get("source_object_id", ""), "")
                    tgt_name = obj_name_map.get(rel.get("target_object_id", ""), "")
                    etype = rel.get("edge_type", "")
                    return f"{src_name} —{etype}→ {tgt_name}"

                def _edge_source(rel: dict) -> str:
                    ev = rel.get("evidence") or []
                    if ev and isinstance(ev, list):
                        first = ev[0]
                        if not isinstance(first, dict):
                            return ""
                        # Relations store evidence as {"quote": ...} (kg_ingest.py);
                        # nodes store evidence as {"quoted_span": ...}.  Accept both.
                        text = (first.get("quoted_span") or first.get("quote") or "")
                        return text[:400]
                    return ""

                left_item = {
                    "text": _edge_text(left_rel),
                    "source_text": _edge_source(left_rel),
                    "object_type": None,
                    "tier": notebook_tier,
                }
                right_item = {
                    "text": _edge_text(right_rel),
                    "source_text": _edge_source(right_rel),
                    "object_type": None,
                    "tier": notebook_tier,
                }
            else:
                # kind == "node"
                left_obj = object_map.get(left_ref, {})
                right_obj = object_map.get(right_ref, {})

                def _node_text(obj: dict) -> str:
                    payload = obj.get("payload") or {}
                    name = payload.get("name", "") if isinstance(payload, dict) else ""
                    return name

                def _node_source(obj: dict) -> str:
                    ev_list = obj.get("evidence") or []
                    if ev_list and isinstance(ev_list, list):
                        first = ev_list[0]
                        if isinstance(first, dict):
                            return (first.get("quoted_span") or "")[:400]
                        # Evidence may be Evidence namedtuple / dataclass
                        return (getattr(first, "quoted_span", None) or "")[:400]
                    return ""

                left_item = {
                    "text": _node_text(left_obj),
                    "source_text": _node_source(left_obj),
                    "object_type": left_obj.get("object_type"),
                    "tier": notebook_tier,
                }
                right_item = {
                    "text": _node_text(right_obj),
                    "source_text": _node_source(right_obj),
                    "object_type": right_obj.get("object_type"),
                    "tier": notebook_tier,
                }

            items.append({
                "candidate": cand,
                "left": left_item,
                "right": right_item,
            })

        # 6. Adjudicate
        from app.services.kg.conflict_review import review_conflict_candidates
        verdicts = review_conflict_candidates(self._kg_llm(), items)

        # 7. Record + (optionally) auto-apply
        auto_applied = 0
        queued = 0
        threshold = self.settings.kg_conflict_auto_apply_threshold

        for cand, verdict in zip(candidates, verdicts):
            kind = cand["kind"]
            conflict_type = verdict["conflict_type"]
            resolution = verdict["resolution"]
            winner_ref = verdict["winner_ref"]
            resolved_payload = verdict["resolved_payload"]
            confidence = verdict["confidence"]
            rationale = verdict["rationale"]

            # Record in queue
            candidate_id = self.write_conflict_candidate(
                notebook_id,
                kind=kind,
                left_ref=cand["left_ref"],
                right_ref=cand["right_ref"],
                conflict_type=conflict_type,
                resolution=resolution,
                winner_ref=winner_ref,
                resolved_payload=(
                    json.dumps(resolved_payload) if resolved_payload is not None else None
                ),
                confidence=confidence,
                rationale=rationale,
            )

            # Auto-apply?  Only for genuine conflicts with a non-trivial resolution.
            should_apply = (
                conflict_type != "none"
                and resolution != "keep"
                and confidence >= threshold
            )
            if should_apply:
                try:
                    self.apply_conflict_resolution(
                        notebook_id,
                        kind=kind,
                        left_ref=cand["left_ref"],
                        right_ref=cand["right_ref"],
                        resolution=resolution,
                        winner_ref=winner_ref,
                        resolved_payload=resolved_payload,
                    )
                    self._set_conflict_status(notebook_id, candidate_id, "applied")
                    auto_applied += 1
                except Exception:  # noqa: BLE001
                    self.event_log.logger.exception(
                        "resolve_notebook_conflicts: auto-apply failed for candidate %s "
                        "(notebook %s, kind=%s, left=%r, right=%r)",
                        candidate_id, notebook_id, kind,
                        cand["left_ref"], cand["right_ref"],
                    )
                    queued += 1
            else:
                queued += 1

        return {
            "detected": len(candidates),
            "auto_applied": auto_applied,
            "queued": queued,
            "skipped_llm": False,
        }

    # ------------------------------------------------------------------
    # Merge review (LLM adjudication) + background drain job
    # ------------------------------------------------------------------

    def review_pending_merges(
        self,
        notebook_id: str,
        limit: int = 50,
        confirm_threshold: Optional[float] = None,
        separate_threshold: Optional[float] = None,
    ) -> dict:
        self.get_notebook(notebook_id)
        # 非对称阈值:auto-merge 需更高置信(误并不可逆、污染图);auto-keep-separate
        # 可低些(误判仅多留一对待审)。未显式传入则取 settings 默认(0.90 / 0.80)。
        confirm = confirm_threshold if confirm_threshold is not None else self.settings.kg_merge_confirm_threshold
        separate = separate_threshold if separate_threshold is not None else self.settings.kg_merge_separate_threshold
        pending = self._pending_merges_batch(notebook_id, max(1, min(limit, 200)))
        from app.services.concept_merge_review import review_merge_candidates
        # review_merge_candidates is total (fail-open, chunked); the outer try is
        # defense-in-depth so this endpoint can never 500 on an LLM deviation (the
        # route only catches KeyError). Same batching/concurrency as the rebuild site.
        try:
            decisions = review_merge_candidates(
                self.llm_client, pending,
                batch_size=self.settings.kg_merge_review_batch_size,
                max_workers=self.settings.kg_job_concurrency,
            )
        except Exception:
            self.event_log.logger.exception(
                "merge-review adjudication failed for %s; proceeding with no decisions",
                notebook_id,
            )
            decisions = []
        confirmed = rejected = unsure = 0
        now = self._now()
        with self._write() as db:
            for decision in decisions:
                candidate_id = decision["candidate_id"]
                confidence = decision["confidence"]
                status = "pending"
                if decision["decision"] == "merge" and confidence >= confirm:
                    status = "confirmed"
                    confirmed += 1
                elif decision["decision"] == "keep_separate" and confidence >= separate:
                    status = "rejected"
                    rejected += 1
                else:
                    status = "deferred"
                    unsure += 1
                self.governance_store.record_merge_review(
                    db, notebook_id, candidate_id, status, confidence,
                    decision["rationale"], now,
                )
        if confirmed or rejected:
            self._mark_unified_kg_dirty(notebook_id)
            self._invalidate_unified_cache(notebook_id)
        return {"reviewed": len(decisions), "confirmed": confirmed, "rejected": rejected, "unsure": unsure}

    def merge_review_job_status(self, notebook_id: str) -> dict:
        with self._connect() as db:
            row = self.governance_store.merge_review_job_row(db, notebook_id)
        if row is None:
            return {"status": "idle", "total": 0, "done": 0, "error": ""}
        return {"status": row["status"], "total": int(row["total"]),
                "done": int(row["done"]), "error": row["error"]}

    def run_merge_review_job(self, notebook_id: str, *, batch: int = 100) -> dict:
        """Drain the whole pending merge queue in batches (each batch = one
        review_pending_merges call). Single-flight per notebook. Fail-open per
        batch; a batch that reviews 0 (LLM down) counts as a stall — abort after
        2 consecutive stalls so a persistent failure can't loop forever. Since
        Task 4 makes unsure→deferred, every reviewed candidate leaves pending, so
        a healthy run strictly shrinks the queue and terminates."""
        self.get_notebook(notebook_id)
        with self._write() as db:
            total = self.governance_store.begin_merge_review_job(
                db, notebook_id, self._now()
            )
            if total is None:
                return {"status": "running", "already": True}
        done, stalls, error, final = 0, 0, "", "done"
        max_batches = (total // max(1, batch)) + 3
        try:
            for _ in range(max_batches):
                if not self._has_pending_merges(notebook_id):
                    break
                summary = self.review_pending_merges(notebook_id, limit=batch)
                reviewed = int(summary.get("reviewed", 0))
                done += reviewed
                with self._write() as db:
                    self.governance_store.set_merge_review_progress(
                        db, notebook_id, done, self._now())
                if reviewed == 0:
                    stalls += 1
                    if stalls >= 2:
                        error, final = "LLM 预审连续无进展,已中止", "failed"
                        break
                else:
                    stalls = 0
        except Exception as exc:  # noqa: BLE001
            error, final = f"{type(exc).__name__}: {exc}", "failed"
            self.event_log.logger.exception("merge review job failed for %s", notebook_id)
        with self._write() as db:
            self.governance_store.finish_merge_review_job(
                db, notebook_id, final, error, self._now())
        return {"status": final, "total": total, "done": done, "error": error}

    # ------------------------------------------------------------------
    # Decided pairs + concept whitelist
    # ------------------------------------------------------------------

    def decided_pairs(self, notebook_id: str) -> Dict[tuple, str]:
        return self.governance_store.decided_pairs(notebook_id)

    def decided_seed_pairs(self, notebook_id: str) -> Dict[frozenset, str]:
        """{frozenset({seed_a, seed_b}): status} for confirmed/rejected/deferred.

        Seed-name keys are STABLE across rebuilds (canonical ids shift when a
        cluster's min-member changes; seed names don't). Legacy rows written
        before the seed_a/seed_b columns existed carry '' → fall back to
        strip-"K-"(canonical), matching the old decided_pairs key derivation."""
        return self.governance_store.decided_seed_pairs(notebook_id)

    def concept_whitelist_terms(self) -> set:
        with self._connect() as db:
            return self.governance_store.concept_whitelist_terms(db)

    def concept_whitelist_list(self) -> List[dict]:
        with self._connect() as db:
            rows = self.governance_store.concept_whitelist_rows(db)
        return [{"term": r["term"], "note": r["note"], "created_at": r["created_at"]} for r in rows]

    def concept_whitelist_add(self, term: str, note: str = "") -> dict:
        from app.services.kg.filters import _norm
        t = _norm(term)
        if not t:
            raise ValueError("empty term")
        now = self._now()
        with self._write() as db:
            self.governance_store.add_whitelist_term(db, t, note, now)
        return {"term": t, "note": note, "created_at": now}

    def concept_whitelist_remove(self, term: str) -> None:
        from app.services.kg.filters import _norm
        with self._write() as db:
            self.governance_store.remove_whitelist_term(db, _norm(term))

    # ------------------------------------------------------------------
    # Governance: promotion state machine (Track F)
    # ------------------------------------------------------------------

    def propose_promotion(self, notebook_id: str, object_id: str) -> dict:
        """Propose a personal-KG object for promotion into the base corpus.

        Idempotent for an already-active proposal of the same object. Raises
        KeyError if the notebook or object is missing; ValueError if the
        notebook is itself a base notebook (use the review gate there instead).
        """
        self.get_notebook(notebook_id)  # KeyError if notebook missing
        now = self._now()
        with self._write() as db:
            obj = db.execute(
                "SELECT object_type FROM knowledge_objects WHERE id=? AND notebook_id=?",
                (object_id, notebook_id),
            ).fetchone()
            if obj is None:
                raise KeyError(object_id)
            nb_row = db.execute(
                "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
            ).fetchone()
            if nb_row and nb_row["tier"] == "base":
                raise ValueError("cannot propose from a base notebook — use the review gate")
            # Idempotency: return any active (non-approved, non-rejected) proposal.
            existing = self.governance_store.active_promotion_for_object(db, object_id)
            if existing is not None:
                return promotion_row_to_dict(existing)
            cand_id = self._new_id("promo")
            self.governance_store.insert_promotion_candidate(
                db, cand_id, notebook_id, object_id, obj["object_type"], now
            )
            row = self.governance_store.promotion_candidate_row(db, cand_id)
        return promotion_row_to_dict(row)

    def list_promotion_queue(self, status_filter: Optional[str] = None) -> List[dict]:
        """List promotion candidates across all notebooks (the curator sees
        everything). Defaults to the active queue (proposed + under_review);
        pass status_filter to view a single status. Denormalises payload +
        evidence from knowledge_objects for display.

        Batched (house pattern, see _hydrate_search_hits): one `id IN (...)`
        knowledge_objects lookup for the whole queue instead of a per-row
        SELECT — was N+1 (one round-trip per candidate)."""
        with self._connect() as db:
            rows = self.governance_store.promotion_queue_rows(db, status_filter)
            object_ids = list(dict.fromkeys(r["object_id"] for r in rows))
            obj_by_id: Dict[str, sqlite3.Row] = {}
            for i in range(0, len(object_ids), _IN_CHUNK):
                batch = object_ids[i:i + _IN_CHUNK]
                ph = ",".join("?" for _ in batch)
                for r in db.execute(
                    f"SELECT id, payload, evidence FROM knowledge_objects WHERE id IN ({ph})",
                    batch,
                ).fetchall():
                    obj_by_id[r["id"]] = r
            out: List[dict] = []
            for row in rows:
                obj = obj_by_id.get(row["object_id"])
                payload = json.loads(obj["payload"] or "{}") if obj else {}
                evidence = (
                    [Evidence(**e) for e in json.loads(obj["evidence"] or "[]")]
                    if obj
                    else []
                )
                out.append(
                    promotion_row_to_dict(row, payload=payload, evidence=evidence)
                )
        return out

    def approve_promotion(self, candidate_id: str) -> dict:
        """Approve a promotion: copy the personal object into the base corpus,
        deduplicating against existing base objects of the same type via the
        kg_merge seed clustering. Idempotent. Raises KeyError if the candidate
        is missing; ValueError if it is rejected or there is no base notebook.
        """
        now = self._now()
        with self._write() as db:
            cand = self.governance_store.promotion_candidate_row(db, candidate_id)
            if cand is None:
                raise KeyError(candidate_id)
            if cand["status"] == "rejected":
                raise ValueError("cannot approve a rejected promotion candidate")
            was_approved = cand["status"] == "approved"
            src_payload = (
                json.loads(
                    (db.execute(
                        "SELECT payload FROM knowledge_objects WHERE id=?",
                        (cand["object_id"],)).fetchone() or {"payload": None})["payload"]
                    or "{}"
                )
                if not was_approved
                else {}
            )
            approval = self.governance_store.approve_promotion_in_transaction(
                db, candidate_id, now
            )
            # Idempotency: an already-approved candidate returns the existing
            # base object with NO post-commit hooks — exactly the old
            # early-return-inside-the-transaction behavior.
            if was_approved:
                return {
                    "candidate_id": candidate_id,
                    "base_object_id": approval.base_object_id,
                    "merged_into": cand["base_match_id"] or "",
                }

        # Embed the new base object's payload (best-effort; outside the txn so a
        # failing embedder never blocks approval). Only for freshly-inserted ones.
        if approval.created_new_object:
            self._embed_knowledge(
                approval.base_object_id, approval.base_notebook_id, src_payload
            )
        self._invalidate_unified_cache(approval.base_notebook_id)
        self._mark_unified_kg_dirty(approval.base_notebook_id)
        return {
            "candidate_id": candidate_id,
            "base_object_id": approval.base_object_id,
            "merged_into": "" if approval.created_new_object else approval.base_object_id,
        }

    def reject_promotion(self, candidate_id: str, reason: str = "") -> dict:
        """Reject a promotion candidate. The personal object is left untouched.
        Raises KeyError if missing; ValueError if already approved."""
        now = self._now()
        with self._write() as db:
            cand = self.governance_store.promotion_candidate_row(db, candidate_id)
            if cand is None:
                raise KeyError(candidate_id)
            if cand["status"] == "approved":
                raise ValueError("cannot reject an approved promotion candidate")
            self.governance_store.set_promotion_rejected(
                db, candidate_id, reason, now
            )
            row = self.governance_store.promotion_candidate_row(db, candidate_id)
        return promotion_row_to_dict(row)

    # ------------------------------------------------------------------
    # Knowledge mutations: update / dedup / merge
    # ------------------------------------------------------------------

    def update_knowledge(
        self, notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate
    ) -> RuleCard:
        now = self._now()
        with self._write() as db:
            row = self.governance_store.update_object_in_transaction(
                db, notebook_id, knowledge_id, payload, now
            )
        # WS4: re-embed payload-level vector when the payload was edited.
        if payload.payload is not None:
            try:
                self._embed_knowledge(
                    knowledge_id, row["notebook_id"], json.loads(row["payload"] or "{}")
                )
            except Exception:
                pass
        self._invalidate_unified_cache(row["notebook_id"])
        # A node edit is a clustering input: a payload/name change moves its
        # normalized-name seed (→ cross-doc cluster membership), a re-embed changes
        # its ANN vector, and a status flip changes which objects are clustered.
        # Mark dirty so kg_mutation_seq advances and rebuild_unified_kg's skip gate
        # can't serve a stale clustering after an in-place rename/re-embed.
        self._mark_unified_kg_dirty(row["notebook_id"])
        obj = {
            "id": row["id"],
            "payload": json.loads(row["payload"] or "{}"),
            "evidence": [Evidence(**item) for item in json.loads(row["evidence"] or "[]")],
            "status": row["status"],
            "owner": row["owner"],
            "last_reviewed": row["last_reviewed"] if "last_reviewed" in row.keys() else "",
        }
        item = self._as_retrieved(obj, row["object_type"])
        return self._rule_card(item)

    def _knowledge_ref(self, obj: dict, object_type: str) -> KnowledgeRef:
        return KnowledgeRef(
            id=obj["id"],
            object_type=object_type,
            headline=knowledge_headline(object_type, obj["payload"]),
            status=obj.get("status", "approved"),
        )

    def _knowledge_similarity(self, a: dict, b: dict, element_vectors: dict) -> float:
        text_a = payload_join(a["payload"])
        text_b = payload_join(b["payload"])
        keyword = max(keyword_score(text_a, text_b), keyword_score(text_b, text_a))
        semantic = 0.0
        vecs_a = [element_vectors[e.element_id] for e in a["evidence"] if e.element_id in element_vectors]
        vecs_b = [element_vectors[e.element_id] for e in b["evidence"] if e.element_id in element_vectors]
        for va in vecs_a:
            for vb in vecs_b:
                semantic = max(semantic, cosine(va, vb))
        return max(keyword, semantic * 0.95)

    def find_duplicates(self, notebook_id: str, object_type: str) -> List[DuplicateGroup]:
        """Near-duplicate detection by normalized-seed BLOCKING — the same seed the
        KG clustering uses (name/statement/formula normalization + acronym alias).
        Only objects that share a seed are compared, so this is O(N + Σ block²)
        instead of the old O(N²) all-pairs — which also loaded EVERY element vector
        of the notebook into memory and froze 查重 at 10^5+ objects. The ≥0.6 grouping
        is preserved, just scoped to each (tiny) same-seed block; keyword overlap only
        (no vectors are loaded — nothing scales with the notebook's embedding count).
        Cross-seed *semantic* near-dups (different names, similar meaning) are out of
        scope here; the clustering / emb_synonym pass merges those on KG rebuild."""
        from app.services.kg_merge import (
            build_acronym_alias_map, _seed_with_alias,
            seed_concept, seed_claim, seed_formula, seed_procedure,
        )
        seed_fn = {
            "concept": seed_concept, "claim": seed_claim,
            "formula": seed_formula, "procedure": seed_procedure,
        }.get(object_type, seed_concept)

        self.get_notebook(notebook_id)
        with self._connect() as db:
            objs = self._knowledge_objects(db, notebook_id, object_type, statuses=None)
        objs = [o for o in objs if o.get("status") != "deprecated"]

        # Block by seed: only same-normalized-name objects become candidates.
        alias_map = build_acronym_alias_map(o["payload"].get("name", "") for o in objs)
        by_seed: Dict[str, List[dict]] = {}
        for o in objs:
            seed = _seed_with_alias(
                {"name": o["payload"].get("name", ""), "payload": o["payload"]},
                seed_fn, alias_map)
            if seed:
                by_seed.setdefault(seed, []).append(o)

        groups: List[DuplicateGroup] = []
        for members in by_seed.values():
            if len(members) < 2:
                continue
            # Same seed = same normalized name/statement = the duplicate signal
            # (consistent with how the KG clustering merges variants, incl. case /
            # whitespace / acronym). similarity is a display hint only: max pairwise
            # keyword overlap within the block — capped so a pathologically large
            # same-name block stays bounded, and with {} vectors so nothing loads
            # the embedding table.
            best = 1.0
            if len(members) <= 25:
                best = 0.0
                for i in range(len(members)):
                    for j in range(i + 1, len(members)):
                        best = max(best, self._knowledge_similarity(members[i], members[j], {}))
            groups.append(DuplicateGroup(
                object_type=object_type,
                similarity=round(best, 3),
                members=[self._knowledge_ref(m, object_type) for m in members],
            ))
        groups.sort(key=lambda g: (-len(g.members), -g.similarity))
        return groups

    def merge_knowledge(self, notebook_id: str, source_id: str, payload: MergeRequest) -> RuleCard:
        into_id = payload.into_id
        if into_id == source_id:
            raise ValueError("cannot merge a knowledge object into itself")
        now = self._now()
        with self._write() as db:
            row = self.governance_store.merge_objects_in_transaction(
                db, notebook_id, source_id, into_id, now
            )
        # merge deprecates one object in place (COUNT unchanged) — bump the
        # monotonic seq so _scale_index_version / _cluster_input_version fast
        # paths (keyed on kg_mutation_seq) don't miss this same-second edit.
        self._mark_unified_kg_dirty(row["notebook_id"])
        self._invalidate_unified_cache(row["notebook_id"])
        obj = {
            "id": row["id"],
            "payload": json.loads(row["payload"] or "{}"),
            "evidence": [Evidence(**item) for item in json.loads(row["evidence"] or "[]")],
            "status": row["status"],
            "owner": row["owner"],
            "last_reviewed": row["last_reviewed"] if "last_reviewed" in row.keys() else "",
        }
        item = self._as_retrieved(obj, row["object_type"])
        return self._rule_card(item)
