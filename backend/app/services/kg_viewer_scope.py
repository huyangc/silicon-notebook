"""The viewer's readable-source rule for the KG reads (PR-A·A5, E4-7).

User rulings Q4 and M1 (2026-09-29).  The notebook's visible sources plus the
viewer's OWN hidden sources — Knowhow projections, which are notebook-wide,
and Memory the viewer created — are what the viewer may read in a notebook
they are a member of.  In a mounted library they are not a member of, they
read that library's visible sources only.

What this module guarantees, and for which reads: object context
(``/objects/{id}/context``), concept detail (``/concepts/{id}/detail``) and
neighbour hydration (``/objects/{id}/neighbors``) never return

* an object that exists only for someone else — its own ``source_id`` (the
  source it was extracted from) is a hidden source the viewer may not read:
  404 for context, dropped from members, attached objects and neighbour
  nodes;
* a cluster every live member of which is such an object;
* an occurrence, definition, step text or evidence item attributed to a
  source the viewer may not read;
* a cluster label taken from such an object.

Since E4-7 the same rule also scopes the knowledge list (rows and total
through the store keyword ``viewer_id``, evidence items here), the type
counts, KG search, the legacy ``/graph`` and the unified graph (the shared
graph plus the viewer's own Memory).  What it does NOT decide: pending
merges/conflicts, duplicate groups, the edge review queue and the analysis
artifacts -- shared tools from which the structural isolation (E4-2/E4-3,
the 0067/v87 migration) removes Memory-derived rows for everyone.

Single definition.  "Which sources are Memory" is
``SourceStore.memory_source_ids``; "which hidden sources belong to this
viewer" is ``SourceStore.hidden_source_ids`` (the call the Ask path freezes
into its scope); the visible half is ``all_visible_source_ids``; membership of
a mounted library is ``SharingStore.user_can_read_notebook``.  Endpoints
consume a ``KgViewerScope`` and re-derive none of it.

Cost shape.  ``for_notebook`` returns ``None`` — nothing filtered, today's
bytes — when the notebook holds no Memory that matters to the viewer (one
read, ``memory_source_ids``, for a notebook without Memory).  With Memory
present that decision is two index-bounded reads sized by the notebook's
Memory/Knowhow count (``memory_source_ids`` + ``hidden_source_ids``; a
mounted library read by a non-member adds one access check).  A filtered
read then builds, lazily and at most once per request: the readable set (one
visible-universe read) for the detail reads' evidence items, and the set of
objects OWNED by an unreadable hidden source (ONE statement whatever the
number of such sources, ``relink_object_rows_for_source(source_ids=...)``),
folded to clusters in batches of 900 object ids only by the reads that judge
a cluster id (neighbours, concept detail, folded search hits -- the latter
two label every checked cluster of a response in one batched member read);
the list page resolves its evidence items' elements in one read per page;
the neighbour view reads the focus row by primary key to route the viewer's
own Memory object to the live tables; the graph overlay adds one
such statement for the viewer's own Memory sources, and one metadata read
and one relation read per 900 of the objects it returns.  Neighbours then
read the first members of every cluster of the
response that needs a check in one batched statement
(``concept_cluster_detail_rows(canonical_ids=...)``), at most
``_FIRST_MEMBER_WINDOW`` per cluster; only a cluster none of whose first
members is visible is paged further, on its own.  Everything else is
decided on the rows the response already carries.

Identities.  Each id a read returns is judged on what it is (codex #806 r1):
a raw object by the object rule (its owner column); a folded cluster id by
its members — hidden when no live member is visible, labelled by its first
visible member when any member may be hidden (``cluster_needs_check``).
Legacy procedure steps are judged in the store, on the sibling's own
``source_id`` and on each evidence element's ACTUAL source.

M1 alignment (permission remediation E4-7, plan §2 D2/D4).  The object rule
is the Python twin of ``memory_sql.foreign_memory_object_excluded``: an
object (a relation) is hidden exactly when its OWN ``source_id`` is a hidden
source the viewer may not read.  Its evidence does not hide it; each evidence
item is judged on its own source instead (``evidence_hidden``) and dropped
alone.  The rule used to hide an object whose evidence cited an unreadable
source and nothing readable; D4 makes "derived from Memory" a property of the
primary source only, and the 0067/v87 migration strips Memory evidence from
shared objects, so an object extracted from a readable source keeps its name
and loses only the foreign items.  ``tests/test_kg_viewer_scope_twins.py``
(and its PostgreSQL twin) pins the Python rule against the SQL fragments on
one fixture set.

The viewer is the request user while the Memory channel is open (D5,
``memory_channel_allowed``) and the empty identity when it is closed: a
token without ``memory:read`` reads every Memory of the notebook as foreign,
its own included, so the scope exists whenever the notebook holds any Memory.

A scope is also returned when nothing is foreign but the viewer owns Memory
in the notebook (``own_memory``): the persisted graph artifacts are
viewer-independent and Memory-free (E4-6), so the graph and neighbour views
overlay the viewer's own Memory objects from the live tables.  Such a scope
does not filter (``filters`` is False); every filtering read goes through
``filtering`` and every store keyword through ``store_viewer_kwargs``, so an
overlay-only scope never changes a read.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from app.services import source_scope as _source_scope
from app.services.source_scope import (
    ceiling_binds,
    current_source_scope,
    live_universe_digests,
    source_scope_visible_universe_matches,
)

_ID_BATCH = 900

# ------------------------------------------------------------ assembly seams
# E4-7 lands beside two parallel changes it reads through; each seam below is
# the one line the assembly changes.
#
#: E4-4 adds the keyword ``viewer_id=`` to the KG store readers
#: (``list_knowledge_page``, ``type_counts``, ``fts_search``,
#: ``knowledge_type_count_rows``, ``notebook_has_kg``).  Until both are
#: assembled the stores do not take it and ``store_viewer_kwargs`` passes
#: nothing.  Assembly: ``True``.
STORE_READERS_TAKE_VIEWER_ID = False


def store_readers_take_viewer_id() -> bool:
    """The seam above, read at call time (callers in other modules must not
    copy the constant at import)."""
    return STORE_READERS_TAKE_VIEWER_ID


#: D5's switch (PR-E1): ``source_scope.memory_channel_allowed``, which E1
#: lands together with the context manager that closes the channel,
#: ``source_scope.memory_access_context``.  Before E1 neither name exists and
#: the channel is open (today's behaviour).  Once EITHER name is in
#: ``source_scope`` the import below is a hard ``from ... import``: a switch
#: that is missing or renamed at assembly fails this module's import, it never
#: reads as "open" (tests/test_kg_viewer_scope_assembly.py also pins the
#: closed context end to end once it exists).
if hasattr(_source_scope, "memory_access_context") or hasattr(
    _source_scope, "memory_channel_allowed"
):
    from app.services.source_scope import (  # noqa: E402
        memory_channel_allowed as _e1_memory_channel_allowed,
    )
else:
    _e1_memory_channel_allowed = None


def memory_channel_allowed() -> bool:
    """D5's switch (see ``_e1_memory_channel_allowed`` above), read at call
    time; open while E1 is not assembled."""
    if _e1_memory_channel_allowed is None:
        return True
    return bool(_e1_memory_channel_allowed())


def viewer_identity(user_id: str) -> str:
    """The identity a Memory predicate judges: the user while the Memory
    channel is open, the empty identity (owns no Memory) when it is closed."""
    return str(user_id or "") if memory_channel_allowed() else ""


def filtering(scope: Optional["KgViewerScope"]) -> Optional["KgViewerScope"]:
    """``scope`` when it hides something from the viewer, else ``None`` -- the
    guard every filtering read takes, so an overlay-only scope reads exactly
    what no scope reads."""
    return scope if scope is not None and scope.filters else None


def store_viewer_kwargs(scope: Optional["KgViewerScope"]) -> Dict[str, str]:
    """``{"viewer_id": ...}`` for a KG store reader -- only when the scope
    filters (plan §2 D2: the service passes ``viewer_id`` only when the
    notebook holds Memory the viewer may not read) and the stores take the
    keyword.  Otherwise ``{}``: the statement is today's, byte for byte."""
    if filtering(scope) is None or not store_readers_take_viewer_id():
        return {}
    return {"viewer_id": scope.viewer_id}

