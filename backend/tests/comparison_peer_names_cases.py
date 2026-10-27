"""Backend-neutral scenarios for rules R3 and R4 of the comparison peers
(plan 2026-10-23 "经挂载读的合并知识对象", task T2).

Comparison peers (co-mention siblings first, Louvain community peers as the
fallback) used to be named by the STORED cluster name. Under a per-library
source ceiling the SQL gate only asks whether a peer cluster has SOME
in-ceiling support, so the stored name could still be the spelling of a member
owned by an out-of-ceiling source (a Knowhow projection or a Memory), or a
stale name left on the surviving member rows after such a member was pruned.
R3: while a ceiling binds the library, a peer is named only from in-ceiling
members -- the stored name when one of the in-ceiling members read carries
it, else the first in-ceiling member with a non-empty name; a cluster with no
such member is dropped. An in-ceiling member follows the object rule: its OWN
``source_id`` is empty (a promoted object) or in the ceiling. R4: the focal
cluster resolved by its stored name must also have an in-ceiling member whose
name is the focal key, else the focal is unresolved.

The world is one library with a visible source ``s-open`` (the ceiling) and a
Knowhow source ``s-hidden`` outside it, built through the real ``store_kg`` /
``rebuild_unified_kg`` (object ids are made deterministic so member-id order is
known):

* ``K-quietzebraxylophone``: hidden ``Quiet Zebra Xylophone (QZX)`` x2, then
  the visible ``QZX``; the stored name is forced to the hidden spelling (the
  merge itself picks ``QZX``).
* ``K-pagedgammawidget``: nine hidden ``Paged Gamma Widget (PGW)``, then the
  visible ``PGW`` -- past the first member window, so it is paged.
* ``K-alphabetagamma``: visible ``ABG``, visible ``Alpha Beta Gamma (ABG)``,
  hidden ``Alpha Beta Gamma (ABG)``; the stored name (forced to the long form)
  is carried by an in-ceiling member that is not the first one.
* ``K-mixedwidget``: ``Mixed Widget`` from both sources (stored name kept).
* ``K-ghostwidget``: owned by ``s-hidden`` but citing ``s-open`` -- the SQL gate
  admits it, R3 drops it. It is the strongest co-mention peer, so under a
  ceiling it would take a ``top_k`` slot if the SQL limit were applied before R3.
* ``K-hiddenwidget``: ``s-hidden`` only -- the SQL gate drops it.

Co-mention and community rows are seeded in the real table shapes (as the
existing ``test_comparison_peer_ceiling.py`` fixture does).

The scenarios of each tuple are ordered so that one world can serve them in
turn: whatever a scenario changes and does not put back (co-mention rows
dropped for the community path, a deprecated member, pruned member rows) is
tolerated by every later one; a scenario that re-owns or renames a member for
its own case (promoted, empty-named, focal carrier) restores it before it ends.
SQLite builds a fresh world per scenario; the PostgreSQL twin, where the
rebuild and the schema migration dominate the cost, runs each tuple (and each
path) on one world.

``tests/test_comparison_peer_ceiling_names.py`` runs these on SQLite and
``tests/postgres/test_comparison_peer_ceiling_names_pg.py`` on PostgreSQL.
"""
from __future__ import annotations

import contextlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.models.schemas import NotebookCreate
from app.repositories.postgres.id_binding import ID_SEPARATOR
from app.services.communities import CommunityQueryService
from app.services.source_scope import source_scope_context
from tests.model_testkit import bind_chat_client

__all__ = ["PATHS", "PATH_SCENARIOS", "SCENARIOS", "build_world"]

FOCAL = "Focal Widget"
QZX = "QZX"
QZX_HIDDEN = "Quiet Zebra Xylophone (QZX)"
PGW = "PGW"
PGW_HIDDEN = "Paged Gamma Widget (PGW)"
ABG = "ABG"
ABG_LONG = "Alpha Beta Gamma (ABG)"
MIXED = "Mixed Widget"
GHOST = "Ghost Widget"
HIDDEN_ONLY = "Hidden Widget"
# A non-ASCII case variant: the visible member is ``Ärger Widget``, the hidden
# one (and the stored name) ``ärger widget`` -- equal only under a Unicode
# lowercase, which neither ``resolve_focal``'s SQL ``lower()`` (ASCII-only on
# both backends) nor R4's ``ascii_lower`` (``app.domain.sql_text_fold``) applies.
ARGER = "Ärger Widget"
ARGER_STORED = "ärger widget"

#: Never in anything a ceiling-bound asker receives (case-insensitive scan):
#: the hidden spelling, its merge seed, and its one distinctive word.
FORBIDDEN = ("quiet zebra xylophone", "quietzebraxylophone", "zebra")

QUESTION = f"{FOCAL} 相比其他"

# (canonical id, forced stored name or None, co-mention claims, community
#  centrality)
PEERS = (
    ("K-ghostwidget", None, 10, 0.9),
    ("K-quietzebraxylophone", QZX_HIDDEN, 9, 0.1),
    ("K-pagedgammawidget", PGW_HIDDEN, 8, 0.6),
    ("K-alphabetagamma", ABG_LONG, 7, 0.7),
    ("K-mixedwidget", None, 6, 0.8),
    ("K-hiddenwidget", None, 4, 1.0),
)
FOCAL_ID = "K-focalwidget"

