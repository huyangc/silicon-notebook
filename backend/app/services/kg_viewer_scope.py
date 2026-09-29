"""The viewer's readable-source rule for three KG detail reads (PR-A·A5).

User rulings Q4 and M1 (2026-09-29).  The notebook's visible sources plus the
viewer's OWN hidden sources — Knowhow projections, which are notebook-wide,
and Memory the viewer created — are what the viewer may read in a notebook
they are a member of.  In a mounted library they are not a member of, they
read that library's visible sources only.

What this module guarantees, and for which reads: object context
(``/objects/{id}/context``), concept detail (``/concepts/{id}/detail``) and
neighbour hydration (``/objects/{id}/neighbors``) never return

* an object that exists only for someone else — its own ``source_id`` (the
  source it was extracted from) is a hidden source the viewer may not read,
  or its evidence cites such a source and no source the viewer may read: 404
  for context, dropped from members, attached objects and neighbour nodes;
* a cluster every live member of which is such an object;
* an occurrence, definition, step text or evidence item attributed to a
  source the viewer may not read;
* a cluster label taken from such an object.

What it does NOT decide: edges and their ``edge_type`` /
``support_count`` / ``source_count`` produced by folding Memory-derived
objects into shared clusters and into the viz artifact (which carries no
provenance), nor the unified graph, KG search, pending merges/conflicts or
analysis artifacts.  Those are removed at the source by the structural
isolation task (Memory objects are not folded into shared clusters,
communities, canonical relations or analysis artifacts), not filtered here.

Single definition.  "Which sources are Memory" is
``SourceStore.memory_source_ids``; "which hidden sources belong to this
viewer" is ``SourceStore.hidden_source_ids`` (the call the Ask path freezes
into its scope); the visible half is ``all_visible_source_ids``; membership of
a mounted library is ``SharingStore.user_can_read_notebook``.  Endpoints
consume a ``KgViewerScope`` and re-derive none of it.

Cost shape.  ``for_notebook`` returns ``None`` — nothing filtered, today's
bytes — when no hidden source of the notebook is unreadable to the viewer.
With Memory present that decision is two index-bounded reads sized by the
notebook's Memory/Knowhow count (``memory_source_ids`` +
``hidden_source_ids``; a mounted library read by a non-member adds one
access check).  A filtered read then builds, lazily and at most once per
request: the readable set (one visible-universe read) for evidence items, and
— only for concept detail and neighbours — the set of objects OWNED by an
unreadable hidden source (ONE statement whatever the number of such sources,
``relink_object_rows_for_source(source_ids=...)``), folded to clusters in
batches of 900 object ids.  Neighbours then read the first members of every
partly hidden cluster of the response in one batched statement
(``concept_cluster_detail_rows(canonical_ids=...)``).  Everything else is
decided on the rows the response already carries.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence

_ID_BATCH = 900


class KgViewerScope:
    """One viewer's readable-source rule over one notebook's KG.

    Built only when some hidden source of the notebook is unreadable to the
    viewer; a ``None`` scope means nothing is filtered.
    """

    def __init__(
        self,
        reader: "KgViewerScopeReader",
        notebook_id: str,
        *,
        own_hidden: FrozenSet[str],
        foreign: FrozenSet[str],
    ) -> None:
        self._reader = reader
        self.notebook_id = notebook_id
        self.own_hidden = own_hidden
        # Hidden sources of this notebook the viewer may not read: other
        # members' Memory, plus the library's Knowhow projections for a
        # non-member reading a mounted library.
        self.foreign = foreign
        self._allowed: Optional[FrozenSet[str]] = None
        self._owned: Optional[tuple] = None

    # -- sources -----------------------------------------------------------
    def allowed_source_ids(self) -> FrozenSet[str]:
        """visible ∪ the viewer's own hidden sources — the ceiling handed to
        ``node_context``.  A frozenset, built once per scope, so the store
        takes it as-is."""
        if self._allowed is None:
            visible = self._reader.sources.all_visible_source_ids(self.notebook_id)
            self._allowed = frozenset(str(s) for s in visible if s) | self.own_hidden
        return self._allowed

    def source_readable(self, source_id: Any) -> bool:
        return bool(source_id) and str(source_id) in self.allowed_source_ids()

    def filter_evidence(self, items: Iterable[Any]) -> List[Any]:
        """Evidence items whose ``source_id`` the viewer may read.  An item
        without a source id cannot be attributed and is dropped — the same
        fail-closed rule ``node_context`` applies to occurrences under a
        ceiling."""
        return [
            item for item in items
            if isinstance(item, dict) and self.source_readable(item.get("source_id"))
        ]

    # -- the object rule -----------------------------------------------------
    def evidence_hidden(self, evidence: Any) -> bool:
        """The evidence half of the object rule: the evidence cites an
        unreadable hidden source and no source the viewer may read."""
        sources = {
            str(s) for s in self._reader.knowledge.source_ids_from_evidence(evidence)
        }
        if not sources or sources.isdisjoint(self.foreign):
            return False
        return sources.isdisjoint(self.allowed_source_ids())

    def row_hidden(self, source_id: Any, evidence: Any) -> bool:
        """The whole object rule on one row.  The owner half comes first: an
        object's name and payload were extracted from its own source, so an
        object owned by an unreadable hidden source is hidden whatever else
        its evidence cites (merged and promoted objects carry the union), and
        also when it has no evidence at all — the same answer the enumeration
        list gives."""
        if source_id and str(source_id) in self.foreign:
            return True
        return self.evidence_hidden(evidence)

    # -- objects owned by unreadable sources (concept detail, neighbours) ----
    def _owned_state(self) -> tuple:
        if self._owned is not None:
            return self._owned
        reader = self._reader
        knowledge = reader.knowledge
        notebook_id = self.notebook_id
        with reader.connect() as db:
            # ONE statement for every unreadable source (the ids travel as one
            # array / JSON parameter), then the folds in batches of 900 ids.
            ordered = sorted({
                str(row["id"])
                for row in knowledge.relink_object_rows_for_source(
                    db, notebook_id, source_ids=sorted(self.foreign)
                )
            })
            per_canonical: Counter = Counter()
            for start in range(0, len(ordered), _ID_BATCH):
                for row in reader.unified_kg.cluster_fold_rows(
                    db, notebook_id, ordered[start:start + _ID_BATCH]
                ):
                    per_canonical[str(row["canonical_id"])] += 1
        self._owned = (frozenset(ordered), dict(per_canonical))
        return self._owned

    @property
    def owned_hidden(self) -> FrozenSet[str]:
        """Live objects owned by an unreadable hidden source (the owner half
        of the rule, notebook-wide).  Objects hidden only by the evidence half
        are not in this set; every read that returns objects evaluates that
        half on the rows it carries (``member_hidden`` / ``objects_hidden``),
        so an object that is readable-owned but cites nothing readable is
        still dropped — it just does not size an over-fetch."""
        return self._owned_state()[0]

    def owned_member_count(self, canonical_id: str) -> int:
        return int(self._owned_state()[1].get(canonical_id, 0))

    def object_hidden(self, object_id: Any, evidence: Any) -> bool:
        """The object rule for a row that carries its id and evidence but not
        its owner column: the owner half through ``owned_hidden``."""
        return str(object_id) in self.owned_hidden or self.evidence_hidden(evidence)

    def member_hidden(self, row: Any) -> bool:
        """A cluster member row (``member_object_id`` + ``evidence``)."""
        return self.object_hidden(row["member_object_id"], row["evidence"])

    def objects_hidden(self, object_ids: Sequence[str]) -> FrozenSet[str]:
        """Which of ``object_ids`` (raw object ids; unknown ids, e.g. folded
        cluster ids, are simply absent from the rows) are hidden.  One batched
        primary-key read per 900 ids for the evidence half."""
        owned = self.owned_hidden
        hidden = {oid for oid in object_ids if oid in owned}
        rest = sorted({oid for oid in object_ids if oid not in owned})
        if rest:
            with self._reader.connect() as db:
                for start in range(0, len(rest), _ID_BATCH):
                    for row in self._reader.knowledge.object_evidence_rows(
                        db, rest[start:start + _ID_BATCH]
                    ):
                        if self.evidence_hidden(row["evidence"]):
                            hidden.add(str(row["id"]))
        return frozenset(hidden)

    def visible_member(self, canonical_id: str) -> Optional[Dict[str, Any]]:
        """The first visible member (member-id order) of a cluster that has
        owned-hidden members, or ``None`` when every live member is hidden.

        A bounded read of ``owned + 1`` member rows (no COUNT), widened by
        doubling only while a full window holds nothing visible (members
        hidden by the evidence half alone)."""
        window = self.owned_member_count(canonical_id) + 1
        after = ""
        with self._reader.connect() as db:
            while True:
                rows, _stored = self._reader.knowledge.concept_cluster_detail_rows(
                    db, self.notebook_id, canonical_id, limit=window, after=after
                )
                for row in rows:
                    if not self.member_hidden(row):
                        return row
                if len(rows) < window:
                    return None
                after = str(rows[-1]["member_object_id"])
                window *= 2

    def _first_visible_members(
        self, canonical_ids: List[str],
    ) -> Dict[str, Optional[Dict[str, Any]]]:
        """``visible_member`` for many clusters with ONE batched read: the
        first ``max(owned) + 1`` members of every listed cluster (so at most
        ``len(canonical_ids) × (max owned + 1)`` rows). A cluster whose window
        is full yet holds nothing visible (members hidden by the evidence half
        alone) falls back to ``visible_member``'s widening scan."""
        if not canonical_ids:
            return {}
        window = max(self.owned_member_count(cid) for cid in canonical_ids) + 1
        with self._reader.connect() as db:
            rows, _unused = self._reader.knowledge.concept_cluster_detail_rows(
                db, self.notebook_id, "", limit=window, canonical_ids=canonical_ids,
            )
        by_cluster: Dict[str, list] = {cid: [] for cid in canonical_ids}
        for row in rows:
            by_cluster.setdefault(str(row["canonical_id"]), []).append(row)
        found: Dict[str, Optional[Dict[str, Any]]] = {}
        for cid in canonical_ids:
            members = by_cluster.get(cid, [])
            visible = next((r for r in members if not self.member_hidden(r)), None)
            if visible is None and len(members) >= window:
                visible = self.visible_member(cid)
            found[cid] = visible
        return found

    def cluster_display_name(self, canonical_id: str, name: str) -> str:
        """A cluster that has owned-hidden members is labelled with its first
        visible member's name (the stored ``canonical_name`` and the label the
        viz artifact bakes from one member's payload may be a hidden
        member's); other clusters keep ``name``."""
        if not self.owned_member_count(canonical_id):
            return name
        row = self.visible_member(canonical_id)
        if row is None:
            return name
        # Same text-JSON row shape concept_detail decodes.
        return str(json.loads(row["payload"] or "{}").get("name", "") or "")

    def filter_neighbourhood(
        self, nodes: List[dict], edges: List[dict], focus_ids: Iterable[str],
    ) -> Optional[tuple]:
        """Neighbour hydration under the rule: ``None`` when the focus itself
        is hidden, else ``(nodes, edges)`` without hidden objects, clusters
        whose every live member is hidden, and edges touching either;
        partly hidden clusters are relabelled."""
        ids = [str(n["id"]) for n in nodes] + [str(f) for f in focus_ids if f]
        hidden_raw = self.objects_hidden(ids)
        labels: Dict[str, str] = {}
        hidden_clusters: set = set()
        for node_id, row in self._first_visible_members(
            [i for i in dict.fromkeys(ids) if self.owned_member_count(i)]
        ).items():
            if row is None:
                hidden_clusters.add(node_id)
            else:
                labels[node_id] = str(
                    json.loads(row["payload"] or "{}").get("name", "") or "")
        gone = hidden_raw | hidden_clusters
        if any(str(f) in gone for f in focus_ids if f):
            return None
        kept_nodes = []
        for node in nodes:
            node_id = str(node["id"])
            if node_id in gone:
                continue
            if node_id in labels:
                node = {**node, "payload": {**(node.get("payload") or {}),
                                            "name": labels[node_id]}}
            kept_nodes.append(node)
        kept = {str(n["id"]) for n in kept_nodes} | {str(f) for f in focus_ids if f}
        kept_edges = [
            e for e in edges
            if str(e["source_object_id"]) in kept and str(e["target_object_id"]) in kept
        ]
        return kept_nodes, kept_edges


