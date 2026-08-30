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
from typing import Any, Callable, Dict, List, Mapping, Optional

from app.core.config import Settings
from app.core.event_logging import EventLogger
from app.models.common import Evidence
from app.models.ask import RuleCard
from app.models.knowledge import (
    DuplicateGroup,
    KnowledgeRef,
    KnowledgeUpdate,
    MergeRequest,
)
from app.repositories.ports import (
    GovernanceStorePort,
    KnowledgeStorePort,
    MemoryStorePort,
    RepositoryRow,
)
from app.services.retrieval import cosine, keyword_score


# SQLite default SQLITE_MAX_VARIABLE_NUMBER-safe chunk size for `IN (...)`
# placeholder lists (mirrors the facade's frozen `_IN_CHUNK` convention).
_IN_CHUNK = 900


class PromotionTargetError(ValueError):
    """晋升目标(target_base_id)解析/校验失败 —— 挂 0 个公共库、挂 >1 个却未
    显式指定、或指定的目标不在挂载集合内(见 _resolve_promotion_target)。

    刻意作为 ValueError 的子类而非独立异常:知识对象晋升路由的
    `except ValueError` 无需改动即可继续把它映射成 400。子类化只是为了让
    Memory 晋升路由(经 memory_routes._memory_call 统一映射,那里的裸
    ValueError 语义是"状态冲突"→409)能在同一个 except 链里把这一类"目标
    无效"的输入错误单独识别出来映射成 400,而不误伤"该 Memory 已在晋升中"
    这类真正的状态冲突。"""