# (object id suffix, name, which source the evidence cites)
OPEN_OBJECTS = (
    ("f-focal", FOCAL, "open"),
    ("q-b", QZX, "open"),
    ("m-b", MIXED, "open"),
    ("a-a", ABG, "open"),
    ("a-b", ABG_LONG, "open"),
    ("p-b", PGW, "open"),
    ("u-b", ARGER, "open"),
)
HIDDEN_OBJECTS = (
    ("q-a1", QZX_HIDDEN, "hidden"),
    ("q-a2", QZX_HIDDEN, "hidden"),
    ("m-a", MIXED, "hidden"),
    ("a-c", ABG_LONG, "hidden"),
    *((f"p-a{index:02d}", PGW_HIDDEN, "hidden") for index in range(9)),
    # owned by the hidden source, evidence citing the visible one
    ("g-a", GHOST, "open"),
    ("h-a", HIDDEN_ONLY, "hidden"),
    ("u-a", ARGER_STORED, "hidden"),
)

# Expected names, in each path's own order.
COMENTION_IN_CEILING = [QZX, PGW, ABG_LONG, MIXED]
COMENTION_NO_CEILING = [GHOST, QZX_HIDDEN, PGW_HIDDEN, ABG_LONG, MIXED, HIDDEN_ONLY]
COMMUNITY_IN_CEILING = [MIXED, ABG_LONG, PGW, QZX]
COMMUNITY_NO_CEILING = [HIDDEN_ONLY, GHOST, MIXED, ABG_LONG, PGW_HIDDEN, QZX_HIDDEN]


@dataclass
class World:
    repo: Any
    nb: str
    ph: str
    js: str
    service: CommunityQueryService
    events: list = field(default_factory=list)

    #: the visible source (the ceiling) and the Knowhow source outside it
    open = "s-open"
    hidden = "s-hidden"
    community = "cm-1"

    @staticmethod
    def oid(suffix: str) -> str:
        return f"ko-{suffix}"

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.repo._write() as db:
            db.execute(sql.replace("{ph}", self.ph).replace("{js}", self.js), params)

    def set_owner(self, suffix: str, source_id: str) -> None:
        """Re-own one object (``''`` is what promotion writes)."""
        self.execute("UPDATE knowledge_objects SET source_id={ph} WHERE id={ph}",
                     (source_id, self.oid(suffix)))

    def set_name(self, suffix: str, name: str) -> None:
        self.execute("UPDATE knowledge_objects SET payload={js} WHERE id={ph}",
                     (json.dumps({"name": name, "section_path": "1"}), self.oid(suffix)))

    def owner(self, suffix: str) -> Any:
        return self.rows("SELECT source_id FROM knowledge_objects WHERE id={ph}",
                         (self.oid(suffix),))[0]["source_id"]

    def arger_id(self) -> str:
        """The canonical id the merge mints for the Ärger pair."""
        return self.repo.cluster_map(self.nb)[self.oid("u-b")]

    def rows(self, sql: str, params: tuple = ()) -> list:
        with self.repo._write() as db:
            return [dict(row) for row in db.execute(sql.replace("{ph}", self.ph), params)]

    def ceiling(self):
        """The frozen per-library ceiling Ask installs: visible sources only."""
        return source_scope_context(
            self.nb, None, None,
            notebook_source_ceilings={self.nb: frozenset({self.open})},
        )

    def deny(self):
        """A ceiling frozen to zero sources: an explicit deny."""
        return source_scope_context(
            self.nb, None, None, notebook_source_ceilings={self.nb: frozenset()},
        )

    @property
    def unified(self):
        return self.repo._runtime.unified_kg


class _RecordingLog:
    def __init__(self, inner, sink: list) -> None:
        self._inner = inner
        self._sink = sink

    def emit(self, event):
        self._sink.append(dict(event))
        self._inner.emit(event)


def _store(world: World, source_id: str, items) -> None:
    """``store_kg`` with the object ids ``items`` names, in order: member-id
    order decides which member a window holds, so it must be known."""
    sources = {"open": world.open, "hidden": world.hidden}
    objects = [
        {"local_id": suffix, "object_type": "concept",
         "payload": {"name": name, "section_path": "1"},
         "evidence": [{"source_id": sources[cited], "source_title": sources[cited],
                       "element_id": "", "element_type": "paragraph",
                       "location_label": "p", "quoted_span": name,
                       "confidence": 1.0}]}
        for suffix, name, cited in items
    ]
    lifecycle = world.repo._runtime.knowledge_lifecycle
    queue = [world.oid(suffix) for suffix, _name, _cited in items]
    original = lifecycle._new_id

    def new_id(prefix: str) -> str:
        if prefix == "ko" and queue:
            return queue.pop(0)
        return f"{prefix}-{uuid.uuid4().hex}"

    lifecycle._new_id = new_id
    try:
        world.repo.store_kg(world.nb, source_id, objects, [])
    finally:
        lifecycle._new_id = original
    assert not queue


