"""Shared scenario and assertions for the E4-4 KG store readers (ruling M1).

A shared notebook ``nb``: user A owns it, user B is a member.  One visible
source (``src-s``) and two confirmed Memories — A's (``src-ma``) and B's
(``src-mb``) — each yield concepts, a claim and a relation.  Every object
name carries the token ``kgtoken`` so every search leg matches every object;
only the Memory ownership decides what a viewer may get back.  A second
notebook ``nb2`` holds B's Memory too, so a probe that is not bound to its own
notebook would show up as a wrong answer, not only as a slow plan.

The four viewers of every reader:

* ``None`` — internal callers: every row, statement text unchanged;
* A / B — the shared rows plus the viewer's OWN Memory rows, never the
  other member's;
* ``""`` — the ``memory:read`` channel is closed: no Memory-derived row at
  all, the viewer's own included.

Used by ``test_memory_kg_store_readers.py`` (SQLite) and
``postgres/test_memory_kg_store_readers_pg.py`` (PostgreSQL) — same scenario,
same expectations, each backend's own placeholder and timestamp dialect.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.core.request_context import reset_request_user, set_request_user
from app.domain.knowledge_contracts import USABLE_STATUSES
from app.models.notebooks import NotebookCreate

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)

SHARED_NAMES = {"kgtoken shared alpha", "kgtoken shared beta", "kgtoken shared claim"}
# Deliberately asymmetric (A: two concepts, B: one): every count A may see
# differs from every count B may see, so a cache entry shared between two
# viewers shows up as a wrong number on either backend.
A_NAMES = {"kgtoken private of A", "kgtoken second of A", "kgtoken claim of A"}
B_PRIVATE = "kgtoken bravo private of B"
B_NAMES = {B_PRIVATE, "kgtoken claim of B"}
ALL_NAMES = SHARED_NAMES | A_NAMES | B_NAMES


class Dialect:
    def __init__(self, postgres: bool) -> None:
        self.postgres = postgres

    def sql(self, text: str) -> str:
        return text.replace("?", "%s") if self.postgres else text

    def ts(self):
        return T0 if self.postgres else T0.isoformat()


def _source(dialect, db, nb, sid, *, memory_id=None):
    db.execute(
        dialect.sql(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,memory_id,created_at,updated_at) "
            "VALUES (?,?,?,?,'extracted','parsed','d.md',?,?,?)"
        ),
        (sid, nb, sid, "memory" if memory_id else "markdown", memory_id,
         dialect.ts(), dialect.ts()),
    )
    db.execute(
        dialect.sql(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,created_at) VALUES (?,?,'paragraph','p',?,?)"
        ),
        (f"el-{sid}", sid, f"text of {sid}", dialect.ts()),
    )


def _memory(dialect, db, nb, memory_id, owner):
    db.execute(
        dialect.sql(
            "INSERT INTO memory_items (id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) VALUES (?,?,?,'ask_answer','confirmed',"
            "'t','c',?,?)"
        ),
        (memory_id, nb, owner, dialect.ts(), dialect.ts()),
    )


def _ev(sid):
    return {"source_id": sid, "source_title": sid, "element_id": f"el-{sid}",
            "element_type": "paragraph", "location_label": "p",
            "quoted_span": f"quote {sid}", "confidence": 1.0}


def _objects(sid, concept_names, claim_name):
    items = [
        {"local_id": f"c{i}", "object_type": "concept",
         "payload": {"name": name, "section_path": "1"}, "evidence": [_ev(sid)]}
        for i, name in enumerate(sorted(concept_names))
    ]
    items.append({"local_id": "claim", "object_type": "claim",
                  "payload": {"name": claim_name, "section_path": "1"},
                  "evidence": [_ev(sid)]})
    return items


def build_world(repo, *, postgres: bool) -> SimpleNamespace:
    dialect = Dialect(postgres)
    a = repo.create_user("a00000001", "pw123456")
    b = repo.create_user("b00000002", "pw123456")
    token = set_request_user(a)
    try:
        nb = repo.create_notebook(NotebookCreate(name="shared")).id
        repo.add_member(nb, b.id)
    finally:
        reset_request_user(token)
    token = set_request_user(b)
    try:
        nb2 = repo.create_notebook(NotebookCreate(name="b-own")).id
    finally:
        reset_request_user(token)
    database = repo._runtime.database
    with database.write() as db:
        _source(dialect, db, nb, "src-s")
        _memory(dialect, db, nb, "mem-a", a.id)
        _source(dialect, db, nb, "src-ma", memory_id="mem-a")
        _memory(dialect, db, nb, "mem-b", b.id)
        _source(dialect, db, nb, "src-mb", memory_id="mem-b")
        _memory(dialect, db, nb2, "mem-b2", b.id)
        _source(dialect, db, nb2, "src-mb2", memory_id="mem-b2")
    def relation(sid):
        return [{"source_local_id": "c0", "target_local_id": "claim",
                 "edge_type": "depends_on", "evidence": [_ev(sid)]}]

    repo.store_kg(nb, "src-s", _objects(
        "src-s", {"kgtoken shared alpha", "kgtoken shared beta"}, "kgtoken shared claim"),
        relation("src-s"))
    repo.store_kg(nb, "src-ma", _objects(
        "src-ma", {"kgtoken private of A", "kgtoken second of A"}, "kgtoken claim of A"),
        relation("src-ma"))
    repo.store_kg(nb, "src-mb", _objects(
        "src-mb", {B_PRIVATE}, "kgtoken claim of B"), relation("src-mb"))
    repo.store_kg(nb2, "src-mb2", _objects(
        "src-mb2", {"kgtoken private of B elsewhere"}, "kgtoken claim elsewhere"),
        relation("src-mb2"))
    with database.connect() as db:
        rows = db.execute(
            dialect.sql("SELECT id, source_id, payload FROM knowledge_objects WHERE notebook_id=?"),
            (nb,),
        ).fetchall()
    names = {}
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            import json
            payload = json.loads(payload or "{}")
        names[row["id"]] = payload.get("name", "")
    ids_by_name = {name: oid for oid, name in names.items()}
    with database.connect() as db:
        nb2_ids = sorted(
            str(row["id"]) for row in db.execute(
                dialect.sql("SELECT id FROM knowledge_objects WHERE notebook_id=?"),
                (nb2,),
            ).fetchall()
        )
    return SimpleNamespace(
        repo=repo, dialect=dialect, nb=nb, nb2=nb2, a=a.id, b=b.id,
        names=names, ids_by_name=ids_by_name, nb2_ids=nb2_ids,
        # viewer -> the object names that viewer may read in nb
        expected={
            None: ALL_NAMES,
            a.id: SHARED_NAMES | A_NAMES,
            b.id: SHARED_NAMES | B_NAMES,
            "": SHARED_NAMES,
        },
        # viewer -> the relation source ids that viewer may read
        expected_relation_sources={
            None: {"src-s", "src-ma", "src-mb"},
            a.id: {"src-s", "src-ma"},
            b.id: {"src-s", "src-mb"},
            "": {"src-s"},
        },
    )


def viewers(world):
    return (None, world.a, world.b, "")


def store(world):
    return world.repo._runtime.knowledge


def queries(world):
    return world.repo._runtime.queries


def connect(world):
    return world.repo._runtime.database.connect()


def invalidate_counts(world):
    world.repo._runtime.queries.invalidate_knowledge_counts(world.nb)


def _name_set(world, ids):
    return {world.names[str(i)] for i in ids if str(i) in world.names}


class Recorder:
    """A connection proxy that records every statement text, then delegates."""

    def __init__(self, db) -> None:
        self._db = db
        self.statements: list[str] = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        return self._db.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._db, name)


# --------------------------------------------------------------------- readers
def check_list_page_and_total(world) -> None:
    """``list_knowledge_page``: the page rows and the total are the same view,
    for every viewer, with and without a status filter."""
    for viewer in viewers(world):
        invalidate_counts(world)
        expected = {n for n in world.expected[viewer] if "claim" not in n}
        with connect(world) as db:
            total, objects = store(world).list_knowledge_page(
                db, world.nb, "concept", None, 0, 50, viewer_id=viewer)
            status = objects[0]["status"]
            s_total, s_objects = store(world).list_knowledge_page(
                db, world.nb, "concept", status, 0, 50, viewer_id=viewer)
        got = {o["payload"]["name"] for o in objects}
        assert got == expected, (viewer, got)
        assert total == len(expected), (viewer, total)
        assert {o["payload"]["name"] for o in s_objects} == expected, viewer
        assert s_total == len(expected), (viewer, s_total)


def _split(names):
    return {"concept": len([n for n in names if "claim" not in n]),
            "claim": len([n for n in names if "claim" in n])}


def check_counts(world) -> None:
    """``type_counts`` / ``count_active_objects`` / the board's
    ``knowledge_type_count_rows`` / ``notebook_analytics``: ``None`` = every
    object (today), ``""`` = shared, a user id = shared + that user's own."""
    for viewer in viewers(world):
        expected = _split(world.expected[viewer])
        board_expected = expected
        with connect(world) as db:
            counts, _labels = store(world).type_counts(db, world.nb, viewer_id=viewer)
            active = store(world).count_active_objects(db, world.nb, viewer_id=viewer)
            board = {
                row["object_type"]: int(row["c"])
                for row in queries(world).knowledge_type_count_rows(
                    db, world.nb, USABLE_STATUSES, viewer_id=viewer)
            }
        assert counts == expected, (viewer, counts)
        assert active == sum(expected.values()), (viewer, active)
        assert board == board_expected, (viewer, board)
        analytics = queries(world).notebook_analytics(world.nb, viewer_id=viewer)
        assert analytics.knowledge_counts == board_expected, (
            viewer, analytics.knowledge_counts)
    with connect(world) as db:
        default_board = {
            row["object_type"]: int(row["c"])
            for row in queries(world).knowledge_type_count_rows(
                db, world.nb, USABLE_STATUSES)
        }
    assert default_board == _split(ALL_NAMES)