# The first window of a "first visible member" scan (codex #806 r3/r4):
# neighbour hydration reads at most this many member rows per checked cluster
# in its one batched statement, and ``visible_member`` starts with at most
# this many, whatever the cluster's suspect count; only a window that is full
# and holds nothing visible is widened (``_scan_members``).
_FIRST_MEMBER_WINDOW = 8


class KgViewerScope:
    """One viewer's readable-source rule over one notebook's KG.

    Built only when the notebook holds Memory that matters to the viewer:
    some hidden source is unreadable (``filters``), or the viewer owns Memory
    there (``own_memory``, the graph overlay).  A ``None`` scope means
    nothing is filtered and nothing is overlaid.
    """

    def __init__(
        self,
        reader: "KgViewerScopeReader",
        notebook_id: str,
        *,
        own_hidden: FrozenSet[str],
        foreign: FrozenSet[str],
        viewer_id: str = "",
        own_memory: FrozenSet[str] = frozenset(),
    ) -> None:
        self._reader = reader
        self.notebook_id = notebook_id
        self.own_hidden = own_hidden
        # Hidden sources of this notebook the viewer may not read: other
        # members' Memory (every Memory when the channel is closed), plus the
        # library's Knowhow projections for a non-member reading a mounted
        # library.
        self.foreign = foreign
        # The identity the store keyword ``viewer_id`` carries (D2): the user,
        # or '' when the Memory channel is closed.
        self.viewer_id = viewer_id
        # The viewer's own Memory sources of this notebook (empty when the
        # channel is closed): the graph overlay's input.
        self.own_memory = own_memory
        self._allowed: Optional[FrozenSet[str]] = None
        self._owned: Optional[FrozenSet[str]] = None
        self._owned_per_cluster: Optional[Dict[str, int]] = None
        self._own_objects: Optional[Tuple[str, ...]] = None

    @property
    def filters(self) -> bool:
        """Whether this scope hides anything from the viewer."""
        return bool(self.foreign)

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

    # -- the object rule (Python twin of memory_sql, plan §2 D2) --------------
    def evidence_hidden(self, item: Any) -> bool:
        """ONE evidence item, judged on its OWN source: hidden when that
        source is a hidden source of this notebook the viewer may not read
        (the twin of ``NOT memory_source_readable`` on the item's source).
        An item that is not a mapping cannot be attributed and is hidden
        (fail closed).  An object is never hidden by its evidence; its
        unreadable items are dropped one by one (the list page:
        ``list_evidence_hidden``, which also judges the element's actual
        source).  Cheaper than ``filter_evidence`` (no visible-universe read)
        and narrower: an item naming a source of another notebook, or none,
        is not this rule's to judge.  Items come as dicts (raw rows) or
        ``Evidence`` models (the list page)."""
        if isinstance(item, dict):
            source_id = item.get("source_id")
        elif hasattr(item, "source_id"):
            source_id = getattr(item, "source_id")
        else:
            return True
        return str(source_id or "") in self.foreign

    def row_hidden(self, source_id: Any) -> bool:
        """The object rule on one row (the twin of
        ``foreign_memory_object_excluded``): the object's OWN source is a
        hidden source the viewer may not read.  Its name and payload were
        extracted from that source (D4), so it is hidden whatever its evidence
        cites, and also when it has none."""
        return bool(source_id) and str(source_id) in self.foreign

    def relation_hidden(self, source_id: Any) -> bool:
        """The relation rule (the twin of ``foreign_memory_relation_excluded``):
        same judgement on the relation's own ``source_id``."""
        return self.row_hidden(source_id)

    # -- objects owned by unreadable sources (concept detail, neighbours) ----
    @property
    def owned_hidden(self) -> FrozenSet[str]:
        """Live objects OWNED by an unreadable hidden source -- under the
        owner-only rule exactly the live objects the rule hides, notebook-wide,
        so a cluster with no owned member provably has no hidden member.  ONE
        statement whatever the number of such sources
        (``relink_object_rows_for_source(source_ids=...)``), once per scope.
        The per-cluster fold is a separate, on-demand read
        (``_owned_clusters``): the graph and raw-hit reads need only this set."""
        if self._owned is None:
            with self._reader.connect() as db:
                self._owned = frozenset(
                    str(r["id"])
                    for r in self._reader.knowledge.relink_object_rows_for_source(
                        db, self.notebook_id, source_ids=sorted(self.foreign))
                )
        return self._owned

    def _owned_clusters(self) -> Dict[str, int]:
        """``{canonical_id: owned-hidden live members}`` -- ``owned_hidden``
        folded to clusters in batches of 900 ids, read only by the reads that
        judge a CANONICAL id (neighbours, concept detail, folded search
        hits), once per scope."""
        if self._owned_per_cluster is None:
            fold = sorted(self.owned_hidden)
            per_owned: Counter = Counter()
            if fold:
                with self._reader.connect() as db:
                    for start in range(0, len(fold), _ID_BATCH):
                        for row in self._reader.unified_kg.cluster_fold_rows(
                            db, self.notebook_id, fold[start:start + _ID_BATCH]
                        ):
                            per_owned[str(row["canonical_id"])] += 1
            self._owned_per_cluster = dict(per_owned)
        return self._owned_per_cluster

    def owned_member_count(self, canonical_id: str) -> int:
        return int(self._owned_clusters().get(canonical_id, 0))

    def cluster_needs_check(self, canonical_id: str) -> bool:
        """Whether a cluster may hold a hidden member — the identity check
        for a CANONICAL id (codex #806 r1): some live member is owned by an
        unreadable hidden source.  Any other cluster is left as it is: no
        member of it is hidden, so neither is it or its label."""
        return bool(self._owned_clusters().get(canonical_id))

    def hidden_member_bound(self, canonical_id: str) -> int:
        """The cluster's owned-hidden member count.  It only ever LOWERS a
        first window below ``_FIRST_MEMBER_WINDOW`` (a cluster with fewer
        hidden members needs fewer rows to meet a visible member); it never
        sizes a read (codex #806 r3/r4: a hub's count did)."""
        return self.owned_member_count(canonical_id)

    def object_hidden(self, object_id: Any) -> bool:
        """The object rule for a LIVE row that carries its id but not its
        owner column (cluster members, attached objects)."""
        return str(object_id) in self.owned_hidden

    def member_hidden(self, row: Any) -> bool:
        """A live cluster member row (``member_object_id``)."""
        return self.object_hidden(row["member_object_id"])

    # -- the shared view and the owner overlay (graph and neighbour views) ---
    def own_memory_object_ids(self) -> Tuple[str, ...]:
        """Live objects owned by the viewer's own Memory sources, in
        insertion order.  One statement (``relink_object_rows_for_source``,
        ids bound through ``id_binding``); none when the viewer owns none."""
        if self._own_objects is None:
            if not self.own_memory:
                self._own_objects = ()
            else:
                with self._reader.connect() as db:
                    self._own_objects = tuple(
                        str(r["id"]) for r in
                        self._reader.knowledge.relink_object_rows_for_source(
                            db, self.notebook_id, source_ids=sorted(self.own_memory))
                    )
        return self._own_objects

    def memory_object_ids(self) -> FrozenSet[str]:
        """Every live object the SHARED view leaves out: owned by a source
        hidden from the viewer, or by the viewer's own Memory (which the
        overlay adds back as its own layer).  The shared view is therefore
        the same for every viewer of the notebook."""
        return self.owned_hidden | frozenset(self.own_memory_object_ids())

    def own_memory_graph(
        self, *, cap: Optional[int], concept_only: bool, name_only: bool,
    ) -> Tuple[List[dict], List[dict], int]:
        """The owner overlay: ``(nodes, edges, total)`` of the viewer's own
        Memory objects, read from the live tables (the persisted artifacts
        hold nobody's Memory, E4-6).  ``total`` counts every such object of
        the requested level; ``nodes`` are the first ``cap`` of them in
        insertion order (``cap`` bounds this layer alone: the graph views
        append it to a shared page that ``cap`` bounds separately, so a
        response holds at most ``2 × cap`` nodes); ``edges`` are the live
        relations whose BOTH
        endpoints are among ``nodes`` (a relation lives inside one source,
        so a Memory relation joins Memory objects of the same Memory).
        Reads: the owned-id statement, one metadata read and one relation
        read per 900 kept ids -- bounded by ``cap``."""
        ids = list(self.own_memory_object_ids())
        if not ids:
            return [], [], 0
        knowledge = self._reader.knowledge
        rows: Dict[str, Any] = {}
        # Only the level filter needs every object's type; otherwise the
        # first ``cap`` ids are all the metadata the answer uses.
        wanted = ids if concept_only or cap is None else ids[:max(0, int(cap))]
        with self._reader.connect() as db:
            for start in range(0, len(wanted), _ID_BATCH):
                for row in knowledge.object_meta_rows_for_notebook(
                    db, self.notebook_id, wanted[start:start + _ID_BATCH]
                ):
                    rows[str(row["id"])] = row
            ordered = [
                oid for oid in wanted if oid in rows
                and (not concept_only or rows[oid]["object_type"] == "concept")
            ]
            total = len(ordered) if concept_only or cap is None else len(ids)
            kept = ordered if cap is None else ordered[:max(0, int(cap))]
            kept_set = set(kept)
            edges: List[dict] = []
            seen: set = set()
            for start in range(0, len(kept), _ID_BATCH):
                for rel in knowledge.neighbor_relation_rows(
                    db, self.notebook_id, kept[start:start + _ID_BATCH]
                ):
                    key = (str(rel["source_object_id"]), str(rel["target_object_id"]),
                           rel["edge_type"])
                    if key in seen or key[0] not in kept_set or key[1] not in kept_set:
                        continue
                    seen.add(key)
                    edges.append({"source_object_id": key[0],
                                  "target_object_id": key[1],
                                  "edge_type": key[2]})
        nodes = []
        for oid in kept:
            payload = _payload(rows[oid]["payload"])
            nodes.append({
                "id": oid,
                "object_type": rows[oid]["object_type"],
                "payload": {"name": payload.get("name", "")} if name_only else payload,
            })
        return nodes, edges, total

    def _raw_objects(self, object_ids: Sequence[str]) -> tuple:
        """``(hidden, known)``: which ids are hidden raw objects, and which are
        raw objects at all (any status) — ids outside ``known`` are folded
        cluster ids (or unknown), judged by the cluster check instead.

        Each row is judged by the object rule on its OWN ``source_id``
        (``row_hidden``), not only through ``owned_hidden``: that set holds
        live objects, and the DB neighbour path can return a deprecated one
        (merges keep the merged-away object's relations) whose owner is an
        unreadable source."""
        owned = self.owned_hidden
        hidden = {oid for oid in object_ids if oid in owned}
        known = set(hidden)
        rest = sorted({oid for oid in object_ids if oid not in owned})
        if rest:
            with self._reader.connect() as db:
                for start in range(0, len(rest), _ID_BATCH):
                    for row in self._reader.knowledge.object_evidence_rows(
                        db, rest[start:start + _ID_BATCH]
                    ):
                        known.add(str(row["id"]))
                        if self.row_hidden(row["source_id"]):
                            hidden.add(str(row["id"]))
        return frozenset(hidden), frozenset(known)

    def visible_member(self, canonical_id: str) -> Optional[Dict[str, Any]]:
        """The first visible member (member-id order) of a cluster, or
        ``None`` when no live member is visible.

        A first read of at most ``_FIRST_MEMBER_WINDOW`` member rows (no
        COUNT), widened by doubling only while a full window holds nothing
        visible (``_scan_members``)."""
        window = min(_FIRST_MEMBER_WINDOW, self.hidden_member_bound(canonical_id) + 1)
        with self._reader.connect() as db:
            return self._scan_members(db, canonical_id, after="", window=window)

    def _scan_members(
        self, db: Any, canonical_id: str, *, after: str, window: int,
    ) -> Optional[Dict[str, Any]]:
        """The first visible member of ``canonical_id`` after the keyset
        cursor ``after`` (member-id order), or ``None``.  Pages of ``window``
        rows, doubling while a full page holds nothing visible.  Terminates:
        each full page moves the cursor strictly forward over a finite member
        list, and a short page is the end of it.  A cluster with no visible
        member is therefore read to its end and answered ``None`` (hidden,
        fail closed), never assumed visible."""
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
        """``visible_member`` for many clusters: ONE batched read of the first
        ``window`` members of every listed cluster, where ``window`` is
        ``_FIRST_MEMBER_WINDOW`` (lower when no listed cluster's
        ``hidden_member_bound + 1`` reaches it) -- at most
        ``len(canonical_ids) × _FIRST_MEMBER_WINDOW`` rows, however large
        one cluster's suspect count is (codex #806 r3: a shared
        ``max(bound) + 1`` let one suspect hub size every cluster's window).

        Same answer as ``visible_member``: the first visible member in
        member-id order.  A cluster whose window is full yet holds nothing
        visible -- and only such a cluster -- is widened on its own from its
        last row (``_scan_members``, pages doubling from ``2 × window``), so
        it reads rows up to the page holding its first visible member, or its
        whole member list when it has none and is answered ``None`` (hidden).
        An id with no live member row maps to ``None``."""
        if not canonical_ids:
            return {}
        window = min(
            _FIRST_MEMBER_WINDOW,
            max(self.hidden_member_bound(cid) for cid in canonical_ids) + 1,
        )
        found: Dict[str, Optional[Dict[str, Any]]] = {}
        with self._reader.connect() as db:
            rows, _unused = self._reader.knowledge.concept_cluster_detail_rows(
                db, self.notebook_id, "", limit=window, canonical_ids=canonical_ids,
            )
            by_cluster: Dict[str, list] = {cid: [] for cid in canonical_ids}
            for row in rows:
                by_cluster.setdefault(str(row["canonical_id"]), []).append(row)
            for cid in canonical_ids:
                members = by_cluster.get(cid, [])
                visible = next((r for r in members if not self.member_hidden(r)), None)
                if visible is None and len(members) >= window:
                    visible = self._scan_members(
                        db, cid, after=str(members[-1]["member_object_id"]),
                        window=2 * window)
                found[cid] = visible
        return found

    def cluster_display_name(self, canonical_id: str, name: str) -> Optional[str]:
        """A cluster that may hold a hidden member (``cluster_needs_check``)
        is labelled with its first visible member's name (the stored
        ``canonical_name`` and the label the viz artifact bakes from one
        member's payload may be a hidden member's); other clusters keep
        ``name``.  ``None`` when such a cluster has no visible live member:
        it does not exist for this viewer, and the stored name -- which can
        only have come from a hidden member -- is never the answer (codex
        #806 r4)."""
        if not self.cluster_needs_check(canonical_id):
            return name
        row = self.visible_member(canonical_id)
        if row is None:
            return None
        # Same text-JSON row shape concept_detail decodes.
        return str(json.loads(row["payload"] or "{}").get("name", "") or "")

    def cluster_labels(self, canonical_ids: Sequence[str]) -> Dict[str, Optional[str]]:
        """``cluster_display_name`` for many clusters at once: for every id
        that may hold a hidden member (``cluster_needs_check``), its first
        visible member's name, or ``None`` when it has no visible live member
        (the cluster does not exist for this viewer).  Ids that need no check
        are absent from the answer (they keep their own name).  ONE batched
        member read for all of them (``_first_visible_members``), whatever
        their number -- KG search's folded hits and neighbour hydration."""
        checked = list(dict.fromkeys(
            str(cid) for cid in canonical_ids if self.cluster_needs_check(str(cid))))
        return {
            cid: None if row is None
            else str(json.loads(row["payload"] or "{}").get("name", "") or "")
            for cid, row in self._first_visible_members(checked).items()
        }

    def object_is_own_memory(self, notebook_id: str, object_id: str) -> bool:
        """Whether ``object_id`` is an object of the viewer's OWN Memory
        (its owner column is one of ``own_memory``): ONE primary-key read of
        that row, and none when the viewer owns no Memory here.  A folded
        cluster id is no row and answers False."""
        if not self.own_memory:
            return False
        row = self._reader.knowledge.get_object_row(notebook_id, object_id)
        return row is not None and str(row["source_id"] or "") in self.own_memory

    def list_evidence_hidden(self, items: Sequence[Any]) -> FrozenSet[int]:
        """Positions of the list page's evidence items the viewer may not
        see, each judged on the source it NAMES and on the source its element
        ACTUALLY lives in -- the rule concept detail applies
        (``KnowledgeQueryService._viewer_resolved_evidence``, codex #806 r1):
        an item that names a readable source but quotes an element of a
        hidden one carries that element's text.  An item without an element
        id is judged on the source it names alone.  Both surfaces resolve the
        element through the same store read, ``_enrich_evidence``, here once
        per DISTINCT element id of the page, in batches of 900 distinct ids
        (one statement for a typical page; a page is at most 200 objects).
        With the E4-4 stores assembled the read asks for the element's
        ``source_id`` only (``sources_only=True``: no element text);
        before them it reads the store's full enrichment.  Items come as
        ``Evidence`` models or dicts."""
        named = [self._as_evidence_dict(item) for item in items]
        hidden = {i for i, item in enumerate(named) if item is None
                  or self.evidence_hidden(item)}
        pending = [(i, str(item["element_id"])) for i, item in enumerate(named)
                   if i not in hidden and item.get("element_id")]
        elements = sorted({element for _i, element in pending})
        narrow = {"sources_only": True} if store_readers_take_viewer_id() else {}
        actual: Dict[str, str] = {}
        if elements:
            with self._reader.connect() as db:
                for start in range(0, len(elements), _ID_BATCH):
                    batch = elements[start:start + _ID_BATCH]
                    resolved = self._reader.knowledge._enrich_evidence(
                        db, [{"element_id": element, "source_id": ""}
                             for element in batch], **narrow)
                    actual.update(
                        (element, str(row.get("source_id") or ""))
                        for element, row in zip(batch, resolved))
        hidden.update(i for i, element in pending
                      if actual.get(element, "") in self.foreign)
        return frozenset(hidden)

    @staticmethod
    def _as_evidence_dict(item: Any) -> Optional[Dict[str, Any]]:
        if isinstance(item, dict):
            return item
        if hasattr(item, "source_id"):
            return {"source_id": getattr(item, "source_id", ""),
                    "element_id": getattr(item, "element_id", "") or ""}
        return None

    def filter_neighbourhood(
        self, nodes: List[dict], edges: List[dict], focus_ids: Iterable[str],
    ) -> Optional[tuple]:
        """Neighbour hydration under the rule: ``None`` when the focus itself
        is hidden, else ``(nodes, edges)`` without hidden objects, clusters
        with no visible live member, and edges touching either; clusters
        that may hold a hidden member are relabelled.

        Every id is judged on its own identity (codex #806 r1): a raw object
        by the object rule, a folded cluster id by ``cluster_needs_check`` and
        its members — a cluster none of whose members is owned by an
        unreadable source but every member of which cites only one is hidden
        like any other."""
        unique = list(dict.fromkeys(
            [str(n["id"]) for n in nodes] + [str(f) for f in focus_ids if f]))
        hidden_raw, known_raw = self._raw_objects(unique)
        labels: Dict[str, str] = {}
        hidden_clusters: set = set()
        for node_id, label in self.cluster_labels(
            [i for i in unique if i not in known_raw]
        ).items():
            if label is None:
                hidden_clusters.add(node_id)
            else:
                labels[node_id] = label
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

    def drop_hidden_graph(
        self, nodes: List[dict], edges: List[dict], *, shared: bool,
    ) -> Tuple[List[dict], List[dict]]:
        """A whole-notebook RAW graph read (LIVE objects, relations carrying
        their own ``source_id``) under the rule: without the objects it hides
        (``owned_hidden``) -- and, for the ``shared`` view, without the
        viewer's own Memory objects as well (``memory_object_ids``; the
        overlay adds them back as a layer of their own) -- without every
        relation whose own source is dropped the same way
        (``relation_hidden``), and without every relation touching a dropped
        object."""
        exclude = self.memory_object_ids() if shared else self.owned_hidden
        dropped_sources = self.foreign | self.own_memory if shared else self.foreign
        kept_nodes = [n for n in nodes if str(n["id"]) not in exclude]
        kept_edges = [
            e for e in edges
            if str(e.get("source_id") or "") not in dropped_sources
            and str(e["source_object_id"]) not in exclude
            and str(e["target_object_id"]) not in exclude
        ]
        return kept_nodes, kept_edges