def build_world(repo, *, postgres: bool) -> World:
    runtime = repo._runtime
    events: list = []
    world = World(
        repo=repo, nb=repo.create_notebook(NotebookCreate(name="peer-names")).id,
        ph="%s" if postgres else "?", js="%s::jsonb" if postgres else "?",
        service=CommunityQueryService(
            notebooks=runtime.notebook_store, unified_kg=runtime.unified_kg,
            event_log=_RecordingLog(runtime.event_log, events),
            sibling_min_bridge=repo.settings.sibling_min_bridge,
        ),
        events=events,
    )
    for source_id, source_type in ((world.open, "markdown"), (world.hidden, "knowhow")):
        runtime.source_store.insert_source(
            source_id=source_id, notebook_id=world.nb, title=source_id,
            source_type=source_type, status="active", parse_status="parsed",
            file_name="", file_path="", file_size=0, file_hash="",
            summary="", doc_type="",
        )
    _store(world, world.open, OPEN_OBJECTS)
    _store(world, world.hidden, HIDDEN_OBJECTS)
    repo.rebuild_unified_kg(world.nb)
    generation = world.rows(
        "SELECT COALESCE(community_generation, 0) AS generation "
        "FROM unified_kg_state WHERE notebook_id={ph}", (world.nb,))[0]["generation"]
    member_sql = (
        "INSERT INTO community_members (canonical_id, notebook_id, level, "
        "community_id, canonical_name, centrality, generation) "
        "VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})"
    )
    for canonical_id, stored, claims, centrality in PEERS:
        if stored is not None:
            world.execute(
                "UPDATE concept_clusters SET canonical_name={ph} "
                "WHERE notebook_id={ph} AND canonical_id={ph}",
                (stored, world.nb, canonical_id))
        first, second = sorted((FOCAL_ID, canonical_id))
        world.execute(
            "INSERT INTO concept_comentions "
            "(notebook_id, canonical_a, canonical_b, bridge_claims) "
            "VALUES ({ph},{ph},{ph},{ph})", (world.nb, first, second, claims))
        world.execute(member_sql, (
            canonical_id, world.nb, 0, world.community,
            stored_name(world, canonical_id), centrality, generation))
    world.execute(member_sql, (
        FOCAL_ID, world.nb, 0, world.community, FOCAL, 0.5, generation))
    # The Ärger cluster lives apart from FOCAL's peers: one co-mention pair and
    # one community, both with Mixed, so it changes none of FOCAL's answers.
    arger = world.arger_id()
    world.execute(
        "UPDATE concept_clusters SET canonical_name={ph} "
        "WHERE notebook_id={ph} AND canonical_id={ph}", (ARGER_STORED, world.nb, arger))
    first, second = sorted((arger, "K-mixedwidget"))
    world.execute(
        "INSERT INTO concept_comentions "
        "(notebook_id, canonical_a, canonical_b, bridge_claims) "
        "VALUES ({ph},{ph},{ph},{ph})", (world.nb, first, second, 3))
    for canonical_id, name, centrality in ((arger, ARGER_STORED, 0.5),
                                           ("K-mixedwidget", MIXED, 0.4)):
        world.execute(member_sql, (
            canonical_id, world.nb, 0, "cm-2", name, centrality, generation))
    return world


def stored_name(world: World, canonical_id: str) -> str:
    names = {row["canonical_name"] for row in world.rows(
        "SELECT canonical_name FROM concept_clusters "
        "WHERE notebook_id={ph} AND canonical_id={ph}", (world.nb, canonical_id))}
    assert len(names) == 1, names
    return names.pop()


def _drop_comentions(world: World) -> None:
    """Leave only the community path: ``resolve_comparison_peers`` falls back."""
    world.execute("DELETE FROM concept_comentions WHERE notebook_id={ph}", (world.nb,))


def _names(world: World, path: str, *, question: str = "compare",
           top_k: int = 8, focal: str = FOCAL) -> list[str]:
    if path == "comention":
        return [name for name, _claims in world.service.sibling_peers(
            world.nb, focal, top_k=top_k)]
    return world.service.community_peers(
        world.nb, focal, question, top_k=top_k, candidates=16)


def assert_clean(payload: Any, *, expect_short: bool = True) -> None:
    """Case-insensitive scan of the whole serialised structure."""
    text = json.dumps(payload, ensure_ascii=False, default=str).lower()
    for needle in FORBIDDEN:
        assert needle not in text, (needle, payload)
    if expect_short:
        assert QZX.lower() in text, payload


@contextlib.contextmanager
def recorded_statements(world: World) -> Iterator[list[tuple[str, tuple]]]:
    """Every ``(statement, params)`` the unified store sends while the block
    runs (both backends: the store opens each read through
    ``database.connect()``)."""
    log: list[tuple[str, tuple]] = []
    unified = world.unified
    original = unified.database

    class _Connection:
        def __init__(self, inner) -> None:
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            log.append((str(sql), tuple(args[0]) if args else ()))
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _Database:
        @contextlib.contextmanager
        def connect(self):
            with original.connect() as db:
                yield _Connection(db)

        def __getattr__(self, name):
            return getattr(original, name)

    unified.database = _Database()
    try:
        yield log
    finally:
        unified.database = original