def check_has_kg(world) -> None:
    """``notebook_has_kg`` (``kg_ready``): ``None`` = any object (today), ``""``
    = a shared object, a user id = a shared object or one of that user's own
    Memory objects.  ``nb2`` holds only B's Memory-derived objects."""
    with connect(world) as db:
        for viewer in viewers(world):
            assert queries(world).notebook_has_kg(db, world.nb, viewer_id=viewer), viewer
        assert queries(world).notebook_has_kg(db, world.nb2) is True
        assert queries(world).notebook_has_kg(db, world.nb2, viewer_id="") is False
        assert queries(world).notebook_has_kg(db, world.nb2, viewer_id=world.a) is False
        assert queries(world).notebook_has_kg(db, world.nb2, viewer_id=world.b) is True
        # A viewer read takes the shared half from the count cache: no
        # notebook-wide EXISTS over knowledge_objects (the probe that had to
        # skip every Memory object of a Memory-only notebook).
        for notebook in (world.nb, world.nb2):
            for viewer in ("", world.a, world.b):
                recorder = Recorder(db)
                queries(world).notebook_has_kg(recorder, notebook, viewer_id=viewer)
                assert not any(
                    " ".join(s.split()).startswith(
                        "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id")
                    for s in recorder.statements
                ), (notebook, viewer, recorder.statements)