def promotion_row_to_dict(
    row: Mapping[str, Any], *, payload=None, evidence=None, source_revision: int = 0,
    target_base_name: str = "",
) -> dict:
    """Map a promotion_candidates row to the PromotionCandidate-shaped dict.
    payload/evidence are denormalised from knowledge_objects when listing.
    target_base_name (Task 13 审查 #4) is likewise denormalised by the caller
    from notebooks — this function stays a pure row mapper with no DB access,
    so callers that can batch (list_promotion_queue) resolve names in one
    `id IN (...)` round-trip up front; callers that don't need it (propose/
    approve/reject single-row responses) simply leave it at the default ''."""
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
        "source_kind": "memory" if row["object_type"] == "memory" else "knowledge",
        "memory_id": row["object_id"] if row["object_type"] == "memory" else "",
        "source_revision": int(source_revision),
        "target_base_id": row["target_base_id"],
        "target_base_name": target_base_name,
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
        governance_store: GovernanceStorePort,
        knowledge: KnowledgeStorePort,
        new_id: Callable[[str], str],
        now: Callable[[], str],
        connect: Callable[[], object],
        write: Callable[[], Any],
        get_notebook: Callable[[str], Any],
        invalidate_unified_cache: Callable[[str], None],
        mark_unified_kg_dirty: Callable[[str], None],
        model_clients: Any,
        edge_centrality_map: Callable[[str], Dict[str, float]],
        embed_knowledge: Callable[[str, str, dict], None],
        knowledge_objects: Callable[..., List[dict]],
        as_retrieved: Callable[[dict, str], Any],
        rule_card: Callable[[Any], RuleCard],
        set_conflict_status: Callable[[str, str, str], None],
        memory_store: MemoryStorePort,
        review_queue_total: Callable[[str], int],
        invalidate_knowledge_counts: Callable[[str], None],
        carry_review_queue_total: Callable[[str, int, int], None],
        kg_mutation_seq: Callable[[str], int],
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
        self.model_clients = model_clients
        # No ``relations_for_notebook`` seat: the one consumer (conflict
        # detection) reads the thin ``conflict_relation_rows`` projection from
        # the governance store instead of the full-row compatibility reader.
        self._edge_centrality_map = edge_centrality_map
        self._embed_knowledge = embed_knowledge
        self._knowledge_objects = knowledge_objects
        self._as_retrieved = as_retrieved
        self._rule_card = rule_card
        self._set_conflict_status = set_conflict_status
        self.memory_store = memory_store
        self._review_queue_total_fn = review_queue_total
        self._invalidate_knowledge_counts_fn = invalidate_knowledge_counts
        self._carry_review_queue_total_fn = carry_review_queue_total
        self._kg_mutation_seq_fn = kg_mutation_seq

    @staticmethod
    def promotion_dict(row, *, payload=None, evidence=None) -> dict:
        return promotion_row_to_dict(row, payload=payload, evidence=evidence)

    @staticmethod
    def headline(object_type: str, payload: dict) -> str:
        return knowledge_headline(object_type, payload)

    @staticmethod
    def joined_payload(payload: dict) -> str:
        return payload_join(payload)

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

        The store hands back a pushed-down ``has_anchor`` flag instead of every
        edge's evidence JSON, and only the objects that actually appear on a
        relation endpoint — see ``GovernanceStore.review_queue_rows``.  The
        corroboration aggregation stays in Python because its grouping key runs
        through ``edge_trust._norm``.

        The object narrowing is lossless.  The anchor pushdown is lossless for
        every evidence shape that used to produce a response, but NOT for the
        shapes that used to produce a 500: malformed evidence TEXT and a
        non-string ``quote`` made the old Python decode raise, and now score
        0.0 and return normally.  That difference is deliberate and registered
        in ``docs/superpowers/specs/2026-08-10-production-hotspot-hardening-design.md``
        (批 A+B).  Note it does not make this endpoint total: the centrality map
        below still parses evidence in Python, so malformed TEXT can still fail
        further downstream.
        """
        import heapq
        import json as _json
        from app.services.kg.edge_trust import (
            compute_trust_score, corroboration_counts,
            corroboration_score_from_count,
        )

        self.get_notebook(notebook_id)
        with self._connect() as db:
            rel_rows, obj_rows = self.governance_store.review_queue_rows(
                db, notebook_id
            )

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
                "_evidence_anchor": 1.0 if r["has_anchor"] else 0.0,
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
            trust = compute_trust_score(
                rel, node_types, corr_score,
                evidence_anchor=rel["_evidence_anchor"],
            )
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

        # heapq.nlargest is exactly ``sorted(key=…, reverse=True)[:limit]``: its
        # decoration carries a strictly decreasing counter, so ties resolve by
        # input order just as a stable sort does, and it never compares the
        # (unorderable) dicts themselves.  It is O(E log limit) instead of
        # O(E log E) — the queue ranks every non-rejected edge but serves ~100.
        # A NEGATIVE limit is the one shape it cannot express (``items[:-1]``
        # means "drop the tail", not "take none"), so that path keeps the sort.
        if limit < 0:
            items.sort(key=lambda x: x["review_priority"], reverse=True)
            return items[:limit]
        return heapq.nlargest(limit, items, key=lambda x: x["review_priority"])

    def review_queue_total(self, notebook_id: str) -> int:
        """Total edge-review-queue size (``review_status != 'rejected'``),
        independent of any ``limit`` passed to ``review_queue`` — a seq-gated
        ``COUNT(*)`` served by the injected query port (R3 T-A3), not a
        Python len() over the (already limited) ranked items ``review_queue``
        returns.

        Calls ``self.get_notebook`` first (S1: symmetry with ``review_queue``
        above) so a nonexistent notebook raises ``KeyError`` from THIS method
        directly rather than depending on a caller-side guard (the API route
        already 404s via its own dependency, but a direct/service-level or
        facade-level caller must see the same failure ``review_queue`` gives)."""
        self.get_notebook(notebook_id)
        return self._review_queue_total_fn(notebook_id)

    # Transitions where NEITHER side is 'rejected' change no ranking input
    # (trust/corr/centrality never read review_status) NOR the review-queue
    # COUNT (`review_status != 'rejected'` — pending/verified are both on the
    # "in" side of that predicate). Only a transition touching 'rejected' on
    # either end (including the rejected->rejected no-op write) can move
    # queue membership. R3 T-A3 P1-2 / T-A2 carry contract.
    _NON_REJECTED_REVIEW_STATUSES = frozenset({"pending", "verified"})

    def set_edge_review(self, notebook_id: str, rel_id: str, status: str) -> None:
        """Persist review_status on a knowledge_relation.

        Allowed statuses: 'pending', 'verified', 'rejected'.
        Raises ValueError for unknown statuses.
        Raises KeyError if the relation does not exist in this notebook.
        Invalidates the federated reasoning graph cache so the next
        graph-reasoning call sees the updated set of active edges.

        R3 T-A3 P1-2: ``update_edge_review`` returns the PREVIOUS status, so
        after the ``kg_mutation_seq`` bump this can tell whether the
        transition can carry-forward the ``review_queue_total`` count memo
        (pure verified<->pending flip, membership unchanged — cheap retag) or
        must let it go cold (either side 'rejected' — membership may have
        actually changed, a fresh COUNT is the safe move). The carry is keyed
        on ``new_seq - 1``: the seq this memo would have been tagged at
        immediately before THIS write's own bump, so a stale/mismatched entry
        (another writer raced in, or the memo was never warm) is dropped
        rather than guessed at — see ``carry_review_queue_total``'s docstring.
        """
        if status not in self._REVIEW_STATUSES:
            raise ValueError(
                f"review_status must be one of {sorted(self._REVIEW_STATUSES)}, got {status!r}")
        with self._write() as db:
            prev_status = self.governance_store.update_edge_review(
                db, notebook_id, rel_id, status
            )
        # review_status flips in place (relation COUNT unchanged) — bump the
        # monotonic seq so seq-keyed fast paths (_scale_index_version /
        # _cluster_input_version) don't serve a stale version for this edit.
        self._mark_unified_kg_dirty(notebook_id)
        non_rejected = self._NON_REJECTED_REVIEW_STATUSES
        if prev_status in non_rejected and status in non_rejected:
            new_seq = self._kg_mutation_seq_fn(notebook_id)
            self._carry_review_queue_total_fn(notebook_id, new_seq - 1, new_seq)
        else:
            self._invalidate_knowledge_counts_fn(notebook_id)
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

    def _set_merge_decision(self, notebook_id: str, candidate_id: str, status: str) -> str:
        if status not in ("confirmed", "rejected"):
            raise ValueError(f"invalid merge status: {status!r}")
        with self._write() as db:
            previous_status = self.governance_store.set_merge_decision(
                db, notebook_id, candidate_id, status, self._now()
            )
        if previous_status is None:
            raise KeyError(candidate_id)
        return previous_status

    def set_merge_decision(self, notebook_id: str, candidate_id: str, status: str) -> None:
        self._set_merge_decision(notebook_id, candidate_id, status)

    def confirm_merge(self, notebook_id: str, candidate_id: str) -> None:
        self.get_notebook(notebook_id)
        self._set_merge_decision(notebook_id, candidate_id, "confirmed")
        self._invalidate_unified_cache(notebook_id)
        self._mark_unified_kg_dirty(notebook_id)

    def reject_merge(self, notebook_id: str, candidate_id: str) -> None:
        self.get_notebook(notebook_id)
        previous_status = self._set_merge_decision(notebook_id, candidate_id, "rejected")
        # A rejection changes neither concept_clusters nor any retrieval
        # product.  The durable cannot-link is consumed by the next rebuild,
        # while the current graph is already correct, so do not launch/advertise
        # an unnecessary whole-notebook rebuild for this path. Reversing an
        # already-confirmed decision is different: that union may already be
        # materialized, so the normal dirty/invalidation path remains required.
        if previous_status == "confirmed":
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
        if not self.model_clients.configured("kg_conflict_review"):
            return {
                "detected": 0,
                "auto_applied": 0,
                "queued": 0,
                "truncated": 0,
                "truncated_edge": 0,
                "truncated_node": 0,
                "skipped_llm": True,
            }

        # 2. Load objects + relations
        # Both reads are detection-shaped: no evidence bodies (they are fetched
        # by id for the surviving candidates only, in step 5) and no relation
        # ``source_id``.  Detection never looks at either, and hauling every
        # object's and every edge's quoted spans through this scan is what made
        # the pass unrunnable on a large notebook.
        with self._connect() as db:
            # Submission-time admission can sit in the fixed maintenance queue
            # before this worker starts. Recheck the object rail on the same
            # worker connection before either object/vector hydration or the
            # relation sentinel read; overflow skips the whole pass.
            object_limit = self.settings.kg_conflict_max_objects
            object_count = self.knowledge.active_object_count(db, notebook_id)
            if object_count > object_limit:
                self.event_log.emit({
                    "kind": "kg_conflict_resolution_skipped",
                    "notebook_id": notebook_id,
                    "reason": "too_many_objects",
                })
                return {
                    "detected": 0,
                    "auto_applied": 0,
                    "queued": 0,
                    "truncated": 0,
                    "truncated_edge": 0,
                    "truncated_node": 0,
                    "skipped_llm": False,
                    "skipped_object_limit": True,
                }
            # Count admission happens before submission, but concurrent writes
            # may race it. LIMIT+1 is the worker-side memory guard: crossing the
            # rail skips the whole pass, never a partial relation universe.
            relation_limit = self.settings.kg_conflict_max_relations
            relations = self.governance_store.conflict_relation_rows(
                db,
                notebook_id,
                max_rows=relation_limit + 1,
            )
            if len(relations) > relation_limit:
                self.event_log.emit({
                    "kind": "kg_conflict_resolution_skipped",
                    "notebook_id": notebook_id,
                    "reason": "too_many_relations",
                })
                return {
                    "detected": 0,
                    "auto_applied": 0,
                    "queued": 0,
                    "truncated": 0,
                    "truncated_edge": 0,
                    "truncated_node": 0,
                    "skipped_llm": False,
                    "skipped_relation_limit": True,
                }
            obj_rows, vec_rows, nb_row = (
                self.governance_store.conflict_resolution_rows(db, notebook_id)
            )

        # Build objects list in detect_conflict_candidates format
        objects = []
        object_map: dict = {}  # object_id → row dict
        for row in obj_rows:
            payload = json.loads(row["payload"] or "{}")
            obj = {
                "id": row["id"],
                "object_type": row["object_type"],
                "payload": payload,
                "status": row["status"],
            }
            objects.append(obj)
            object_map[row["id"]] = obj

        # Build name lookup for edge-text rendering: object_id → name
        obj_name_map: dict = {
            obj["id"]: (obj["payload"].get("name", "") if isinstance(obj["payload"], dict) else "")
            for obj in objects
        }

        # 3. Build the embedding matrix; log + skip on any error
        # 运行时截断旁路(计划 §1.2,conflict 同步接线):此处原先连存储维过滤都
        # 没有 —— conflict_detect._cosine_sim 虽已改混维零容忍,这里仍须①先按
        # 存储原生维过滤(异维残留出局)②通过后截断到运行时空间,保证语义策略
        # 收到的向量同维可比(而非靠下游把混维对静默判 0 丢召回)。
        # 表示是一整块 (N, dim) float32 —— 每条向量 `.tolist()` 成 Python float
        # 列表的旧写法在 1024 维下每条约 8 KB(列表对象+指针数组+装箱 float),
        # 15 万对象就是 5 GB;同一批数据作矩阵是 N×dim×4 字节(约 0.6 GB),
        # 且预分配一次、逐行写入,不留下同量级的中间列表。
        embeddings = None
        if vec_rows:
            try:
                import numpy as np

                from app.services.kg.conflict_detect import EmbeddingMatrix
                from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec
                _dim = self.settings.embed_dim
                _rd = resolve_runtime_dim(self.settings)
                matrix = None
                row_by_id: dict = {}
                for r in vec_rows:
                    if not r["vector"]:
                        continue
                    arr = decode_vector(r["vector"])
                    if arr is None or arr.size != _dim:
                        continue
                    if _rd:
                        arr = truncate_vec(arr, _rd)
                    if matrix is None:
                        matrix = np.empty(
                            (len(vec_rows), int(arr.size)), dtype=np.float32
                        )
                    elif int(arr.size) != matrix.shape[1]:
                        continue
                    row = len(row_by_id)
                    matrix[row] = arr
                    row_by_id[r["object_id"]] = row
                if matrix is not None and row_by_id:
                    embeddings = EmbeddingMatrix(
                        matrix[:len(row_by_id)], row_by_id
                    )
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

        def _note_ann_failure(group_size: int) -> None:
            self.event_log.emit({
                "kind": "kg_conflict_semantic_ann_failed",
                "notebook_id": notebook_id,
                "group_size": int(group_size),
            })

        candidates = detect_conflict_candidates(
            objects,
            relations,
            embeddings=embeddings,
            sim_threshold=self.settings.kg_conflict_sim_threshold,
            semantic_bruteforce_max=(
                self.settings.kg_conflict_semantic_bruteforce_max
            ),
            semantic_ann_k=self.settings.kg_conflict_semantic_ann_k,
            on_semantic_ann_failure=_note_ann_failure,
        )

        if not candidates:
            return {
                "detected": 0,
                "auto_applied": 0,
                "queued": 0,
                "truncated": 0,
                "truncated_edge": 0,
                "truncated_node": 0,
                "skipped_llm": False,
            }

        # 4b. Cap what reaches adjudication.  Every candidate costs one LLM call,
        # so an unbounded detector output is an unbounded model bill on a large
        # notebook.  Truncation follows the detector's own emission order and is
        # disclosed (return value + a counts-only event) rather than silent.
        detected_total = len(candidates)
        candidates, truncated_by_kind = self._apply_candidate_quota(
            candidates, self.settings.kg_conflict_max_candidates
        )
        truncated = sum(truncated_by_kind.values())
        if truncated:
            self.event_log.emit({
                "kind": "kg_conflict_candidates_truncated",
                "notebook_id": notebook_id,
                "detected": detected_total,
                "kept": len(candidates),
                "truncated": truncated,
                "truncated_edge": truncated_by_kind["edge"],
                "truncated_node": truncated_by_kind["node"],
            })

        # 5. Materialise T3 input items
        # Build relation lookup: rel_id → relation dict
        rel_map: dict = {r["id"]: r for r in relations}

        # Evidence is read here, by id, for the surviving candidates only — the
        # notebook-wide scans in step 2 deliberately left it behind.
        self._hydrate_conflict_evidence(candidates, object_map, rel_map)

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
        verdicts = review_conflict_candidates(
            self.model_clients.chat("kg_conflict_review"), items
        )

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
            # ``detected`` stays "what was adjudicated" (auto_applied + queued);
            # ``truncated`` carries what the cap dropped before adjudication.
            "detected": len(candidates),
            "auto_applied": auto_applied,
            "queued": queued,
            "truncated": truncated,
            "truncated_edge": truncated_by_kind["edge"],
            "truncated_node": truncated_by_kind["node"],
            "skipped_llm": False,
        }

    @staticmethod
    def _apply_candidate_quota(
        candidates: List[dict], cap: int
    ) -> "tuple[List[dict], dict]":
        """Cut candidates to ``cap`` with a per-signal-class quota.

        A flat prefix cut is not neutral: the detector emits every edge-shaped
        candidate (strategies 1/2) before the first node-shaped one, and one
        notebook with a heavily linked hub can produce thousands of
        ``shared_head`` pairs.  Prefix truncation then spends the entire budget
        on that one strategy and the nmos/pmos-style discriminative candidates —
        the ones this feature exists for — vanish as a class.

        So each class gets half the budget; whatever a class does not use is
        handed to the other. Within a class the detector's emission order is
        preserved, and the merged output keeps the detector's global order so
        downstream ``zip(candidates, verdicts)`` still lines up.
        """
        by_kind = {"edge": 0, "node": 0}
        for candidate in candidates:
            by_kind["edge" if candidate["kind"] == "edge" else "node"] += 1

        half = cap // 2
        quota = {
            "edge": min(by_kind["edge"], half),
            "node": min(by_kind["node"], cap - min(by_kind["edge"], half)),
        }
        # Hand any slack the node class left over back to the edge class.
        quota["edge"] = min(by_kind["edge"], cap - quota["node"])

        taken = {"edge": 0, "node": 0}
        kept: List[dict] = []
        for candidate in candidates:
            kind = "edge" if candidate["kind"] == "edge" else "node"
            if taken[kind] < quota[kind]:
                taken[kind] += 1
                kept.append(candidate)
        return kept, {
            "edge": by_kind["edge"] - taken["edge"],
            "node": by_kind["node"] - taken["node"],
        }

    _CONFLICT_EVIDENCE_ID_BATCH = 500

    def _hydrate_conflict_evidence(
        self, candidates: List[dict], object_map: dict, rel_map: dict
    ) -> None:
        """Attach evidence to exactly the objects/relations the candidates cite.

        Bounded by construction: at most ``2 × kg_conflict_max_candidates`` ids,
        read in batches so the ``IN`` list never grows into a giant parameter
        list (same 500-id convention the bulk KG deletes use).
        """
        object_ids: set = set()
        relation_ids: set = set()
        for candidate in candidates:
            refs = (candidate["left_ref"], candidate["right_ref"])
            if candidate["kind"] == "edge":
                relation_ids.update(refs)
            else:
                object_ids.update(refs)
        object_ids &= set(object_map)
        relation_ids &= set(rel_map)
        if not object_ids and not relation_ids:
            return

        batch = self._CONFLICT_EVIDENCE_ID_BATCH

        def _batches(ids: set) -> list:
            ordered = sorted(ids)
            return [
                ordered[start:start + batch]
                for start in range(0, len(ordered), batch)
            ]

        with self._connect() as db:
            for chunk in _batches(object_ids):
                for row in self.knowledge.object_evidence_rows(db, chunk):
                    target = object_map.get(row["id"])
                    if target is not None:
                        target["evidence"] = json.loads(row["evidence"] or "[]")
            for chunk in _batches(relation_ids):
                rows = self.governance_store.conflict_relation_evidence_rows(
                    db, chunk
                )
                for row in rows:
                    target = rel_map.get(row["id"])
                    if target is not None:
                        target["evidence"] = json.loads(row["evidence"] or "[]")

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
                self.model_clients.chat("kg_merge_review"), pending,
                batch_size=self.settings.kg_merge_review_batch_size,
                max_workers=self.model_clients.parallelism("kg_merge_review"),
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
        # Only a confirmed merge changes clustering.  Rejected/deferred rows are
        # durable cannot-links for future rebuilds; the current graph remains
        # valid and must not be marked dirty merely because the queue shrank.
        if confirmed:
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

    def _resolve_promotion_target(
        self, db: object, notebook_id: str, target_base_id: str = ""
    ) -> str:
        """挂 0 个公共库 → 拒绝；挂 1 个 → 默认它；挂 >1 个 → 必须显式指定且必须
        在挂载集合内(设计 §6 晋升目标)。Shared by propose_promotion and
        propose_memory_promotion — both write into the SAME
        promotion_candidates.target_base_id column, and the approval side
        (approve_promotion_in_transaction / approve_memory_promotion_in_transaction)
        reads it uniformly, so both proposal paths must resolve it the same way."""
        allowed = self.governance_store.mounted_public_base_ids(db, notebook_id)
        if not allowed:
            raise PromotionTargetError("该笔记本尚未挂载任何公共知识库，无法提交晋升")
        target = (target_base_id or "").strip()
        if not target:
            if len(allowed) > 1:
                raise PromotionTargetError("挂载了多个公共知识库，请指定晋升目标")
            target = allowed[0]
        if target not in allowed:
            raise PromotionTargetError("晋升目标必须是本笔记本已挂载的公共知识库")
        return target

    @staticmethod
    def _memory_promotion_payload(item, snapshot: Optional[dict] = None) -> dict:
        pinned = snapshot or {}
        candidates = pinned.get("candidates", [])
        return {
            "name": str(pinned.get("title") or item.title),
            "title": str(pinned.get("title") or item.title),
            "memory_id": item.id,
            "candidates": candidates if isinstance(candidates, list) else [],
        }

    def propose_memory_promotion(
        self, item, candidates: List[dict], user_id: str, *, target_base_id: str = ""
    ) -> dict:
        """Place a creator-owned confirmed Memory into the existing curator queue.

        Task 8 (multi-domain base libraries) adds target_base_id, mirroring
        propose_promotion's kwarg: which mounted public reference library to
        promote into (required only when more than one is mounted; see
        _resolve_promotion_target, shared with the knowledge-object path)."""
        self.get_notebook(item.notebook_id)
        if item.created_by != user_id:
            raise KeyError(item.id)
        if item.status != "confirmed":
            raise ValueError("only confirmed Memory can be promoted")
        now = self._now()
        with self._write() as db:
            existing = self.governance_store.active_promotion_for_object(db, item.id)
            if existing is not None:
                current = self.memory_store.promotion_rows_on(db, [item.id])[item.id]
                snapshot = self.memory_store.pinned_promotion_snapshot(
                    current, str(existing["id"]), required=False
                )
                return promotion_row_to_dict(
                    existing,
                    payload=self._memory_promotion_payload(current, snapshot),
                    evidence=[Evidence(**card) for card in snapshot.get("evidence", [])],
                    source_revision=int(snapshot.get("source_revision") or 0),
                )
            target = self._resolve_promotion_target(db, item.notebook_id, target_base_id)
            cand_id = self._new_id("promo")
            current = self.memory_store.promotion_rows_on(db, [item.id]).get(item.id)
            if current is None:
                raise KeyError(item.id)
            evidence = self.governance_store.safe_memory_evidence(
                db, current.notebook_id, current.provenance
            )
            self.governance_store.insert_promotion_candidate(
                db, cand_id, item.notebook_id, item.id, "memory", now,
                target_base_id=target,
            )
            current = self.memory_store.propose_promotion_on(
                db, item.id, user_id, cand_id, candidates, evidence, item, now
            )
            snapshot = self.memory_store.pinned_promotion_snapshot(current, cand_id)
            row = self.governance_store.promotion_candidate_row(db, cand_id)
        return promotion_row_to_dict(
            row,
            payload=self._memory_promotion_payload(current, snapshot),
            evidence=[Evidence(**card) for card in snapshot.get("evidence", [])],
            source_revision=int(snapshot.get("source_revision") or 0),
        )

    def propose_promotion(
        self, notebook_id: str, object_id: str, *, target_base_id: str = ""
    ) -> dict:
        """Propose a personal-KG object for promotion into the base corpus.

        Idempotent for an already-active proposal of the same object. Raises
        KeyError if the notebook or object is missing; ValueError if the
        notebook is itself a base notebook (use the review gate there instead),
        or if ``target_base_id`` cannot be resolved against this notebook's
        mounted public reference libraries (0 mounted → reject; 1 → default;
        >1 → the caller must pass target_base_id explicitly — see
        _resolve_promotion_target).
        """
        self.get_notebook(notebook_id)  # KeyError if notebook missing
        now = self._now()
        with self._write() as db:
            obj = self.governance_store.promotion_object_type_row(
                db, notebook_id, object_id
            )
            if obj is None:
                raise KeyError(object_id)
            nb_row = self.governance_store.notebook_tier_row(db, notebook_id)
            if nb_row and nb_row["tier"] == "base":
                raise ValueError("cannot propose from a base notebook — use the review gate")
            # Idempotency: return any active (non-approved, non-rejected) proposal.
            existing = self.governance_store.active_promotion_for_object(db, object_id)
            if existing is not None:
                return promotion_row_to_dict(existing)
            target = self._resolve_promotion_target(db, notebook_id, target_base_id)
            cand_id = self._new_id("promo")
            self.governance_store.insert_promotion_candidate(
                db, cand_id, notebook_id, object_id, obj["object_type"], now,
                target_base_id=target,
            )
            row = self.governance_store.promotion_candidate_row(db, cand_id)
        return promotion_row_to_dict(row)

    def list_promotion_queue(self, status_filter: Optional[str] = None) -> List[dict]:
        """List promotion candidates across all notebooks (the curator sees
        everything). Defaults to the active queue (proposed + under_review);
        pass status_filter to view a single status. Denormalises payload +
        evidence from knowledge_objects for display, and (Task 13 审查 #4)
        target_base_name from notebooks — the curator otherwise has no way to
        know which library a candidate targets unless they happen to own it
        (notebooks.find on the frontend's own notebook list misses every
        public base someone else created).

        Batched (house pattern, see _hydrate_search_hits): one `id IN (...)`
        knowledge_objects lookup for the whole queue instead of a per-row
        SELECT — was N+1 (one round-trip per candidate). target_base_name
        resolution follows the same pattern: one `id IN (...)` notebooks
        lookup for the whole queue, not a per-row SELECT."""
        with self._connect() as db:
            rows = self.governance_store.promotion_queue_rows(db, status_filter)
            object_ids = list(dict.fromkeys(
                r["object_id"] for r in rows if r["object_type"] != "memory"
            ))
            memory_ids = list(dict.fromkeys(
                r["object_id"] for r in rows if r["object_type"] == "memory"
            ))
            obj_by_id: Dict[str, RepositoryRow] = {}
            for i in range(0, len(object_ids), _IN_CHUNK):
                batch = object_ids[i:i + _IN_CHUNK]
                for r in self.governance_store.promotion_object_rows(db, batch):
                    obj_by_id[r["id"]] = r
            memory_by_id = self.memory_store.promotion_rows_on(db, memory_ids)
            target_ids = list(dict.fromkeys(
                r["target_base_id"] for r in rows if r["target_base_id"]
            ))
            name_by_id: Dict[str, str] = {}
            for i in range(0, len(target_ids), _IN_CHUNK):
                batch = target_ids[i:i + _IN_CHUNK]
                for r in self.governance_store.notebook_name_rows(db, batch):
                    name_by_id[r["id"]] = r["name"]
            out: List[dict] = []
            for row in rows:
                target_base_name = name_by_id.get(row["target_base_id"], "")
                memory = memory_by_id.get(row["object_id"])
                if row["object_type"] == "memory":
                    snapshot = (
                        self.memory_store.pinned_promotion_snapshot(
                            memory, str(row["id"]), required=False
                        )
                        if memory
                        else {}
                    )
                    safe_evidence = [
                        Evidence(**evidence)
                        for evidence in snapshot.get("evidence", [])
                        if isinstance(evidence, dict)
                    ]
                    out.append(
                        promotion_row_to_dict(
                            row,
                            payload=(
                                self._memory_promotion_payload(memory, snapshot)
                                if memory
                                else {}
                            ),
                            evidence=safe_evidence,
                            source_revision=int(snapshot.get("source_revision") or 0),
                            target_base_name=target_base_name,
                        )
                    )
                    continue
                obj = obj_by_id.get(row["object_id"])
                payload = json.loads(obj["payload"] or "{}") if obj else {}
                evidence = (
                    [Evidence(**e) for e in json.loads(obj["evidence"] or "[]")]
                    if obj
                    else []
                )
                out.append(
                    promotion_row_to_dict(
                        row, payload=payload, evidence=evidence,
                        target_base_name=target_base_name,
                    )
                )
        return out

    def approve_promotion(self, candidate_id: str, reviewer_id: str = "") -> dict:
        """Approve a promotion: copy the personal object into the base corpus,
        deduplicating against existing base objects of the same type via the
        kg_merge seed clustering. Idempotent. Raises KeyError if the candidate
        is missing; ValueError if it is rejected or there is no base notebook.
        """
        now = self._now()
        with self._write() as db:
            identity = self.governance_store.promotion_candidate_identity(
                db, candidate_id
            )
            if identity is None:
                raise KeyError(candidate_id)
            locked_memory = None
            if identity["object_type"] == "memory":
                locked_memory = self.memory_store.lock_promotion_memory_on(
                    db, identity["object_id"], identity["notebook_id"]
                )
            cand = self.governance_store.promotion_candidate_row(db, candidate_id)
            if cand is None:
                raise KeyError(candidate_id)
            if (
                cand["object_id"] != identity["object_id"]
                or cand["notebook_id"] != identity["notebook_id"]
                or cand["object_type"] != identity["object_type"]
            ):
                raise ValueError("promotion candidate routing changed")
            if cand["status"] == "rejected":
                raise ValueError("cannot approve a rejected promotion candidate")
            if cand["object_type"] == "memory":
                memory, _legacy_candidates, existing_ids = (
                    self.memory_store.promotion_data_on(db, cand["object_id"])
                )
                if locked_memory is None or memory.id != locked_memory.id:
                    raise KeyError(cand["object_id"])
                if cand["status"] == "approved" and existing_ids:
                    return {
                        "candidate_id": candidate_id,
                        "base_object_id": existing_ids[0],
                        "base_object_ids": existing_ids,
                        "merged_into": cand["base_match_id"] or "",
                    }
                snapshot = self.memory_store.pinned_promotion_snapshot(
                    memory, candidate_id
                )
                self.memory_store.validate_pinned_promotion_on(
                    db, memory, candidate_id, snapshot
                )
                extracted = [
                    dict(candidate)
                    for candidate in snapshot.get("candidates", [])
                    if isinstance(candidate, dict)
                ]
                reviewer = reviewer_id or self.governance_store.first_admin_user_id(db)
                if not reviewer:
                    raise ValueError("no administrator available for Memory review")
                evidence = [
                    dict(card)
                    for card in snapshot.get("evidence", [])
                    if isinstance(card, dict)
                ]
                approval = self.governance_store.approve_memory_promotion_in_transaction(
                    db, candidate_id, extracted, evidence, reviewer, now
                )
                base_ids = approval["base_object_ids"] or existing_ids
                if memory.promotion_state != "approved":
                    self.memory_store.record_promotion_decision_on(
                        db,
                        memory.id,
                        "approved",
                        reviewer,
                        now,
                        base_object_ids=base_ids,
                    )
                memory_approval = (approval, list(base_ids))
            else:
                memory_approval = None
            if memory_approval is not None:
                pass
            else:
                was_approved = cand["status"] == "approved"
                src_payload = (
                    json.loads(
                        (self.governance_store.object_payload_row(
                            db, cand["object_id"]
                        ) or {"payload": None})["payload"]
                        or "{}"
                    )
                    if not was_approved
                    else {}
                )
                if reviewer_id:
                    approval = self.governance_store.approve_promotion_in_transaction(
                        db, candidate_id, now, reviewer_id
                    )
                else:
                    approval = self.governance_store.approve_promotion_in_transaction(
                        db, candidate_id, now
                    )
            # Idempotency: an already-approved candidate returns the existing
            # base object with NO post-commit hooks — exactly the old
            # early-return-inside-the-transaction behavior.
            if memory_approval is None and was_approved:
                return {
                    "candidate_id": candidate_id,
                    "base_object_id": approval.base_object_id,
                    "base_object_ids": [approval.base_object_id] if approval.base_object_id else [],
                    "merged_into": cand["base_match_id"] or "",
                }

        if memory_approval is not None:
            memory_result, base_ids = memory_approval
            for object_id in memory_result["created_object_ids"]:
                with self._connect() as db:
                    payload_row = self.governance_store.object_payload_row(db, object_id)
                if payload_row is not None:
                    self._embed_knowledge(
                        object_id,
                        memory_result["base_notebook_id"],
                        json.loads(payload_row["payload"] or "{}"),
                    )
            self._invalidate_unified_cache(memory_result["base_notebook_id"])
            self._mark_unified_kg_dirty(memory_result["base_notebook_id"])
            return {
                "candidate_id": candidate_id,
                "base_object_id": base_ids[0] if base_ids else "",
                "base_object_ids": base_ids,
                "merged_into": (
                    memory_result["merged_object_ids"][0]
                    if memory_result["merged_object_ids"] else ""
                ),
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
            "base_object_ids": [approval.base_object_id] if approval.base_object_id else [],
            "merged_into": "" if approval.created_new_object else approval.base_object_id,
        }

    def reject_promotion(
        self, candidate_id: str, reason: str = "", reviewer_id: str = ""
    ) -> dict:
        """Reject a promotion candidate. The personal object is left untouched.
        Raises KeyError if missing; ValueError if already approved."""
        now = self._now()
        with self._write() as db:
            identity = self.governance_store.promotion_candidate_identity(
                db, candidate_id
            )
            if identity is None:
                raise KeyError(candidate_id)
            locked_memory = None
            if identity["object_type"] == "memory":
                locked_memory = self.memory_store.lock_promotion_memory_on(
                    db, identity["object_id"], identity["notebook_id"]
                )
            cand = self.governance_store.promotion_candidate_row(db, candidate_id)
            if cand is None:
                raise KeyError(candidate_id)
            if (
                cand["object_id"] != identity["object_id"]
                or cand["notebook_id"] != identity["notebook_id"]
                or cand["object_type"] != identity["object_type"]
            ):
                raise ValueError("promotion candidate routing changed")
            if cand["status"] == "approved":
                raise ValueError("cannot reject an approved promotion candidate")
            if cand["status"] == "rejected":
                return promotion_row_to_dict(cand)
            reviewer = (
                reviewer_id
                or (
                    self.governance_store.first_admin_user_id(db)
                    if cand["object_type"] == "memory"
                    else "curator"
                )
            )
            self.governance_store.set_promotion_rejected(
                db, candidate_id, reason, now, reviewer
            )
            if cand["object_type"] == "memory":
                if locked_memory is None:
                    raise KeyError(cand["object_id"])
                self.memory_store.record_promotion_decision_on(
                    db,
                    cand["object_id"],
                    "rejected",
                    reviewer,
                    now,
                    reason=reason,
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
        # honor_quotes=False: both sides are STORED TEXT, not a user query. A
        # document that quotes someone is not declaring a search constraint, so
        # merge similarity must keep reading `"..."` as ordinary words.
        keyword = max(
            keyword_score(text_a, text_b, honor_quotes=False),
            keyword_score(text_b, text_a, honor_quotes=False),
        )
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