def _bound_ids(param: Any) -> list[str]:
    """The cluster ids one ``comparison_peer_members`` statement binds (its first
    parameter): a JSON array on SQLite, ``id_binding``'s text form (or an array)
    on PostgreSQL."""
    if isinstance(param, (list, tuple)):
        return [str(value) for value in param]
    text = str(param)
    if text.startswith("["):
        return [str(value) for value in json.loads(text)]
    return text.split(ID_SEPARATOR)


def _peer_member_reads(statements) -> list[tuple[str, tuple]]:
    return [(sql, params) for sql, params in statements if "member_name" in sql]


def member_reads(statements, *, focal: str = "") -> tuple[list[str], list[str]]:
    """``(batched first-window reads, keyset page reads)`` of
    ``comparison_peer_members`` for R3's peer names -- R4's pages of the focal
    cluster ``focal`` (the same read, listing that one cluster) left out."""
    reads = [sql for sql, params in _peer_member_reads(statements)
             if not focal or _bound_ids(params[0]) != [focal]]
    paged = [sql for sql in reads
             if "ko2.id > ?" in sql or 'ko.id COLLATE "C" > %s' in sql]
    return [sql for sql in reads if sql not in paged], paged


def focal_reads(statements, focal: str) -> list[str]:
    """R4's pages of the focal cluster: ``comparison_peer_members`` listing
    ``focal`` alone (a peer is never the focal, so R3 never lists it)."""
    return [sql for sql, params in _peer_member_reads(statements)
            if _bound_ids(params[0]) == [focal]]


def focal_read_limits(statements, focal: str) -> list[Any]:
    """The page size bound to each R4 focal page (the read's last parameter)."""
    return [params[-1] for _sql, params in _peer_member_reads(statements)
            if _bound_ids(params[0]) == [focal]]


def stored_name_resolutions(statements) -> list[str]:
    """``resolve_focal`` statements (the stored-name match)."""
    return [sql for sql, _params in statements if "lower(canonical_name)" in sql]


def comention_row_limits(statements) -> list[Any]:
    """The ``LIMIT`` bound to each co-mention row read (its last parameter)."""
    return [params[-1] for sql, params in statements
            if "FROM concept_comentions cm" in sql]


# --------------------------------------------------------------------- scenarios
def world_preconditions(world: World, monkeypatch) -> None:
    """The fixture is what the scenarios rely on."""
    cluster_of = world.repo.cluster_map(world.nb)
    for suffix, _name, _cited in (*OPEN_OBJECTS, *HIDDEN_OBJECTS):
        assert world.oid(suffix) in cluster_of, suffix
    for members, canonical_id in (
        (("q-b", "q-a1"), "K-quietzebraxylophone"),
        (("p-b", "p-a00"), "K-pagedgammawidget"),
        (("a-a", "a-c"), "K-alphabetagamma"),
        (("f-focal",), FOCAL_ID),
        (("u-a", "u-b"), world.arger_id()),
    ):
        assert {cluster_of[world.oid(suffix)] for suffix in members} == {canonical_id}
    assert stored_name(world, world.arger_id()) == ARGER_STORED
    # The stored names R3 has to deal with.
    assert stored_name(world, "K-quietzebraxylophone") == QZX_HIDDEN
    assert stored_name(world, "K-pagedgammawidget") == PGW_HIDDEN
    assert stored_name(world, "K-alphabetagamma") == ABG_LONG
    assert world.unified.top_community_for(world.nb, FOCAL_ID) == world.community
    # Ghost passes the SQL source gate (its evidence cites s-open) ...
    gated = world.unified.comention_peers(
        world.nb, FOCAL_ID, 1, 8, allowed_source_ids=[world.open])
    assert [name for name, _claims in gated] == [
        GHOST, QZX_HIDDEN, PGW_HIDDEN, ABG_LONG, MIXED]
    # ... but no member of it is owned by s-open.
    owners = {row["source_id"] for row in world.rows(
        "SELECT source_id FROM knowledge_objects WHERE id={ph}", (world.oid("g-a"),))}
    assert owners == {world.hidden}