def check_counts_cache_holds_no_viewer(world) -> None:
    """The memo is shared across viewers: A reading first must not leak A's
    Memory count into B's answer, and back (the mutation "cache stores the
    viewer's counts" turns this red)."""
    invalidate_counts(world)
    order = (world.a, world.b, "", world.a, None, world.b)
    for viewer in order:
        names = world.expected[viewer]
        with connect(world) as db:
            counts, _ = store(world).type_counts(db, world.nb, viewer_id=viewer)
        assert counts["concept"] == len([n for n in names if "claim" not in n]), viewer


def check_graph_readers(world) -> None:
    for viewer in viewers(world):
        with connect(world) as db:
            nodes = store(world).graph_node_rows(db, world.nb, viewer_id=viewer)
            unified = store(world).unified_graph_rows(db, world.nb, viewer_id=viewer)
            relations = store(world).relations_for_notebook(db, world.nb, viewer_id=viewer)
            meta = store(world).object_meta_rows_for_notebook(
                db, world.nb, list(world.names), viewer_id=viewer)
        assert _name_set(world, [r["id"] for r in nodes]) == world.expected[viewer], viewer
        assert _name_set(world, [r["id"] for r in unified]) == world.expected[viewer], viewer
        assert _name_set(world, [r["id"] for r in meta]) == world.expected[viewer], viewer
        assert {r["source_id"] for r in relations} == (
            world.expected_relation_sources[viewer]), viewer


