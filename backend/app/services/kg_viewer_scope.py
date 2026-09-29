"""The viewer's readable-source rule for KG browse reads (PR-A·A5).

User rulings Q4 and M1 (2026-09-29): a KG browse surface shows a viewer only
what derives from sources that viewer may read — the notebook's visible
sources plus the viewer's OWN hidden sources (Knowhow projections, which are
notebook-wide, and the viewer's own confirmed Memory).  A KG object,
occurrence or relation derived from a ``memory`` source is visible only when
``memory_items.created_by`` is the viewer.

This module is the single service-side definition of that rule.  It does not
spell a predicate of its own: "which sources are Memory" is
``SourceStore.memory_source_ids`` and "which hidden sources belong to this
viewer" is ``SourceStore.hidden_source_ids`` (the same call the Ask path
freezes into its scope), and the readable set is ``all_visible_source_ids ∪
hidden_source_ids`` exactly as ``source_scope.scoped_allowed_source_ids``
documents for a single-notebook run.  Browse endpoints consume a
``KgViewerScope`` and never re-derive any part of it.

Cost shape.  ``for_notebook`` returns ``None`` — "no filtering, today's bytes"
— whenever the notebook holds no Memory source that is foreign to the viewer.
That probe is two index-bounded reads sized by the notebook's Memory/Knowhow
count (measured 0.7 ms on a 49,000-source notebook), so the common case pays
nothing else.  Only a notebook where another member has confirmed Memory
builds the readable set (one visible-universe read, 17–30 ms at 49,000
sources) and, on the surfaces that list objects, the hidden-object set (per
foreign Memory source one ``source_id``-indexed object read, plus one
reverse-index read once that index is certified; ~14 ms for 20 Memory
sources on a 150,000-object notebook).
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional

_ID_BATCH = 900


class KgViewerScope:
    """One viewer's readable-source rule over one notebook's KG.

    Built only when the notebook holds Memory the viewer does not own; a
    ``None`` scope means nothing is filtered.
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
        # Memory sources of this notebook the viewer does not own.
        self.foreign = foreign
        self._allowed: Optional[FrozenSet[str]] = None
        self._hidden: Optional[tuple] = None

    # -- sources -----------------------------------------------------------
    def allowed_source_ids(self) -> FrozenSet[str]:
        """visible ∪ the viewer's own hidden sources — the ceiling handed to
        ``node_context``.  A frozenset, built once per scope, so the store can
        take it as-is."""
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

    def evidence_hidden(self, evidence: Any) -> bool:
        """An object whose evidence references a foreign Memory source and no
        source the viewer may read is invisible to the viewer.  Objects with
        no attributable evidence, or with at least one readable source, stay
        visible (their foreign evidence items are filtered separately)."""
        sources = {
            str(s) for s in self._reader.knowledge.source_ids_from_evidence(evidence)
        }
        if not sources or sources.isdisjoint(self.foreign):
            return False
        return sources.isdisjoint(self.allowed_source_ids())

    # -- objects and clusters ----------------------------------------------
    def _hidden_state(self) -> tuple:
        if self._hidden is not None:
            return self._hidden
        reader = self._reader
        knowledge = reader.knowledge
        notebook_id = self.notebook_id
        with reader.connect() as db:
            # Candidates: every live object extracted from a foreign Memory
            # source (``source_id`` index, rows carry their evidence) plus —
            # once the reverse index is certified — every object whose
            # evidence merely references one.  The uncertified reverse-index
            # read is a keyset scan over the notebook's evidence JSON per
            # source (measured 1.6 s for 20 Memory sources on a 150k-object
            # notebook), so it is not taken on a browse read; an object not
            # extracted from that Memory keeps its own source's evidence, so
            # it can never have that Memory as its only readable-or-not source.
            evidence: Dict[str, Any] = {}
            for source_id in sorted(self.foreign):
                for row in knowledge.relink_object_rows_for_source(db, notebook_id, source_id):
                    evidence[row["id"]] = row["evidence"]
            if knowledge.source_index_backfilled(db, notebook_id):
                extra: set = set()
                for source_id in sorted(self.foreign):
                    extra.update(knowledge.stale_object_ids_for_source(db, source_id, notebook_id))
                ordered_extra = sorted(str(oid) for oid in extra if oid not in evidence)
                for start in range(0, len(ordered_extra), _ID_BATCH):
                    batch = ordered_extra[start:start + _ID_BATCH]
                    live = {
                        row["id"] for row in knowledge.object_meta_rows(db, batch)
                        if row["status"] != "deprecated"
                    }
                    for row in knowledge.object_evidence_rows(db, batch):
                        if row["id"] in live:
                            evidence[row["id"]] = row["evidence"]
            hidden = sorted(
                oid for oid, ev in evidence.items() if self.evidence_hidden(ev)
            )
            per_canonical: Counter = Counter()
            for start in range(0, len(hidden), _ID_BATCH):
                for row in reader.unified_kg.cluster_fold_rows(
                    db, notebook_id, hidden[start:start + _ID_BATCH]
                ):
                    per_canonical[row["canonical_id"]] += 1
            hidden_canonicals = frozenset(
                canonical_id for canonical_id, count in per_canonical.items()
                if knowledge.concept_cluster_member_total(
                    db, notebook_id, canonical_id
                ) <= count
            )
        self._hidden = (frozenset(hidden), hidden_canonicals, dict(per_canonical))
        return self._hidden

    @property
    def hidden_objects(self) -> FrozenSet[str]:
        return self._hidden_state()[0]

    @property
    def hidden_canonicals(self) -> FrozenSet[str]:
        """Clusters every live member of which is hidden from the viewer."""
        return self._hidden_state()[1]

    def hidden_member_count(self, canonical_id: str) -> int:
        return int(self._hidden_state()[2].get(canonical_id, 0))

    def node_hidden(self, node_id: str) -> bool:
        """A graph node id is either a raw object id or a folded canonical id."""
        return node_id in self.hidden_objects or node_id in self.hidden_canonicals

    def cluster_display_name(self, canonical_id: str, name: str) -> str:
        """The label a cluster shows this viewer.

        A cluster's stored ``canonical_name`` (and the label the viz artifact
        bakes from one member's payload) may be a hidden member's name, so a
        cluster that still has hidden members is labelled with its first
        visible member (member-id order) instead.  One bounded page read,
        sized by the cluster's hidden-member count; clusters without hidden
        members keep ``name`` untouched."""
        hidden = self.hidden_member_count(canonical_id)
        if not hidden:
            return name
        with self._reader.connect() as db:
            rows, _stored = self._reader.knowledge.concept_cluster_detail_rows(
                db, self.notebook_id, canonical_id, limit=hidden + 1
            )
        for row in rows:
            if row["member_object_id"] not in self.hidden_objects:
                # Same text-JSON row shape concept_detail decodes.
                payload = json.loads(row["payload"] or "{}")
                return str(payload.get("name", "") or "")
        return name


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
    ) -> None:
        self.database = database
        self.sources = sources
        self.knowledge = knowledge
        self.unified_kg = unified_kg
        self.current_user_id = current_user_id

    def connect(self):
        return self.database.connect()

    def for_notebook(self, notebook_id: str) -> Optional[KgViewerScope]:
        with self.connect() as db:
            memory = self.sources.memory_source_ids(db, notebook_id)
        if not memory:
            return None
        own = frozenset(
            str(s) for s in self.sources.hidden_source_ids(
                notebook_id, self.current_user_id()
            ) if s
        )
        foreign = frozenset(str(s) for s in memory if s and str(s) not in own)
        if not foreign:
            return None
        return KgViewerScope(self, notebook_id, own_hidden=own, foreign=foreign)