def store_member_read_shape(world: World, monkeypatch) -> None:
    """``comparison_peer_members``: the first ``limit`` live members of every
    listed cluster in member-id order, each with its own source and name, and
    the keyset page after a cursor; the two ``with_canonical_id`` shapes."""
    unified, oid = world.unified, world.oid
    rows = unified.comparison_peer_members(
        world.nb, ["K-quietzebraxylophone", "K-pagedgammawidget", "K-nope"], limit=2)
    got = [(r["canonical_id"], r["member_object_id"], r["source_id"], r["member_name"])
           for r in rows]
    assert got == [
        ("K-pagedgammawidget", oid("p-a00"), world.hidden, PGW_HIDDEN),
        ("K-pagedgammawidget", oid("p-a01"), world.hidden, PGW_HIDDEN),
        ("K-quietzebraxylophone", oid("q-a1"), world.hidden, QZX_HIDDEN),
        ("K-quietzebraxylophone", oid("q-a2"), world.hidden, QZX_HIDDEN),
    ]
    page = unified.comparison_peer_members(
        world.nb, ["K-pagedgammawidget"], limit=16, after=oid("p-a07"))
    assert [(r["member_object_id"], r["source_id"], r["member_name"]) for r in page] == [
        (oid("p-a08"), world.hidden, PGW_HIDDEN), (oid("p-b"), world.open, PGW)]
    assert unified.comparison_peer_members(world.nb, [], limit=8) == []
    # A deprecated member is not a live member.
    world.execute("UPDATE knowledge_objects SET status='deprecated' WHERE id={ph}",
                  (oid("q-a1"),))
    rows = unified.comparison_peer_members(world.nb, ["K-quietzebraxylophone"], limit=8)
    assert [r["member_object_id"] for r in rows] == [oid("q-a2"), oid("q-b")]
    # The canonical id rides along only when asked for.
    assert unified.comention_peers(
        world.nb, FOCAL_ID, 1, 2, allowed_source_ids=[world.open],
        with_canonical_id=True,
    ) == [("K-ghostwidget", GHOST, 10), ("K-quietzebraxylophone", QZX_HIDDEN, 9)]
    assert unified.comention_peers(world.nb, FOCAL_ID, 1, 1) == [(GHOST, 10)]
    # R4 pages the focal cluster through the same read: the limit bounds the
    # members walked, whatever their names; the keyset resumes after the last.
    first = unified.comparison_peer_members(world.nb, ["K-pagedgammawidget"], limit=8)
    assert [r["member_object_id"] for r in first] == [
        oid(f"p-a{index:02d}") for index in range(8)]
    rest = unified.comparison_peer_members(
        world.nb, ["K-pagedgammawidget"], limit=16, after=oid("p-a07"))
    assert [(r["member_object_id"], r["member_name"]) for r in rest] == [
        (oid("p-a08"), PGW_HIDDEN), (oid("p-b"), PGW)]
    with_ids = unified.community_member_peers(
        world.nb, world.community, FOCAL_ID, 2, allowed_source_ids=[world.open],
        with_canonical_id=True)
    assert [(r["canonical_id"], r["canonical_name"]) for r in with_ids] == [
        ("K-ghostwidget", GHOST), ("K-mixedwidget", MIXED)]
    plain = unified.community_member_peers(world.nb, world.community, FOCAL_ID, 1)
    assert [set(dict(r)) for r in plain] == [{"canonical_name", "centrality"}]


def ranking_reads_the_in_ceiling_name(world: World, monkeypatch) -> None:
    """Community ranking (``keyword_score``) runs on the R3 name: a question
    spelling the hidden name out does not pull QZX to the top under a
    ceiling, while without one the stored spelling still ranks it first."""
    _drop_comentions(world)
    question = "Quiet Zebra Xylophone"
    with world.ceiling():
        assert _names(world, "community", question=question, top_k=1) == [MIXED]
    assert _names(world, "community", question=question, top_k=1) == [QZX_HIDDEN]


def comention_top_k_counts_named_peers(world: World, monkeypatch) -> None:
    """Under a ceiling the co-mention read asks SQL for twice ``top_k`` and
    cuts to ``top_k`` after R3, so Ghost -- the strongest pair, admitted by the
    gate, dropped by R3 -- does not take the only slot; without a ceiling the
    read keeps ``LIMIT top_k`` and answers as before."""
    with world.ceiling():
        with recorded_statements(world) as statements:
            one = world.service.sibling_peers(world.nb, FOCAL, top_k=1)
            two = world.service.sibling_peers(world.nb, FOCAL, top_k=2)
    assert one == [(QZX, 9)]
    assert two == [(QZX, 9), (PGW, 8)]
    assert comention_row_limits(statements) == [2, 4]
    with recorded_statements(world) as bare:
        assert world.service.sibling_peers(world.nb, FOCAL, top_k=1) == [(GHOST, 10)]
    assert comention_row_limits(bare) == [1]


def deny_ceiling_is_silent(world: World, monkeypatch) -> None:
    """A ceiling frozen to zero sources denies the library: both paths and
    ``resolve_comparison_peers`` answer empty at their entry -- not one
    statement, not one event -- even for a focal R4 would let through (its
    member promoted, ``source_id=''``)."""
    world.set_owner("f-focal", "")
    try:
        before = len(world.events)
        with world.deny():
            with recorded_statements(world) as statements:
                assert _names(world, "comention") == []
                assert _names(world, "community") == []
                assert world.service.resolve_comparison_peers(
                    world.nb, FOCAL, "compare", top_k=8, candidates=16,
                ) == ([], "community")
        assert statements == []
        assert world.events[before:] == []
    finally:
        world.set_owner("f-focal", world.open)


def hidden_spelling_never_names_a_peer(world: World, monkeypatch, path: str) -> None:
    """Both paths: in-ceiling names, hidden spelling gone, QZX present; the
    stored name survives where an in-ceiling member carries it (Mixed, and
    ABG's long form although its first in-ceiling member is ``ABG``); a
    cluster with no in-ceiling member (Ghost) is dropped; PGW is found on the
    page after the first window. Without a ceiling the stored names are back."""
    if path == "community":
        _drop_comentions(world)
    expected_in = COMENTION_IN_CEILING if path == "comention" else COMMUNITY_IN_CEILING
    expected_out = COMENTION_NO_CEILING if path == "comention" else COMMUNITY_NO_CEILING
    with world.ceiling():
        names = _names(world, path)
        resolved = world.service.resolve_comparison_peers(
            world.nb, FOCAL, "compare", top_k=8, candidates=16)
    assert names == expected_in
    assert resolved == (expected_in, path)
    assert_clean([names, resolved, world.events])
    assert _names(world, path) == expected_out