def check_own_memory_overlay(world) -> None:
    """``own_memory_only``: exactly the viewer's own Memory rows — what the
    service overlays on the viewer-independent viz artifact."""
    with connect(world) as db:
        for viewer, names, sources in (
            (world.a, A_NAMES, {"src-ma"}), (world.b, B_NAMES, {"src-mb"}),
        ):
            rows = store(world).unified_graph_rows(
                db, world.nb, viewer_id=viewer, own_memory_only=True)
            rels = store(world).relations_for_notebook(
                db, world.nb, viewer_id=viewer, own_memory_only=True)
            assert _name_set(world, [r["id"] for r in rows]) == names, viewer
            assert {r["source_id"] for r in rels} == sources, viewer
        assert store(world).unified_graph_rows(
            db, world.nb, viewer_id="", own_memory_only=True) == []
        assert store(world).relations_for_notebook(
            db, world.nb, viewer_id="", own_memory_only=True) == []
        for reader in (store(world).unified_graph_rows, store(world).relations_for_notebook):
            try:
                reader(db, world.nb, own_memory_only=True)
            except ValueError:
                pass
            else:  # pragma: no cover - the assertion is the point
                raise AssertionError("own_memory_only without a viewer must raise")


def check_search_legs(world) -> None:
    """KG search (lexical ``fts_search`` + hit hydration ``object_meta_rows``)
    and the notebook search box's KG leg (``search_notebook``)."""
    for viewer in viewers(world):
        with connect(world) as db:
            hits = store(world).fts_search(db, world.nb, "kgtoken", 30, viewer_id=viewer)
            meta = store(world).object_meta_rows(
                db, list(world.names), notebook_id=world.nb, viewer_id=viewer)
        assert {h["name"] for h in hits} == world.expected[viewer], (viewer, hits)
        assert _name_set(world, [r["id"] for r in meta]) == world.expected[viewer], viewer
        response = queries(world).search_notebook(world.nb, "kgtoken", viewer_id=viewer)
        labels = {h.label for h in response.hits if h.source_id == "" and h.scope not in (
            "Notebook", "Domain")}
        assert labels == world.expected[viewer], (viewer, labels)


def check_neighbour_relations(world) -> None:
    ids = list(world.names)
    for viewer in viewers(world):
        with connect(world) as db:
            rows = store(world).neighbor_relation_rows(db, world.nb, ids, viewer_id=viewer)
        endpoints = {str(r["source_object_id"]) for r in rows} | {
            str(r["target_object_id"]) for r in rows}
        allowed = {world.ids_by_name[n] for n in world.expected[viewer]}
        assert endpoints and endpoints <= allowed, (viewer, _name_set(world, endpoints))
        assert len(rows) == len(world.expected_relation_sources[viewer]), viewer


def check_shared_tooling(world) -> None:
    """Community summary context and duplicate hydration leave every Memory
    row out, whoever asks (shared tooling, no viewer)."""
    ids = list(world.names)
    with connect(world) as db:
        objects, relations = store(world).community_context_rows(db, world.nb, ids)
        members = store(world).duplicate_member_rows(db, world.nb, ids)
    assert _name_set(world, [o["id"] for o in objects]) == SHARED_NAMES
    assert _name_set(world, [m["id"] for m in members]) == SHARED_NAMES
    shared_ids = {world.ids_by_name[n] for n in SHARED_NAMES}
    assert relations and all(
        str(r["source_object_id"]) in shared_ids and str(r["target_object_id"]) in shared_ids
        for r in relations
    )