def _payload(value: Any) -> dict:
    """A payload column as a dict (text JSON on SQLite and the PG compat rows,
    a dict elsewhere)."""
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _memory_split(
    sources: Any, connect: Callable[[], Any], notebook_id: str, viewer: str,
) -> tuple[FrozenSet[str], FrozenSet[str], FrozenSet[str]]:
    """``(Memory sources, viewer's own hidden sources, foreign Memory)``; the
    second read only when the library holds Memory."""
    with connect() as db:
        memory = frozenset(
            str(s) for s in sources.memory_source_ids(db, notebook_id) if s)
    if not memory:
        return frozenset(), frozenset(), frozenset()
    own = frozenset(str(s) for s in sources.hidden_source_ids(notebook_id, viewer) if s)
    return memory, own, memory - own


def foreign_memory_source_ids(
    sources: Any, connect: Callable[[], Any], notebook_id: str, viewer: str,
) -> tuple[FrozenSet[str], FrozenSet[str]]:
    """``(viewer's own hidden sources, Memory sources of other users)`` for one
    library — THE short-circuit probe, shared by the KG detail reads and the
    run-level ``ceiling_binds`` verdict.  A library with no Memory answers
    ``(∅, ∅)`` after one read (``memory_source_ids``); otherwise the second read
    is ``hidden_source_ids(notebook_id, viewer)``.  Both are bounded by the
    library's Memory/Knowhow count.  ``hidden_source_ids`` renders
    ``memory_source_readable``, so an orphan Memory source is foreign to
    everyone, and the empty identity (a closed Memory channel) owns none."""
    _memory, own, foreign = _memory_split(sources, connect, notebook_id, viewer)
    return own, foreign