def absent_ceiling_reads_no_member(world: World, monkeypatch, path: str) -> None:
    """No ceiling: stored names, and not one member or focal read (zero extra
    statements -- the statement set itself is pinned byte for byte by
    ``test_comparison_peer_ceiling.py::test_absent_ceiling_is_byte_identical``);
    the co-mention read keeps ``LIMIT top_k``."""
    if path == "community":
        _drop_comentions(world)
    with recorded_statements(world) as statements:
        names = _names(world, path)
        hidden_focal = _names(world, path, focal=QZX_HIDDEN)
    assert names == (COMENTION_NO_CEILING if path == "comention"
                     else COMMUNITY_NO_CEILING)
    assert hidden_focal, "without a ceiling the stored spelling resolves as before"
    # Not one member read -- neither R3's nor R4's focal pages (same statement).
    assert member_reads(statements) == ([], [])
    assert not [sql for sql, _params in statements if "canonical_id, canonical_name" in sql]
    if path == "comention":
        assert comention_row_limits(statements) == [8, 8]


def one_batched_member_read(world: World, monkeypatch, path: str) -> None:
    """Five peers survive the SQL gate (QZX, PGW, ABG, Mixed, Ghost): their
    first windows come back in ONE statement; only PGW, whose whole first
    window is hidden, is paged on its own. The focal check (R4) is one more
    statement per resolved focal; a focal the stored names do not resolve
    costs none."""
    if path == "community":
        _drop_comentions(world)
    with world.ceiling():
        with recorded_statements(world) as statements:
            names = _names(world, path)
        with recorded_statements(world) as unresolved:
            assert _names(world, path, focal="No Such Widget") == []
    assert sorted(names) == sorted(COMENTION_IN_CEILING)
    batched, paged = member_reads(statements, focal=FOCAL_ID)
    assert len(batched) == 1, batched
    assert len(paged) == 1, paged
    assert len(focal_reads(statements, FOCAL_ID)) == 1, statements
    assert member_reads(unresolved) == ([], [])


def stale_stored_name_is_not_used(world: World, monkeypatch, path: str) -> None:
    """The hidden members' cluster rows are pruned (what a Knowhow
    re-projection / table delete does) while the surviving rows keep the old
    canonical id and name: R3 names the cluster from what is left."""
    with world.repo._write() as db:
        world.repo._runtime.knowledge.prune_cluster_rows_for_source(
            db, world.nb, world.hidden)
    members = world.unified.comparison_peer_members(
        world.nb, ["K-quietzebraxylophone"], limit=8)
    assert [r["member_object_id"] for r in members] == [world.oid("q-b")]
    assert stored_name(world, "K-quietzebraxylophone") == QZX_HIDDEN
    if path == "community":
        _drop_comentions(world)
    with world.ceiling():
        names = _names(world, path)
    assert sorted(names) == sorted([QZX, PGW, ABG_LONG, MIXED])
    assert_clean(names)


def promoted_member_names_its_cluster(world: World, monkeypatch, path: str) -> None:
    """A promoted object is written with ``source_id=''`` (its evidence cites
    the library's visible promotion source): by the object rule it is an
    in-ceiling member, so a cluster whose only visible members are promoted is
    kept and named by them -- Mixed keeps its name, QZX is still named QZX."""
    if path == "community":
        _drop_comentions(world)
    for suffix in ("m-b", "q-b"):
        world.set_owner(suffix, "")
        assert world.owner(suffix) == ""
    try:
        with world.ceiling():
            names = _names(world, path)
        assert names == (COMENTION_IN_CEILING if path == "comention"
                         else COMMUNITY_IN_CEILING)
        assert_clean(names)
    finally:
        for suffix in ("m-b", "q-b"):
            world.set_owner(suffix, world.open)


def empty_named_member_does_not_drop(world: World, monkeypatch, path: str) -> None:
    """The first in-ceiling member of QZX in member-id order has an empty
    name: it cannot name the cluster, the next in-ceiling member (``QZX``) does,
    and the cluster is not dropped."""
    if path == "community":
        _drop_comentions(world)
    world.set_owner("q-a2", world.open)
    world.set_name("q-a2", "")
    try:
        with world.ceiling():
            names = _names(world, path)
        assert names == (COMENTION_IN_CEILING if path == "comention"
                         else COMMUNITY_IN_CEILING)
    finally:
        world.set_owner("q-a2", world.hidden)
        world.set_name("q-a2", QZX_HIDDEN)


def focal_resolved_once_per_library(world: World, monkeypatch, path: str) -> None:
    """``resolve_comparison_peers`` resolves the focal (stored-name match plus
    R4's member read) once and hands it to both paths: when co-mention comes
    back empty and the community path answers, it is not resolved again."""
    if path == "community":
        _drop_comentions(world)
    with world.ceiling():
        with recorded_statements(world) as statements:
            names, source = world.service.resolve_comparison_peers(
                world.nb, FOCAL, "compare", top_k=8, candidates=16)
    assert source == path
    assert names == (COMENTION_IN_CEILING if path == "comention"
                     else COMMUNITY_IN_CEILING)
    assert len(stored_name_resolutions(statements)) == 1, statements
    assert len(focal_reads(statements, FOCAL_ID)) == 1, statements