def check_none_statements_unchanged(world, expected: dict[str, list[str]]) -> None:
    """``viewer_id=None``: each reader issues exactly the pre-isolation
    statement text (``expected`` maps reader name -> statements)."""
    ids = sorted(world.names)
    calls = {
        "graph_node_rows": lambda db: store(world).graph_node_rows(db, world.nb),
        "unified_graph_rows": lambda db: store(world).unified_graph_rows(db, world.nb),
        "relations_for_notebook": lambda db: store(world).relations_for_notebook(db, world.nb),
        "neighbor_relation_rows": lambda db: store(world).neighbor_relation_rows(
            db, world.nb, ids[:1]),
        "object_meta_rows_for_notebook": lambda db: store(world).object_meta_rows_for_notebook(
            db, world.nb, ids[:1]),
        "object_meta_rows": lambda db: store(world).object_meta_rows(db, ids[:1]),
        "list_knowledge_page_rows": lambda db: store(world).list_knowledge_page(
            db, world.nb, "concept", None, 0, 5),
        "fts_search": lambda db: store(world).fts_search(db, world.nb, "kgtoken", 5),
        "notebook_has_kg": lambda db: queries(world).notebook_has_kg(db, world.nb),
    }
    for name, call in calls.items():
        with connect(world) as db:
            recorder = Recorder(db)
            call(recorder)
        statements = recorder.statements
        if name == "list_knowledge_page_rows":
            statements = [s for s in statements if s.startswith("SELECT * ")]
        if callable(expected[name]):
            assert expected[name](statements), (name, statements)
        else:
            assert statements == expected[name], (name, statements)


# ------------------------------------------------------------ review round 1
_PRE_ISOLATION_COUNT_SQL = (
    "SELECT object_type, status, COUNT(*) AS c FROM knowledge_objects "
    "WHERE notebook_id=? GROUP BY object_type, status"
)


def check_none_counts_zero_live_reads(world) -> None:
    """Count readers with ``viewer_id=None``: the pre-isolation numbers, from
    ONE cold statement per ``(epoch, seq)`` and zero live reads after it — the
    warm path issues only the seq read, as before the isolation."""
    invalidate_counts(world)
    with connect(world) as db:
        recorder = Recorder(db)
        cold, _ = store(world).type_counts(recorder, world.nb)
        cold_statements = list(recorder.statements)
        recorder.statements.clear()
        warm, _ = store(world).type_counts(recorder, world.nb)
        active = store(world).count_active_objects(recorder, world.nb)
        board = {
            row["object_type"]: int(row["c"])
            for row in queries(world).knowledge_type_count_rows(
                recorder, world.nb, USABLE_STATUSES)
        }
        total, _rows = store(world).list_knowledge_page(
            recorder, world.nb, "concept", None, 0, 5)
        warm_statements = list(recorder.statements)
        before = {}
        for row in db.execute(
            world.dialect.sql(_PRE_ISOLATION_COUNT_SQL), (world.nb,)
        ).fetchall():
            if row["status"] != "deprecated":
                before[row["object_type"]] = before.get(row["object_type"], 0) + int(row["c"])
    assert cold == warm == board == before == _split(ALL_NAMES), (cold, warm, board, before)
    assert active == len(ALL_NAMES)
    assert total == _split(ALL_NAMES)["concept"]
    assert len(cold_statements) == 2 and "kg_mutation_seq" in cold_statements[0], cold_statements
    assert cold_statements[1].startswith("WITH total AS"), cold_statements
    live = [s for s in warm_statements if "kg_mutation_seq" not in s]
    assert len(live) == 1 and live[0].startswith("SELECT * FROM knowledge_objects"), live


def check_fts_filters_before_the_limit(world) -> None:
    """``k=1``: another member's object that ranks FIRST without the filter
    takes no slot — the viewer still gets one hit (the filter sits before
    the limit, SQLite in the MATCH statement, PostgreSQL in the candidate
    arms)."""
    query = "kgtoken bravo"
    with connect(world) as db:
        unfiltered = store(world).fts_search(db, world.nb, query, 1)
        filtered = store(world).fts_search(db, world.nb, query, 1, viewer_id=world.a)
    assert [hit["name"] for hit in unfiltered] == [B_PRIVATE]  # control
    assert len(filtered) == 1 and filtered[0]["name"] not in B_NAMES, filtered