class KgViewerScopeReader:
    """Builds ``KgViewerScope`` for the request's current user."""

    def __init__(
        self,
        *,
        database: Any,
        sources: Any,
        knowledge: Any,
        unified_kg: Any,
        current_user_id: Callable[[], str],
        can_read_notebook: Callable[[str, str], bool],
    ) -> None:
        self.database = database
        self.sources = sources
        self.knowledge = knowledge
        self.unified_kg = unified_kg
        self.current_user_id = current_user_id
        self.can_read_notebook = can_read_notebook

    def connect(self):
        return self.database.connect()

    def for_notebook(
        self, notebook_id: str, active_notebook_id: Optional[str] = None,
    ) -> Optional[KgViewerScope]:
        """``active_notebook_id`` is the notebook the route authorised; a
        different ``notebook_id`` is a mounted library, whose own members get
        the member rule and everyone else its visible sources only.  Any read
        failure propagates: a scope that cannot be built never degrades to
        "no filtering"."""
        viewer = self.current_user_id()
        member = (
            active_notebook_id is None
            or notebook_id == active_notebook_id
            or self.can_read_notebook(notebook_id, viewer)
        )
        with self.connect() as db:
            memory = {str(s) for s in self.sources.memory_source_ids(db, notebook_id) if s}
        if member:
            if not memory:
                return None
            own = frozenset(
                str(s) for s in self.sources.hidden_source_ids(notebook_id, viewer) if s
            )
            foreign = frozenset(memory - own)
        else:
            # Visible sources only: every hidden source is unreadable.  The
            # empty identity owns no Memory, so this read is exactly the
            # library's notebook-wide Knowhow half of ``hidden_source_ids``.
            own = frozenset()
            foreign = frozenset(
                memory | {str(s) for s in self.sources.hidden_source_ids(notebook_id, "") if s}
            )
        if not foreign:
            return None
        return KgViewerScope(self, notebook_id, own_hidden=own, foreign=foreign)