def focal_reads_are_bounded_pages(world: World, monkeypatch, path: str) -> None:
    """R4 walks the focal cluster's members a bounded page at a time. PGW's
    stored name is the hidden spelling of its nine hidden members, the tenth
    member ``p-b`` is the visible ``PGW``: the first page (8 members) holds no
    in-ceiling member spelled as the key, the next page (16, after the last
    member walked) holds the remaining two and is short -- the focal is
    unresolved. Once ``p-b`` is spelled as the key it is found on that second
    page and the focal resolves; either way two bounded pages."""
    if path == "community":
        _drop_comentions(world)
    with world.ceiling():
        with recorded_statements(world) as unresolved:
            assert _names(world, path, focal=PGW_HIDDEN) == []
    assert focal_read_limits(unresolved, "K-pagedgammawidget") == [8, 16]
    world.set_name("p-b", PGW_HIDDEN)
    try:
        with world.ceiling():
            with recorded_statements(world) as resolved:
                names = _names(world, path, focal=PGW_HIDDEN)
        assert names == ([FOCAL] if path == "comention"
                         else [MIXED, ABG_LONG, FOCAL, QZX])
        assert focal_read_limits(resolved, "K-pagedgammawidget") == [8, 16]
    finally:
        world.set_name("p-b", PGW)


def non_ascii_case_variant_focal(world: World, monkeypatch, path: str) -> None:
    """Both backends give one answer for a non-ASCII case variant. The key of
    ``Ärger Widget`` is ``ärger widget`` (Unicode lowercase), which is also the
    stored name, so the stored-name match (``lower(canonical_name)``, ASCII-only
    on both backends) resolves it -- and R4 compares member names with the same
    ASCII-only lowering, so the visible ``Ärger Widget`` is not spelled as the
    key and only the hidden ``ärger widget`` is: under a ceiling the focal is
    unresolved; without one it resolves as before."""
    if path == "community":
        _drop_comentions(world)
    with world.ceiling():
        assert _names(world, path, focal=ARGER) == []
    assert _names(world, path, focal=ARGER) == [MIXED]


def _focal_unresolved_events(world: World, focal: str) -> list[dict]:
    return [event for event in world.events
            if event == {"kind": "community_unavailable", "notebook_id": world.nb,
                         "reason": "focal_unresolved", "focal": focal}]


def hidden_spelling_focal_is_unresolved(world: World, monkeypatch, path: str) -> None:
    """R4: the stored name of QZX is the hidden spelling, so the stored-name
    match resolves it -- but no in-ceiling member is spelled that way, so under
    a ceiling the focal is unresolved: no peers on either path, and the
    community path's existing ``focal_unresolved`` event. Without a ceiling
    the spelling resolves exactly as before."""
    if path == "community":
        _drop_comentions(world)
    before = len(_focal_unresolved_events(world, QZX_HIDDEN))
    with world.ceiling():
        assert _names(world, path, focal=QZX_HIDDEN) == []
        assert world.service.resolve_comparison_peers(
            world.nb, QZX_HIDDEN, "compare", top_k=8, candidates=16,
        ) == ([], "community")
    unresolved = _focal_unresolved_events(world, QZX_HIDDEN)
    # community path: its own call + resolve's fallback; co-mention: resolve's
    assert len(unresolved) - before == (2 if path == "community" else 1)
    if path == "comention":
        assert _names(world, path, focal=QZX_HIDDEN) == [FOCAL]
    else:
        assert _names(world, path, focal=QZX_HIDDEN) == [
            HIDDEN_ONLY, GHOST, MIXED, ABG_LONG, PGW_HIDDEN, FOCAL]


def focal_carried_in_ceiling_resolves(world: World, monkeypatch, path: str) -> None:
    """R4: ABG's stored name (its long form) is also the spelling of the
    in-ceiling member ``a-b``, so it resolves under a ceiling as before; a
    promoted carrier (``source_id=''``) counts the same; once the only
    in-ceiling carrier is re-owned by the hidden source the focal is
    unresolved."""
    if path == "community":
        _drop_comentions(world)
    expected = ([FOCAL] if path == "comention" else [MIXED, PGW, FOCAL, QZX])
    try:
        with world.ceiling():
            assert _names(world, path, focal=ABG_LONG) == expected
            world.set_owner("a-b", "")
            assert _names(world, path, focal=ABG_LONG) == expected
            world.set_owner("a-b", world.hidden)
            assert _names(world, path, focal=ABG_LONG) == []
        assert _names(world, path, focal=ABG_LONG)  # no ceiling: resolves
    finally:
        world.set_owner("a-b", world.open)