def check_fts_ceiling_and_viewer(world) -> None:
    """A source ceiling that still lists B's Memory source: only the viewer
    filter keeps B's objects out — both ceiling forms (reverse index and
    authoritative evidence)."""
    ceiling = ["src-s", "src-ma", "src-mb"]
    with connect(world) as db:
        for authoritative in (False, True):
            unfiltered = store(world).fts_search(
                db, world.nb, "kgtoken", 30, allowed_source_ids=ceiling,
                authoritative_source_filter=authoritative)
            hits = store(world).fts_search(
                db, world.nb, "kgtoken", 30, allowed_source_ids=ceiling,
                authoritative_source_filter=authoritative, viewer_id=world.a)
            assert {h["name"] for h in unfiltered} >= B_NAMES, authoritative  # control
            assert {h["name"] for h in hits} == SHARED_NAMES | A_NAMES, (authoritative, hits)


def check_object_meta_rows_binding(world) -> None:
    """The viewer form needs its notebook, and is bound to it: B's own objects
    of another notebook (``nb2``) are not hydrated for ``nb``."""
    with connect(world) as db:
        try:
            store(world).object_meta_rows(db, list(world.names), viewer_id=world.a)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("a viewer-scoped hydration without its notebook must raise")
        elsewhere = store(world).object_meta_rows(
            db, world.nb2_ids, notebook_id=world.nb2, viewer_id=world.b)
        bound = store(world).object_meta_rows(
            db, world.nb2_ids, notebook_id=world.nb, viewer_id=world.b)
    assert len(elsewhere) == len(world.nb2_ids)  # control: B may read them in nb2
    assert bound == []


def check_edge_centrality_is_shared(world) -> None:
    """The edge-centrality source rows (one process-wide cache ranking the
    edge-review queue for everyone) never hold a Memory-derived object or
    relation, unbounded or top-K bounded."""
    shared_ids = {world.ids_by_name[n] for n in SHARED_NAMES}
    with connect(world) as db:
        for max_nodes in (100, 1):
            nodes, relations = store(world).edge_centrality_source_rows(
                db, world.nb, max_nodes)
            node_ids = {str(n["id"] if isinstance(n, dict) else n) for n in nodes}
            assert node_ids and node_ids <= shared_ids, (max_nodes, _name_set(world, node_ids))
            for relation in relations:
                assert str(relation["source_object_id"]) in shared_ids, max_nodes
                assert str(relation["target_object_id"]) in shared_ids, max_nodes
        full_nodes, full_relations = store(world).edge_centrality_source_rows(
            db, world.nb, 100)
    assert _name_set(world, [n["id"] for n in full_nodes]) == SHARED_NAMES
    assert len(full_relations) == 1


def check_object_evidence_rows(world) -> None:
    """``with_evidence=False`` reads ``(id, source_id)`` only; the default
    reads what it always read, with the same statement."""
    ids = sorted(world.names)[:3]
    with connect(world) as db:
        recorder = Recorder(db)
        full = store(world).object_evidence_rows(recorder, ids)
        default_statements = list(recorder.statements)
        recorder.statements.clear()
        slim = store(world).object_evidence_rows(recorder, ids, with_evidence=False)
        slim_statements = list(recorder.statements)
    assert default_statements == [world.dialect.sql(
        "SELECT id, evidence, source_id FROM knowledge_objects WHERE id IN (?,?,?)")]
    assert slim_statements == [world.dialect.sql(
        "SELECT id, source_id FROM knowledge_objects WHERE id IN (?,?,?)")]
    assert {r["id"]: r["source_id"] for r in full} == {r["id"]: r["source_id"] for r in slim}
    assert all("evidence" in dict(r) for r in full)
    assert all(set(dict(r)) == {"id", "source_id"} for r in slim)