class NodeContextCeilingVerdict:
    """``source_scope.ceiling_binds`` for the current run, with its two store
    probes: drift (the frozen lists against the library's current visible
    universe and the asker's hidden half) and foreign hidden sources
    (``foreign_memory_source_ids``).  Both probes read as the identity the
    freeze used (``scope.owner_id``), never whoever is current — this runs in
    detached workers."""

    def __init__(self, *, database: Any, sources: Any) -> None:
        self.database = database
        self.sources = sources

    def __call__(self, notebook_id: str) -> bool:
        scope = current_source_scope()
        if scope is None:
            return False
        library = notebook_id or scope.notebook_id

        def drifted() -> bool:
            # The one-row fingerprint (``universe_digest``), as the drift
            # probe reads it; a store without the keyword reads both sets.
            digests = live_universe_digests(
                self.sources.all_visible_source_ids, library, scope.owner_id)
            if digests is not None:
                return not source_scope_visible_universe_matches(
                    library, current_digests=digests)
            return not source_scope_visible_universe_matches(
                library,
                self.sources.all_visible_source_ids(library),
                self.sources.hidden_source_ids(library, scope.owner_id),
            )

        def foreign_hidden() -> bool:
            return bool(foreign_memory_source_ids(
                self.sources, self.database.connect, library, scope.owner_id)[1])

        return ceiling_binds(
            scope, notebook_id, drifted=drifted, foreign_hidden=foreign_hidden)


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
        "no filtering".

        The identity judged is ``viewer_identity``: with the Memory channel
        closed it is '' and every Memory of the notebook is foreign, the
        viewer's own included (plan correction 1).  A notebook without Memory
        answers ``None`` after one read, as before."""
        user = self.current_user_id()
        viewer = viewer_identity(user)
        member = (
            active_notebook_id is None
            or notebook_id == active_notebook_id
            or self.can_read_notebook(notebook_id, user)
        )
        if member:
            memory, own, foreign = _memory_split(
                self.sources, self.connect, notebook_id, viewer)
            own_memory = memory & own
        else:
            with self.connect() as db:
                memory = {str(s) for s in self.sources.memory_source_ids(db, notebook_id) if s}
            # Visible sources only: every hidden source is unreadable.  The
            # empty identity owns no Memory, so this read is exactly the
            # library's notebook-wide Knowhow half of ``hidden_source_ids``.
            own = frozenset()
            own_memory = frozenset()
            foreign = frozenset(
                memory | {str(s) for s in self.sources.hidden_source_ids(notebook_id, "") if s}
            )
        if not foreign and not own_memory:
            return None
        return KgViewerScope(
            self, notebook_id, own_hidden=own, foreign=foreign,
            viewer_id=viewer, own_memory=own_memory,
        )