def chunk_sub_queries_carry_no_hidden_spelling(world: World, monkeypatch, path: str) -> None:
    """Chunk mode: the comparison peers appended to ``sub_queries`` (they are
    sent to retrieval, embedding and the model) are the R3 names."""
    if path == "community":
        _drop_comentions(world)
    queries = _chunk_sub_queries(world, monkeypatch)
    assert queries[0] == FOCAL
    expected = COMENTION_IN_CEILING if path == "comention" else COMMUNITY_IN_CEILING
    assert queries[1:] == expected
    assert_clean(queries)


def reasoning_trace_carries_no_hidden_spelling(world: World, monkeypatch, path: str) -> None:
    """Reasoning mode, really run: the ``expand_community`` step's peers, the
    queries actually issued, ``used_queries`` and the whole trace hold the R3
    names only; so do the events of the run."""
    if path == "community":
        _drop_comentions(world)
    result, issued, events = _reasoning_run(world, monkeypatch)
    steps = [step.model_dump() for step in result.trace]
    expand = [step for step in steps if step["step_type"] == "expand_community"]
    assert len(expand) == 1, steps
    expected = COMENTION_IN_CEILING if path == "comention" else COMMUNITY_IN_CEILING
    assert expand[0]["detail"]["peers"] == expected
    assert expand[0]["detail"]["source"] == path
    assert set(expected) <= set(issued)
    assert_clean(expand)
    assert_clean(steps)
    assert_clean(issued)
    assert_clean([entry["query"] for entry in result.attempted])
    assert_clean(events, expect_short=False)


# ----------------------------------------------------------------- consumers
def _chunk_sub_queries(world: World, monkeypatch) -> list[str]:
    """``AskService._chunk_retrieve`` up to the plan: the sub-queries it is
    about to retrieve with (the rest of the chunk engine is not under test)."""

    class _Rewrite:
        configured = True
        model = "fake"

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({"query": FOCAL, "sub_queries": [{"query": FOCAL}],
                               "comparison": {"focal": FOCAL}})

    class _Stop(Exception):
        pass

    planned: list[list[str]] = []

    def _plan(notebook_id, queries):
        planned.append(list(queries))
        raise _Stop

    bind_chat_client(world.repo, "query_rewrite", _Rewrite())
    ask = world.repo._runtime.ask_service()
    # The library dimension is not under test: this one library is mounted.
    monkeypatch.setattr(CommunityQueryService, "mounted_base_ids",
                        lambda self, notebook_id: [world.nb])
    monkeypatch.setattr(ask.candidates, "chunk_plan", _plan)
    with world.ceiling():
        try:
            ask._chunk_retrieve(world.nb, QUESTION, "", "", cancel_event=None,
                                ask_stage=lambda *args, **kwargs: None)
        except _Stop:
            pass
    assert len(planned) == 1, planned
    return planned[0]


def _reasoning_run(world: World, monkeypatch):
    from app.services.reasoning_retrieval import ReasoningRetriever

    class _SeqLLM:
        configured = True
        model = "fake"

        def __init__(self):
            self._reflects = [
                {"next_action": "expand_community", "query": FOCAL,
                 "reason": "需要同类"},
                {"next_action": "answer", "sufficient": True},
            ]

        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": FOCAL}]})
            if self._reflects:
                return json.dumps(self._reflects.pop(0))
            return json.dumps({"next_action": "answer", "sufficient": True})

    events: list[dict] = []
    runtime = world.repo._runtime
    monkeypatch.setattr(runtime.event_log, "emit",
                        lambda event: events.append(dict(event)))
    bind_chat_client(world.repo, "reasoning_agent", _SeqLLM())
    retriever = ReasoningRetriever.from_repository(world.repo, world.repo.settings)
    monkeypatch.setattr(retriever.communities, "mounted_base_ids",
                        lambda *args: [world.nb])
    issued: list[str] = []
    real_search = retriever.search

    def _search(notebook_id, query, *args, **kwargs):
        issued.append(query)
        return real_search(notebook_id, query, *args, **kwargs)

    monkeypatch.setattr(retriever, "search", _search)
    with world.ceiling():
        result = retriever.run(world.nb, QUESTION, "")
    return result, issued, events


#: In an order one world can serve (see the module docstring): the member
#: deprecation at the end of ``store_member_read_shape`` and the co-mention
#: rows ``ranking_reads_the_in_ceiling_name`` drops come after every scenario
#: that reads them.
SCENARIOS = (
    world_preconditions,
    store_member_read_shape,
    comention_top_k_counts_named_peers,
    deny_ceiling_is_silent,
    ranking_reads_the_in_ceiling_name,
)
#: Each runs once per path: ``comention`` (the world as built) and
#: ``community`` (co-mention rows removed, so the fallback answers). The
#: prune of ``stale_stored_name_is_not_used`` comes last.
PATH_SCENARIOS = (
    hidden_spelling_never_names_a_peer,
    absent_ceiling_reads_no_member,
    one_batched_member_read,
    chunk_sub_queries_carry_no_hidden_spelling,
    reasoning_trace_carries_no_hidden_spelling,
    focal_resolved_once_per_library,
    focal_reads_are_bounded_pages,
    non_ascii_case_variant_focal,
    hidden_spelling_focal_is_unresolved,
    focal_carried_in_ceiling_resolves,
    promoted_member_names_its_cluster,
    empty_named_member_does_not_drop,
    stale_stored_name_is_not_used,
)
PATHS = ("comention", "community")