def check_enrich_evidence_sources_only(world) -> None:
    """``_enrich_evidence(sources_only=True)`` resolves each element's
    ``source_id`` without reading the element text; ``element_text`` is left
    empty and ``element_type``/``location_label`` are not added from the
    element (here the stored evidence already carries them, so the key sets
    match); the default statement is unchanged."""
    evidence = [_ev("src-s"), _ev("src-mb"), {"source_id": "x", "element_id": "el-gone",
                                               "quoted_span": "q", "source_title": "t"}]
    knowledge = store(world)
    with connect(world) as db:
        recorder = Recorder(db)
        full = knowledge._enrich_evidence(recorder, evidence)
        default_statements = list(recorder.statements)
        recorder.statements.clear()
        slim = knowledge._enrich_evidence(recorder, evidence, sources_only=True)
        slim_statements = list(recorder.statements)
    marks = world.dialect.sql("?,?,?")
    assert default_statements == [
        "SELECT id, source_id, element_type, location_label, text "
        f"FROM source_elements WHERE id IN ({marks})"]
    assert slim_statements == [f"SELECT id, source_id FROM source_elements WHERE id IN ({marks})"]
    assert [set(item) for item in slim] == [set(item) for item in full]
    assert [item["source_id"] for item in slim] == [item["source_id"] for item in full]
    assert [item["element_id"] for item in slim] == [item["element_id"] for item in full]
    assert all(item["element_text"] == "" for item in slim)
    assert full[0]["element_text"] == "text of src-s"


# ------------------------------------------------ stale-index readers (corr. 8)
def ceiling_for_a(world) -> dict:
    """A's frozen all-selected ceiling: visible sources + A's own Memory."""
    return {
        "mode": "include",
        "source_ids": ["src-s", "src-ma"],
        "narrowed": False,
        "hidden_source_ids": ["src-ma"],
        "owner_id": world.a,
    }


def _stale_index():
    return SimpleNamespace(ann_labels=["stale"], relation_ann_labels=["stale"],
                           manifest={"dim": 16})


def check_stale_kg_ann_label(world, monkeypatch) -> None:
    from app.services.source_scope import source_scope_context

    candidates = world.repo.retrieval.candidates
    foreign = world.ids_by_name[B_PRIVATE]
    shared = world.ids_by_name["kgtoken shared alpha"]
    monkeypatch.setattr(candidates, "_scale_index", lambda *_a, **_k: _stale_index())
    monkeypatch.setattr(
        candidates, "_kg_object_candidates",
        lambda *_a, **_k: {foreign: 0.99, shared: 0.5},
    )
    unscoped = world.repo.retrieval.retrieve_scored(world.nb, "kgtoken private")
    assert foreign in {hit.object_id for hit in unscoped}  # control
    with source_scope_context(world.nb, ceiling_for_a(world)):
        scoped = world.repo.retrieval.retrieve_scored(world.nb, "kgtoken private")
    got = _name_set(world, [hit.object_id for hit in scoped])
    assert not got & B_NAMES, got
    assert "kgtoken shared alpha" in got


def check_stale_relation_ann_label(world, monkeypatch) -> None:
    from app.services.source_scope import filter_retrieval_items, source_scope_context

    candidates = world.repo.retrieval.candidates
    with connect(world) as db:
        relations = store(world).relations_for_notebook(db, world.nb)
    by_source = {r["source_id"]: r["id"] for r in relations}
    monkeypatch.setattr(candidates, "_scale_index", lambda *_a, **_k: _stale_index())
    monkeypatch.setattr(
        candidates, "_relation_ann_candidates",
        lambda *_a, **_k: {by_source["src-mb"]: 0.99, by_source["src-s"]: 0.5},
    )
    unscoped = world.repo._retrieve_relations_scored(world.nb, B_PRIVATE)
    assert by_source["src-mb"] in {hit.relation_id for hit in unscoped}  # control
    with source_scope_context(world.nb, ceiling_for_a(world)):
        hits = world.repo._retrieve_relations_scored(world.nb, B_PRIVATE)
        kept = filter_retrieval_items(world.nb, "relation", hits)
    assert by_source["src-mb"] not in {hit.relation_id for hit in kept}


def check_stale_completion_candidates(world) -> None:
    ids = [world.ids_by_name[n] for n in sorted(ALL_NAMES)]
    with connect(world) as db:
        rows = store(world).completion_candidate_rows(db, world.nb, "src-s", ids)
    assert _name_set(world, [r["id"] for r in rows]) == SHARED_NAMES
