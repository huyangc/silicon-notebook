"""Shared fixture for the M1 artifact-isolation tests (E4-6).

One notebook with a shared source and a member's Memory source, both holding
knowledge objects, plus the three relation shapes the isolation must tell apart:

* ``shared -> shared`` supported by a shared relation only           (kept);
* ``shared -> shared`` supported by a Memory relation only           (dropped);
* ``shared -> shared`` supported by BOTH a shared and a Memory relation (kept,
  on the shared relation alone).

``SECRET_NAMES`` are the Memory-derived names no shared artifact may contain;
concepts appear in a viz artifact under their canonical id (``canonical``).
Backend-neutral: the statements are written with ``?`` placeholders and
rewritten for the PostgreSQL repository, so the SQLite tests and their
PostgreSQL twins seed the very same shape.
"""
from __future__ import annotations

import itertools
from contextlib import contextmanager
import json
import os

from app.models.schemas import NotebookCreate

import struct

NOW = "2026-09-29T00:00:00"
SHARED_SOURCE = "src-shared"
MEMORY_SOURCE = "src-memory"
SECRET_NAMES = ("SECRET-ALPHA", "SECRET-BETA")


def is_postgres(repo) -> bool:
    return type(repo).__name__ == "PostgresRepository"


def sql(repo, statement: str) -> str:
    """``?`` placeholders -> the repository's paramstyle."""
    return statement.replace("?", "%s") if is_postgres(repo) else statement


def _object(local_id: str, name: str) -> dict:
    return {
        "local_id": local_id,
        "object_type": "concept",
        "payload": {"name": name, "section_path": ""},
        "evidence": [],
    }


def _object_ids_by_name(repo, notebook_id: str) -> dict:
    name = "payload ->> 'name'" if is_postgres(repo) else "json_extract(payload,'$.name')"
    with repo._connect() as db:
        rows = db.execute(
            sql(repo, f"SELECT id, {name} AS name "
                      "FROM knowledge_objects WHERE notebook_id=?"),
            (notebook_id,),
        ).fetchall()
    return {row["name"]: row["id"] for row in rows}


def _relation_id(repo, notebook_id: str, source_object_id: str,
                 target_object_id: str, edge_type: str, source_id: str) -> str:
    with repo._connect() as db:
        row = db.execute(
            sql(repo, "SELECT id FROM knowledge_relations WHERE notebook_id=? AND "
                      "source_object_id=? AND target_object_id=? AND edge_type=? "
                      "AND source_id=?"),
            (notebook_id, source_object_id, target_object_id, edge_type,
             source_id),
        ).fetchone()
    return row["id"]


def make_it_look_pre_isolation(root: str) -> None:
    """Rewrite the manifest under ``root`` into what the release before the M1
    isolation wrote: no ``memory_isolation`` / ``memory_sources_digest`` fields,
    and no isolation pair in its version list."""
    path = os.path.join(str(root), "manifest.json")
    with open(path) as handle:
        manifest = json.load(handle)
    manifest.pop("memory_isolation", None)
    manifest.pop("memory_sources_digest", None)
    version = manifest.get("version") or []
    if "memory_isolation" in version:
        at = version.index("memory_isolation")
        del version[at:at + 2]
    with open(path, "w") as handle:
        json.dump(manifest, handle)


def decoded_artifact_bytes(root: str) -> bytes:
    """Every file under ``root`` as bytes, compressed ``.npz`` members unpacked so
    a name or id stored inside one is visible to a substring check."""
    import numpy as np

    blob = b""
    for directory, _dirs, files in os.walk(root):
        for name in sorted(files):
            path = os.path.join(directory, name)
            if name.endswith(".npz"):
                with np.load(path, allow_pickle=True) as archive:
                    for key in archive.files:
                        blob += key.encode() + np.asarray(archive[key]).tobytes()
                        blob += repr(archive[key].tolist()).encode()
            elif name.endswith(".npy"):
                blob += repr(np.load(path, allow_pickle=True).tolist()).encode()
            else:
                with open(path, "rb") as handle:
                    blob += handle.read()
    return blob


def assert_a_memory_vector_takes_no_synonym_slot(repo, monkeypatch) -> None:
    """The synonym KNN keeps each node's top-k neighbours. A Memory object whose
    vector sits between two shared objects is the top-1 neighbour of both; if
    its row were only dropped AFTER the KNN (as an edge), it would still have
    taken the slot and the shared pair would lose its edge. Dropped BEFORE the
    KNN, the shared pair keeps it."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id, ids = seeded["notebook_id"], seeded["objects"]
    planes = {"left": (1.0, 0.0), "right": (1.0, 0.2), SECRET_NAMES[0]: (1.0, 0.1)}
    others = [name for name in ids if name not in planes]
    with repo._write() as db:
        for axis, name in enumerate(list(planes) + others):
            head = planes.get(name)
            values = [0.0] * 16
            if head is not None:
                values[0], values[1] = head
            else:
                values[2 + axis] = 1.0  # orthogonal to everything else
            db.execute(
                sql(repo, "INSERT INTO knowledge_embeddings (object_id,notebook_id,"
                          "vector,created_at) VALUES (?,?,?,?) ON CONFLICT (object_id) "
                          "DO UPDATE SET vector = EXCLUDED.vector"),
                (ids[name], nb_id, struct.pack("<16f", *values), NOW),
            )
    repo._vector_cache.invalidate(f"{nb_id}:matrix:knowledge_embeddings")
    settings = repo._runtime.index_projections.settings
    monkeypatch.setattr(settings, "ppr_emb_synonym_enabled", True)
    monkeypatch.setattr(settings, "ppr_emb_synonym_threshold", 0.9)
    monkeypatch.setattr(settings, "ppr_emb_synonym_topk", 1)

    _nodes, edges, _chunks, _kg, _counts = repo._runtime.scale_builder.gather_graph(nb_id)
    pair = {ids["left"], ids["right"]}
    assert any({a, b} == pair and w > 0.97 for a, b, w in edges), edges
    assert all(ids[SECRET_NAMES[0]] not in (a, b) for a, b, _w in edges)


def mix_a_shared_object_into_a_memory_cluster(repo, seeded) -> None:
    """Put the shared object ``left`` into one cluster with a Memory object, under
    a canonical id minted from the Memory member's name -- the legacy mixed
    cluster E4-5 deletes and E4-2 stops seeding, which every shared artifact
    still defends against on its own.

    Written row by row, independent of how the rebuild clusters: the shared
    member's row is moved under the canonical id, and the Memory member's row
    in the published generation is (re)inserted -- after E4-2 a rebuild gives
    the Memory object no cluster row at all. A sentinel checks that the mixed
    cluster really exists before any artifact assertion relies on it."""
    nb_id, ids = seeded["notebook_id"], seeded["objects"]
    canonical, memory_member = "K-secret-alpha-cluster", ids[SECRET_NAMES[0]]
    with repo._write() as db:
        state = db.execute(
            sql(repo, "SELECT cluster_generation FROM unified_kg_state "
                      "WHERE notebook_id=?"), (nb_id,),
        ).fetchone()
        generation = int(
            0 if state is None or state["cluster_generation"] is None
            else state["cluster_generation"]
        )
        for member in (ids["left"], memory_member):
            db.execute(
                sql(repo, "DELETE FROM concept_clusters WHERE notebook_id=? "
                          "AND member_object_id=? AND generation=?"),
                (nb_id, member, generation),
            )
            db.execute(
                sql(repo, "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
                          "member_object_id,canonical_name,object_type,created_at,"
                          "generation) VALUES (?,?,?,?,?,'concept',?,?)"),
                (f"cc-legacy-{member}", nb_id, canonical, member,
                 SECRET_NAMES[0], NOW, generation),
            )
        members = {
            row["member_object_id"] for row in db.execute(
                sql(repo, "SELECT member_object_id FROM concept_clusters "
                          "WHERE notebook_id=? AND canonical_id=? AND generation=?"),
                (nb_id, canonical, generation),
            ).fetchall()
        }
    assert members == {ids["left"], memory_member}, members  # the mixed cluster exists
    repo._vector_cache.invalidate(f"{nb_id}:clustermap")


def assert_no_partition_names_a_memory_cluster(repo, seeded) -> None:
    """The source-partition companion names, per shared source, the clusters its
    objects belong to. After a scale build over a mixed cluster, no partition
    names that cluster, while the shared object itself is still partitioned.
    Shared by the SQLite test and its PostgreSQL twin."""
    nb_id, ids = seeded["notebook_id"], seeded["objects"]
    make_the_shared_source_partitionable(repo, seeded)
    mix_a_shared_object_into_a_memory_cluster(repo, seeded)
    manifest = repo.build_scale_index(nb_id)
    root = str(repo._runtime.scale_artifacts.artifacts.source_partition_dir(nb_id))
    with open(os.path.join(root, "manifest.json")) as handle:
        assert json.load(handle)["published_sources"] == 1, manifest
    blob = decoded_artifact_bytes(root).lower()
    assert ids["left"].lower().encode() in blob  # the shared object is partitioned
    assert b"secret" not in blob


def make_the_shared_source_partitionable(repo, seeded) -> None:
    """Give the shared source what the offline source-partition build requires:
    a completed KG run naming its object count, one source-index row and one
    source fact per object at that generation, and the notebook's source index
    marked backfilled."""
    nb_id, ids = seeded["notebook_id"], seeded["objects"]
    shared = ("MOSFET", "gain", "bias", "left", "right")
    with repo._write() as db:
        db.execute(
            sql(repo, "INSERT INTO extraction_runs (id,notebook_id,source_id,run_type,"
                      "status,error_message,created_at,updated_at) "
                      "VALUES (?,?,?,'kg','completed',?,?,?)"),
            ("run-shared", nb_id, SHARED_SOURCE,
             f"kg objects={len(shared)} relations=2", NOW, NOW),
        )
        for name in shared:
            db.execute(
                sql(repo, "INSERT INTO knowledge_object_sources (object_id,"
                          "source_id,notebook_id) VALUES (?,?,?)"),
                (ids[name], SHARED_SOURCE, nb_id),
            )
            db.execute(
                sql(repo, "INSERT INTO knowledge_source_facts (id,notebook_id,"
                          "source_id,source_generation,local_object_id,"
                          "global_object_id,object_type,payload,evidence,"
                          "projection_version,created_at,updated_at) "
                          "VALUES (?,?,?,?,?,?,'concept','{}','[]',1,?,?)"),
                (f"fact-{name}", nb_id, SHARED_SOURCE, "run-shared", name,
                 ids[name], NOW, NOW),
            )
        db.execute(
            sql(repo, "UPDATE unified_kg_state SET source_index_backfilled=1 "
                      "WHERE notebook_id=?"),
            (nb_id,),
        )


LATE_SECRET = "SECRET-LATE"
_LATE_COUNTER = itertools.count(1)


def seed_notebook_without_memory(repo) -> str:
    """A notebook with one shared source and two objects, no Memory."""
    nb = repo.create_notebook(NotebookCreate(name="no memory yet"))
    with repo._write() as db:
        db.execute(
            sql(repo, "INSERT INTO sources (id,notebook_id,title,source_type,"
                      "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)"),
            (SHARED_SOURCE, nb.id, "shared", "md", "ready", NOW, NOW),
        )
    repo.store_kg(
        nb.id, SHARED_SOURCE, [_object("a", "MOSFET"), _object("b", "gain")],
        [{"source_local_id": "a", "target_local_id": "b",
          "edge_type": "depends_on", "evidence": []}],
    )
    repo.rebuild_unified_kg(nb.id)
    return nb.id


def _bump_kg_seq(repo, db, notebook_id: str) -> None:
    # the KG write of an extraction / delete moves the mutation sequence, as
    # ``store_kg`` / ``delete_source`` do
    db.execute(
        sql(repo, "UPDATE unified_kg_state SET kg_mutation_seq = "
                  "COALESCE(kg_mutation_seq,0) + 1 WHERE notebook_id=?"),
        (notebook_id,),
    )


def confirm_a_memory(repo, notebook_id: str, n: int, *, source_id: str | None = None,
                     relation_between: tuple | None = None) -> dict:
    """What confirming one more memory writes: its Memory source (unless
    ``source_id`` names an existing one -- a re-extraction of that memory), an
    object derived from it (named ``SECRET-LATE-<n>``, id sorting after every
    other object) and that object's vector; with ``relation_between`` also a
    relation of that source between the two given objects, and its vector.
    Direct writes, so the hooks below can run it from inside a store read
    without re-entering the repository."""
    new_source = source_id is None
    source_id = source_id or f"src-late-{notebook_id}-{n}"
    object_id = f"zz-late-memory-{notebook_id}-{n}"
    relation_id = f"zz-late-relation-{notebook_id}-{n}"
    name = f"{LATE_SECRET}-{n}"
    vector = struct.pack("<16f", *([0.5] * 16))
    with repo._write() as db:
        if new_source:
            db.execute(
                sql(repo, "INSERT INTO sources (id,notebook_id,title,source_type,"
                          "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)"),
                (source_id, notebook_id, "memory", "memory", "ready", NOW, NOW),
            )
        db.execute(
            sql(repo, "INSERT INTO knowledge_objects (id,notebook_id,object_type,"
                      "status,owner,payload,evidence,source_id,created_at,"
                      "updated_at) VALUES (?,?,'concept','approved','',?,'[]',?,?,?)"),
            (object_id, notebook_id, json.dumps({"name": name}), source_id, NOW, NOW),
        )
        db.execute(
            sql(repo, "INSERT INTO knowledge_embeddings (object_id,notebook_id,"
                      "vector,created_at) VALUES (?,?,?,?)"),
            (object_id, notebook_id, vector, NOW),
        )
        if relation_between is not None:
            db.execute(
                sql(repo, "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
                          "source_object_id,target_object_id,edge_type,evidence,"
                          "created_at,review_status) VALUES (?,?,?,?,?,?,?,?,?)"),
                (relation_id, notebook_id, source_id, *relation_between,
                 "depends_on", "[]", NOW, "pending"),
            )
            db.execute(
                sql(repo, "INSERT INTO relation_embeddings (relation_id,notebook_id,"
                          "vector,created_at) VALUES (?,?,?,?)"),
                (relation_id, notebook_id, vector, NOW),
            )
        _bump_kg_seq(repo, db, notebook_id)
    return {"source_id": source_id, "object_id": object_id, "name": name,
            "relation_id": relation_id if relation_between is not None else None,
            "new_source": new_source}


def delete_a_memory(repo, notebook_id: str, confirmed: dict) -> None:
    """What deleting that memory writes, in one transaction (as
    ``delete_source`` does): its rows, and its source if it created one."""
    with repo._write() as db:
        if confirmed.get("relation_id"):
            db.execute(sql(repo, "DELETE FROM relation_embeddings WHERE relation_id=?"),
                       (confirmed["relation_id"],))
            db.execute(sql(repo, "DELETE FROM knowledge_relations WHERE id=?"),
                       (confirmed["relation_id"],))
        db.execute(sql(repo, "DELETE FROM knowledge_embeddings WHERE object_id=?"),
                   (confirmed["object_id"],))
        db.execute(sql(repo, "DELETE FROM knowledge_objects WHERE id=?"),
                   (confirmed["object_id"],))
        if confirmed.get("new_source"):
            db.execute(sql(repo, "DELETE FROM sources WHERE id=?"),
                       (confirmed["source_id"],))
        _bump_kg_seq(repo, db, notebook_id)


class _StatementHook:
    """A connection wrapper that runs ``before(statement)`` right before each
    statement is sent -- the narrowest window there is between two reads."""

    def __init__(self, inner, before):
        self._inner, self._before = inner, before

    def execute(self, statement, params=()):
        self._before(statement)
        return self._inner.execute(statement, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def arm_memory_confirmed_mid_build(repo, monkeypatch, notebook_id: str, *,
                                   window: str, times: int = 1,
                                   vanish: bool = False,
                                   existing_source: str | None = None) -> dict:
    """Confirm one more memory (``confirm_a_memory``) INSIDE a running build, up
    to ``times`` times (once per build attempt), in one of three windows:

    * ``"object_read"`` -- immediately before the viz derive sends its object
      read (the notebook's FIRST Memory, when it had none); with ``vanish`` the
      memory is deleted again right after that read, before the relation read
      -- a Memory the build's own snapshots never see;
    * ``"ann_feed"`` -- right after the KG ANN feed has read its Memory
      object-id set and before it streams the vectors;
    * ``"relation_feed"`` -- the same for the relation ANN feed; the memory
      also gets a relation (between the two shared objects of the fixture).

    For the two feed windows, ``vanish`` deletes the memory again at the next
    stage -- right before the gather (``ann_feed``) or the viz derive
    (``relation_feed``) -- i.e. AFTER the build's observation that follows
    the feed and BEFORE its pre-publish snapshot: only the running union of
    what the build observed still knows the row its ANN index holds.

    ``existing_source`` confirms the object (and relation) under that existing
    Memory source instead of a new one -- a re-extraction.

    Returns ``{"confirmed": [...], "events": [...]}``, both filled as the build
    runs; ``events`` collects every event the builder emits from here on.
    """
    projections = repo._runtime.index_projections
    builder = repo._runtime.scale_builder
    state = {"armed": False, "done": 0, "pending_delete": None}
    confirmed: list = []
    ids = _object_ids_by_name(repo, notebook_id)

    def confirm_now() -> None:
        if state["armed"] and state["done"] < times:
            state["armed"] = False
            state["done"] += 1
            relation = (
                (ids["MOSFET"], ids["gain"]) if window == "relation_feed" else None
            )
            confirmed.append(confirm_a_memory(
                repo, notebook_id, next(_LATE_COUNTER),
                source_id=existing_source, relation_between=relation,
            ))
            if vanish:
                state["pending_delete"] = confirmed[-1]

    if window == "object_read":
        real_derive = builder._derive_object_graph_lite

        def before(statement: str) -> None:
            if "FROM knowledge_objects" in statement and "AS name" in statement:
                confirm_now()
            elif ("FROM knowledge_relations" in statement
                  and state["pending_delete"] is not None):
                delete_a_memory(repo, notebook_id, state["pending_delete"])
                state["pending_delete"] = None

        def derive(nb):
            outer = projections.connect  # whatever is in place right now

            @contextmanager
            def connect():
                with outer() as db:
                    yield _StatementHook(db, before)

            state["armed"] = nb == notebook_id
            projections.connect = connect
            try:
                return real_derive(nb)
            finally:
                state["armed"] = False
                projections.connect = outer

        monkeypatch.setattr(builder, "_derive_object_graph_lite", derive, raising=False)
    elif window in ("ann_feed", "relation_feed"):
        feed = "knowledge_embeddings" if window == "ann_feed" else "relation_embeddings"
        real_pages = projections.embedding_pages
        real_ids = projections.memory_derived_ids

        def embedding_pages(nb, table, id_column, **kw):
            state["armed"] = nb == notebook_id and table == feed
            try:
                yield from real_pages(nb, table, id_column, **kw)
            finally:
                state["armed"] = False

        def memory_derived_ids(nb, table):
            found = real_ids(nb, table)
            if table == feed:
                confirm_now()
            return found

        monkeypatch.setattr(projections, "embedding_pages", embedding_pages)
        monkeypatch.setattr(projections, "memory_derived_ids", memory_derived_ids)
        if vanish:
            stage = "gather_graph" if window == "ann_feed" else "_derive_object_graph_lite"
            real_stage = getattr(builder, stage)

            def next_stage(nb, *args, **kwargs):
                if nb == notebook_id and state["pending_delete"] is not None:
                    delete_a_memory(repo, notebook_id, state["pending_delete"])
                    state["pending_delete"] = None
                return real_stage(nb, *args, **kwargs)

            monkeypatch.setattr(builder, stage, next_stage, raising=False)
    else:
        raise ValueError(window)

    events: list = []
    real_emit = builder.event_log.emit

    def emit(event):
        events.append(dict(event))
        return real_emit(event)

    monkeypatch.setattr(builder.event_log, "emit", emit)
    return {"confirmed": confirmed, "events": events}


def assert_no_label_name_or_node_of(repo, notebook_id: str, memory_object_ids,
                                    names, relation_ids=()) -> None:
    """The published main index (ANN labels, graph node ids, embedded viz ids
    and names, relation ANN labels) holds none of the given Memory rows."""
    idx = repo._scale_index(notebook_id, allow_stale=True)
    assert idx is not None
    held = (set(idx.ann_labels) | set(idx.node_ids) | set(idx.viz_ids))
    assert not held & set(memory_object_ids), held & set(memory_object_ids)
    assert not set(names) & set(idx.viz_names)
    relation_labels = set(idx.relation_ann_labels or ())
    assert not relation_labels & set(relation_ids), relation_labels & set(relation_ids)


def _late(raced, key):
    return [c[key] for c in raced["confirmed"] if c.get(key)]


def assert_a_memory_confirmed_mid_build_never_publishes(repo, monkeypatch,
                                                        notebook_id, window,
                                                        existing_source=None,
                                                        vanish=False) -> None:
    """For the ANN windows -- where the feeds filter by an id set read up front
    and a Memory row confirmed afterwards DOES reach an ANN label. Shared by
    the SQLite tests and their PostgreSQL twins.

    1. One Memory row confirmed during the first attempt: that attempt holds it
       and publishes nothing, the build runs once more and publishes an index
       that is current and holds none of it.
    2. Confirmed during BOTH attempts: nothing is published, one content-free
       event is emitted, and the previous index stays as it was.

    ``vanish``: each memory is deleted again right after the build observed it
    (see ``arm_memory_confirmed_mid_build``), so the same holds only because
    the build keeps the union of every observation.
    """
    runtime = repo._runtime.scale_artifacts
    before = runtime.load(notebook_id, allow_stale=True).manifest["build_id"]
    raced = arm_memory_confirmed_mid_build(
        repo, monkeypatch, notebook_id, window=window, times=1,
        existing_source=existing_source, vanish=vanish,
    )
    manifest = repo.build_scale_index(notebook_id)
    assert len(raced["confirmed"]) == 1
    assert_no_label_name_or_node_of(
        repo, notebook_id, _late(raced, "object_id"), _late(raced, "name"),
        _late(raced, "relation_id"),
    )
    assert manifest["build_id"] != before
    # the one publish is current: served exactly, embedded viz included
    assert runtime.load(notebook_id) is not None
    assert manifest["build_id"] == runtime.load(notebook_id).manifest["build_id"]
    assert not [e for e in raced["events"] if "discarded" in str(e.get("kind"))]

    # re-armed on top (the first hooks are spent and pass straight through)
    raced = arm_memory_confirmed_mid_build(
        repo, monkeypatch, notebook_id, window=window, times=2,
        existing_source=existing_source, vanish=vanish,
    )
    outcome = repo.build_scale_index(notebook_id)
    assert outcome == {"status": "discarded", "notebook_id": notebook_id,
                       "reason": "memory_appeared_during_build"}
    assert [e for e in raced["events"] if e.get("kind") == "scale_index_build_discarded"] == [
        {"kind": "scale_index_build_discarded", "notebook_id": notebook_id,
         "reason": "memory_appeared_during_build"}
    ]
    kept = repo._scale_index(notebook_id, allow_stale=True)
    assert kept.manifest["build_id"] == manifest["build_id"]
    assert_no_label_name_or_node_of(
        repo, notebook_id, _late(raced, "object_id"), _late(raced, "name"),
        _late(raced, "relation_id"),
    )


def assert_a_memory_confirmed_at_the_object_read_never_reaches_a_published_index(
    repo, monkeypatch, notebook_id, *, vanish: bool
) -> None:
    """The viz derive's reads exclude Memory in the statement itself: a memory
    confirmed right before the object read -- and even deleted again right
    after it, so no snapshot of the build ever sees it -- is not in what the
    build publishes, and the build publishes on its first attempt (twice
    armed, still no discard: nothing reached the artifact)."""
    raced = arm_memory_confirmed_mid_build(
        repo, monkeypatch, notebook_id, window="object_read", times=2, vanish=vanish
    )
    manifest = repo.build_scale_index(notebook_id)
    assert len(raced["confirmed"]) == 1  # one attempt, one confirmation
    assert manifest.get("status") != "discarded"
    assert_no_label_name_or_node_of(
        repo, notebook_id, _late(raced, "object_id"), _late(raced, "name")
    )
    runtime = repo._runtime.scale_artifacts
    assert runtime.load(notebook_id) is not None
    served = repo._viz_index(notebook_id)
    assert served is not None
    assert not set(_late(raced, "name")) & set(served.viz_names)


def assert_a_memory_confirmed_mid_viz_build_never_publishes(repo, monkeypatch) -> None:
    """The standalone viz build: a memory confirmed right before its object read
    (and deleted again right after) is not in the published viz."""
    notebook_id = seed_notebook_without_memory(repo)
    for vanish in (False, True):
        raced = arm_memory_confirmed_mid_build(
            repo, monkeypatch, notebook_id, window="object_read", times=1,
            vanish=vanish,
        )
        manifest = repo.build_viz_index(notebook_id)
        assert manifest is not None and len(raced["confirmed"]) == 1
        published = repo._runtime.scale_artifacts.artifacts.load_viz(notebook_id)
        assert raced["confirmed"][0]["name"] not in set(published.viz_names)
        assert raced["confirmed"][0]["object_id"] not in set(published.viz_ids)


def assert_a_pre_isolation_index_is_rebuilt_once_its_memory_is_deleted(repo) -> None:
    """See ``test_fold_never_turns_a_pre_isolation_index_…``; shared with the
    PostgreSQL twin."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    store = repo._runtime.scale_artifacts.artifacts
    with repo._write() as db:
        db.execute(sql(repo, "UPDATE sources SET source_type='md' WHERE id=?"),
                   (MEMORY_SOURCE,))
    repo.build_scale_index(nb_id)
    make_it_look_pre_isolation(store.scale_dir(nb_id))
    with repo._write() as db:
        db.execute(sql(repo, "UPDATE sources SET source_type='memory' WHERE id=?"),
                   (MEMORY_SOURCE,))
    repo._scale_idx_cache.pop(nb_id, None)
    old = store.read_manifest(store.scale_dir(nb_id))
    assert "memory_isolation" not in old and MEMORY_SOURCE in old["watermark_sources"]

    repo.delete_source(MEMORY_SOURCE)  # the owner deletes the Memory
    with repo._write() as db:  # and a new shared source arrives
        db.execute(
            sql(repo, "INSERT INTO sources (id,notebook_id,title,source_type,"
                      "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)"),
            ("src-new", nb_id, "new", "md", "ready", NOW, NOW),
        )
    repo.store_kg(nb_id, "src-new", [_object("n1", "NEWCOMER")], [])
    events: list = []
    builder = repo._runtime.scale_builder
    real_emit = builder.event_log.emit
    builder.event_log.emit = lambda e: (events.append(dict(e)), real_emit(e))[1]
    try:
        folded = repo.fold_scale_index_delta(nb_id)
    finally:
        builder.event_log.emit = real_emit
    assert [e["reason"] for e in events if e.get("kind") == "scale_fold_refused"] == [
        "memory_isolation"
    ]
    assert folded["memory_isolation"] == 1
    idx = repo._runtime.scale_artifacts.load(nb_id)
    assert idx is not None and "NEWCOMER" in set(idx.viz_names)
    assert not set(SECRET_NAMES) & set(idx.viz_names)
    served = repo._viz_index(nb_id)
    assert served is not None and not set(SECRET_NAMES) & set(served.viz_names)


def _file_identities(root) -> dict:
    return {
        name: os.stat(os.path.join(str(root), name)).st_ino
        for name in sorted(os.listdir(str(root)))
        if name != "manifest.json" and os.path.isfile(os.path.join(str(root), name))
    }


def assert_an_index_built_before_the_first_memory_recovers_by_fold(repo) -> None:
    """P1-1: an index built by the isolating code while the notebook had no
    Memory stays usable when the first Memory arrives, and the fold that the
    scheduling runs re-stamps it (new build, same content, current version)
    instead of doing nothing forever; the same for a second Memory."""
    notebook_id = seed_notebook_without_memory(repo)
    first = repo.build_scale_index(notebook_id)
    runtime = repo._runtime.scale_artifacts
    n_nodes = runtime.status(notebook_id)["n_nodes"]
    assert n_nodes > 0
    late = []
    for n in (1, 2):
        late.append(confirm_a_memory(repo, notebook_id, n))
        # no manual memo reset: the Memory rows move the version by themselves
        status = runtime.status(notebook_id)
        assert status["state"] == "stale"
        assert status["n_nodes"] == n_nodes  # isolated artifact: counts still shown
        root = runtime.artifacts.scale_dir(notebook_id)
        files_before = _file_identities(root)
        folded = repo.fold_scale_index_delta(notebook_id)
        assert folded["build_id"] != first["build_id"]
        # P2-C: only the manifest was rewritten -- every array / ANN file of
        # the new live root is the very file the old one had (same inode)
        assert _file_identities(root) == files_before and files_before
        assert folded["memory_isolation"] == 1 and folded["memory_sources_digest"]
        # same content, so the same build time: the worker's full-build test
        # (a KG rebuild after ``built_at``) keeps seeing the original build
        assert folded["built_at"] == first["built_at"]
        status = runtime.status(notebook_id)
        assert (status["state"], status["n_nodes"]) == ("indexed", n_nodes), status
        assert runtime.load(notebook_id) is not None
        first = folded
    assert_no_label_name_or_node_of(
        repo, notebook_id, [m["object_id"] for m in late], [m["name"] for m in late]
    )
    # and a fold with nothing new to stamp stays a no-op
    assert repo.fold_scale_index_delta(notebook_id)["build_id"] == first["build_id"]
    # an ordinary fold (a new shared source) records the shared rows it saw,
    # so the next Memory-only change is still a re-stamp, not a full build
    with repo._write() as db:
        db.execute(
            sql(repo, "INSERT INTO sources (id,notebook_id,title,source_type,"
                      "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)"),
            ("src-folded", notebook_id, "folded", "md", "ready", NOW, NOW),
        )
    repo.store_kg(notebook_id, "src-folded", [_object("f", "FOLDED")], [])
    repo.fold_scale_index_delta(notebook_id)
    confirm_a_memory(repo, notebook_id, 3)
    root = runtime.artifacts.scale_dir(notebook_id)
    files_before = _file_identities(root)
    folded = repo.fold_scale_index_delta(notebook_id)
    assert _file_identities(root) == files_before, "a full build, not a re-stamp"
    assert runtime.load(notebook_id).manifest["build_id"] == folded["build_id"]


def _capture_events(repo) -> list:
    """Every event the scale builder emits from here on (the test's repository
    is discarded afterwards, so the hook is never taken off)."""
    builder = repo._runtime.scale_builder
    events: list = []
    real_emit = builder.event_log.emit
    builder.event_log.emit = lambda e: (events.append(dict(e)), real_emit(e))[1]
    return events


def assert_a_re_stamp_never_hides_a_shared_re_extraction(repo) -> None:
    """P2-N1: a shared source re-parsed under its old id (a new object, no new
    source -- so no delta source) followed by the KG rebuild ingestion runs,
    then a Memory confirmed: the fold must not re-stamp the old content as
    current. It builds in full, so the new object is in the index; without the
    Memory the index stays stale and the worker picks a full build."""
    notebook_id = seed_notebook_without_memory(repo)
    first = repo.build_scale_index(notebook_id)
    runtime = repo._runtime.scale_artifacts
    repo.store_kg(
        notebook_id, SHARED_SOURCE,
        [_object("a", "MOSFET"), _object("b", "gain"), _object("c", "NEWCOMER")],
        [],
    )
    repo.rebuild_unified_kg(notebook_id)
    # control: no Memory change -- nothing to fold or stamp, stale, full next
    assert repo.fold_scale_index_delta(notebook_id)["build_id"] == first["build_id"]
    assert runtime.load(notebook_id) is None
    assert runtime._resolve_mode(notebook_id, "auto") == "full"

    events = _capture_events(repo)
    confirm_a_memory(repo, notebook_id, next(_LATE_COUNTER))
    folded = repo.fold_scale_index_delta(notebook_id)
    assert [e["reason"] for e in events if e.get("kind") == "scale_fold_refused"] == [
        "kg_rebuilt_since_build"
    ]
    idx = runtime.load(notebook_id)
    assert idx is not None and "NEWCOMER" in set(idx.viz_names)
    assert folded["build_id"] == idx.manifest["build_id"] != first["build_id"]
    assert folded["built_at"] > first["built_at"]
    assert runtime._resolve_mode(notebook_id, "auto") == "fold"


def assert_a_re_stamp_never_hides_an_online_re_extraction(repo) -> None:
    """The online ingestion path re-parses a shared source under its old id
    (``store_kg`` + ``incremental_fuse_source``) and rebuilds no KG, so
    ``last_rebuild_at`` does not move. A Memory confirmed afterwards must not
    let the fold re-stamp the index as current without the new object: the
    shared rows differ from those it was built over, so it builds in full."""
    notebook_id = seed_notebook_without_memory(repo)
    first = repo.build_scale_index(notebook_id)
    runtime = repo._runtime.scale_artifacts
    repo.store_kg(
        notebook_id, SHARED_SOURCE,
        [_object("a", "MOSFET"), _object("b", "gain"), _object("c", "NEWCOMER")],
        [],
    )
    repo.incremental_fuse_source(notebook_id, SHARED_SOURCE)
    assert runtime.status(notebook_id)["state"] == "stale"
    events = _capture_events(repo)
    confirm_a_memory(repo, notebook_id, next(_LATE_COUNTER))
    folded = repo.fold_scale_index_delta(notebook_id)
    assert [e["reason"] for e in events if e.get("kind") == "scale_fold_refused"] == [
        "shared_content_changed"
    ]
    idx = runtime.load(notebook_id)
    assert idx is not None and "NEWCOMER" in set(idx.viz_names)
    assert folded["build_id"] == idx.manifest["build_id"] != first["build_id"]
    assert runtime.status(notebook_id)["state"] == "indexed"


def _change_only_shared_vectors(repo, notebook_id: str) -> None:
    """A shared object's vector rewritten (as a node-embedding backfill does);
    objects, relations, chunks and clusters untouched."""
    object_id = _object_ids_by_name(repo, notebook_id)["MOSFET"]
    vector = struct.pack("<16f", *([0.75] * 16))
    with repo._write() as db:
        db.execute(sql(repo, "DELETE FROM knowledge_embeddings WHERE object_id=?"),
                   (object_id,))
        db.execute(
            sql(repo, "INSERT INTO knowledge_embeddings (object_id,notebook_id,"
                      "vector,created_at) VALUES (?,?,?,?)"),
            (object_id, notebook_id, vector, "2026-12-31T00:00:00"),
        )


def _change_only_shared_clusters(repo, notebook_id: str) -> None:
    """The published generation's cluster rows of shared members rewritten;
    objects, relations, chunks and vectors untouched."""
    with repo._write() as db:
        state = db.execute(
            sql(repo, "SELECT cluster_generation FROM unified_kg_state "
                      "WHERE notebook_id=?"), (notebook_id,),
        ).fetchone()
        generation = int(
            0 if state is None or state["cluster_generation"] is None
            else state["cluster_generation"]
        )
        rows = db.execute(
            sql(repo, "SELECT COUNT(*) AS c FROM concept_clusters "
                      "WHERE notebook_id=? AND generation=?"),
            (notebook_id, generation),
        ).fetchone()["c"]
        assert rows, "the fixture's notebook has no published cluster rows"
        db.execute(
            sql(repo, "UPDATE concept_clusters SET created_at=? "
                      "WHERE notebook_id=? AND generation=?"),
            ("2026-12-31T00:00:00", notebook_id, generation),
        )
    repo._vector_cache.invalidate(f"{notebook_id}:clustermap")


def assert_a_re_stamp_never_hides_a_shared_change_of(repo, change: str) -> None:
    """Only the shared vectors (``vectors``) or only the published cluster rows
    (``clusters``) changed, no source and no KG rebuild -- then a Memory is
    confirmed: the fold must not re-stamp, because the shared rows the index
    was built over differ (each is one component of the shared-content
    digest)."""
    notebook_id = seed_notebook_without_memory(repo)
    first = repo.build_scale_index(notebook_id)
    {"vectors": _change_only_shared_vectors,
     "clusters": _change_only_shared_clusters}[change](repo, notebook_id)
    events = _capture_events(repo)
    confirm_a_memory(repo, notebook_id, next(_LATE_COUNTER))
    folded = repo.fold_scale_index_delta(notebook_id)
    assert [e["reason"] for e in events if e.get("kind") == "scale_fold_refused"] == [
        "shared_content_changed"
    ], change
    assert folded["build_id"] != first["build_id"]
    assert repo._runtime.scale_artifacts.load(notebook_id) is not None


def assert_a_re_stamp_republishes_the_source_partition_companion(repo) -> None:
    """P3-2: the companion pairs on the main manifest's build id; a re-stamp
    mints a new one, so it must republish the companion under it (else the
    companion is silently refused from then on)."""
    seeded = seed_shared_notebook_with_memory(repo)
    notebook_id = seeded["notebook_id"]
    make_the_shared_source_partitionable(repo, seeded)
    first = repo.build_scale_index(notebook_id)
    store = repo._runtime.scale_artifacts.artifacts
    root = store.source_partition_dir(notebook_id)

    def companion() -> dict:
        with open(os.path.join(str(root), "manifest.json")) as handle:
            return json.load(handle)

    assert companion()["parent_build_id"] == first["build_id"]
    assert companion()["published_sources"] == 1
    confirm_a_memory(repo, notebook_id, next(_LATE_COUNTER))
    files_before = _file_identities(store.scale_dir(notebook_id))
    folded = repo.fold_scale_index_delta(notebook_id)
    assert _file_identities(store.scale_dir(notebook_id)) == files_before  # a re-stamp
    assert folded["build_id"] != first["build_id"]
    assert companion()["parent_build_id"] == folded["build_id"]
    assert companion()["published_sources"] == 1
    from pathlib import Path

    from app.services.kg.source_partition_index import validate_partition_root

    validate_partition_root(
        Path(str(root)), folded["version"], store.scale_build_id(notebook_id)
    )


def _pre_isolation_notebook(repo, tier: str) -> str:
    """A notebook of ``tier`` without Memory whose scale index (embedded viz
    included) and standalone viz were built by the release before the
    isolation."""
    notebook_id = seed_notebook_without_memory(repo)
    with repo._write() as db:
        db.execute(sql(repo, "UPDATE notebooks SET tier=? WHERE id=?"),
                   (tier, notebook_id))
    store = repo._runtime.scale_artifacts.artifacts
    repo.build_scale_index(notebook_id)
    repo.build_viz_index(notebook_id)
    make_it_look_pre_isolation(store.scale_dir(notebook_id))
    make_it_look_pre_isolation(repo._viz_index_dir(notebook_id))
    repo._scale_idx_cache.pop(notebook_id, None)
    repo._viz_idx_cache.pop(notebook_id, None)
    return notebook_id


def assert_no_artifact_built_before_the_isolation_is_served(
    repo, monkeypatch, tier: str
) -> None:
    """Right after the upgrade, a notebook that holds no Memory -- personal or
    a public library -- serves nothing it built before: the index is stale
    with no counts, not loaded as current, and its viz is refused; a large
    notebook shows no preview until the scale build, a small one rebuilds its
    viz on the spot."""
    notebook_id = _pre_isolation_notebook(repo, tier)
    runtime = repo._runtime.scale_artifacts
    assert runtime.load(notebook_id) is None
    status = repo.scale_index_status(notebook_id)
    assert status["state"] == "stale" and status["stale"] is True
    assert (status["n_nodes"], status["n_ann"]) == (0, 0)
    probe = repo._viz_index_probe(notebook_id)
    assert (probe["viz_indexed"], probe["viz_nodes"], probe["viz_edges"]) == (False, 0, 0)

    with monkeypatch.context() as patch:
        patch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
        assert repo._viz_index(notebook_id) is None
    rebuilt = repo._viz_index(notebook_id)
    assert rebuilt is not None and rebuilt.manifest["memory_isolation"] == 1
    assert {"MOSFET", "gain"} <= set(rebuilt.viz_names)


def assert_a_pre_isolation_index_is_rebuilt_by_the_next_fold(repo, tier: str) -> None:
    """A pre-isolation index cannot tell whether it holds a Memory deleted
    before the upgrade (hard-deleted, gone from the watermark after a fold).
    So the first fold onto one is refused and replaced by a full build even
    for a notebook that holds no Memory and lost no source, a public library
    (tier ``base``) included -- once: the rebuilt index is isolated and the
    next fold is an ordinary one."""
    notebook_id = seed_notebook_without_memory(repo)
    with repo._write() as db:
        db.execute(sql(repo, "UPDATE notebooks SET tier=? WHERE id=?"),
                   (tier, notebook_id))
    store = repo._runtime.scale_artifacts.artifacts
    repo.build_scale_index(notebook_id)
    make_it_look_pre_isolation(store.scale_dir(notebook_id))
    repo._scale_idx_cache.pop(notebook_id, None)
    events = _capture_events(repo)
    for n in (1, 2):
        source_id = f"src-new-{n}"
        with repo._write() as db:
            db.execute(
                sql(repo, "INSERT INTO sources (id,notebook_id,title,source_type,"
                          "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)"),
                (source_id, notebook_id, "new", "md", "ready", NOW, NOW),
            )
        repo.store_kg(notebook_id, source_id, [_object("n", f"NEW-{n}")], [])
        repo.fold_scale_index_delta(notebook_id)
        if n == 1:
            # a full build derives its viz afresh (a fold would copy it over)
            idx = repo._runtime.scale_artifacts.load(notebook_id)
            assert idx is not None and "NEW-1" in set(idx.viz_names), tier
            assert idx.manifest["memory_isolation"] == 1
    refused = [e["reason"] for e in events if e.get("kind") == "scale_fold_refused"]
    assert refused == ["memory_isolation"], (tier, refused)


def seed_shared_notebook_with_memory(repo) -> dict:
    """Returns ``{"notebook_id", "objects": {name: id}, "memory_object_ids",
    "memory_relation_ids", "shared_relation_ids"}``."""
    nb = repo.create_notebook(NotebookCreate(name="shared with memory"))
    with repo._write() as db:
        for row in (
            (SHARED_SOURCE, nb.id, "shared", "md", "ready", NOW, NOW),
            (MEMORY_SOURCE, nb.id, "memory", "memory", "ready", NOW, NOW),
        ):
            db.execute(
                sql(repo, "INSERT INTO sources (id,notebook_id,title,source_type,"
                          "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)"),
                row,
            )
    repo.store_kg(
        nb.id, SHARED_SOURCE,
        [_object("a", "MOSFET"), _object("b", "gain"), _object("c", "bias"),
         _object("x", "left"), _object("y", "right")],
        [
            {"source_local_id": "a", "target_local_id": "b",
             "edge_type": "depends_on", "evidence": []},
            {"source_local_id": "a", "target_local_id": "c",
             "edge_type": "depends_on", "evidence": []},
        ],
    )
    repo.store_kg(
        nb.id, MEMORY_SOURCE,
        [_object("m1", SECRET_NAMES[0]), _object("m2", SECRET_NAMES[1])],
        [{"source_local_id": "m1", "target_local_id": "m2",
          "edge_type": "depends_on", "evidence": []}],
    )
    ids = _object_ids_by_name(repo, nb.id)
    a, b, x, y = ids["MOSFET"], ids["gain"], ids["left"], ids["right"]
    memory_relations = [
        # supported only by a Memory relation, between two shared objects
        (x, y, "depends_on"),
        # the same edge as the shared a->b, so supported by both
        (a, b, "depends_on"),
        # a Memory-only edge type on an existing shared pair
        (a, b, "contrasts_with"),
    ]
    with repo._write() as db:
        for index, (source, target, edge_type) in enumerate(memory_relations):
            db.execute(
                sql(repo, "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
                          "source_object_id,target_object_id,edge_type,evidence,"
                          "created_at,review_status) VALUES (?,?,?,?,?,?,?,?,?)"),
                (f"kr-mem-{index}", nb.id, MEMORY_SOURCE, source, target,
                 edge_type, json.dumps([]), NOW, "pending"),
            )
    memory_relation_ids = [f"kr-mem-{i}" for i in range(len(memory_relations))]
    memory_relation_ids.append(
        _relation_id(repo, nb.id, ids[SECRET_NAMES[0]], ids[SECRET_NAMES[1]],
                     "depends_on", MEMORY_SOURCE)
    )
    shared_relation_ids = [
        _relation_id(repo, nb.id, a, b, "depends_on", SHARED_SOURCE),
        _relation_id(repo, nb.id, a, ids["bias"], "depends_on", SHARED_SOURCE),
    ]
    vector = struct.pack("<16f", *([0.25] * 16))
    upsert = (
        "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,"
        "created_at) VALUES (?,?,?,?) ON CONFLICT (relation_id) DO UPDATE "
        "SET vector = EXCLUDED.vector"
    )
    with repo._write() as db:
        for relation_id in memory_relation_ids + shared_relation_ids:
            db.execute(sql(repo, upsert), (relation_id, nb.id, vector, NOW))
    repo.rebuild_unified_kg(nb.id)
    # concepts fold to their canonical id in every viz artifact
    folded = repo.cluster_map(nb.id)
    return {
        "notebook_id": nb.id,
        "objects": ids,
        "canonical": {name: folded.get(oid, oid) for name, oid in ids.items()},
        "memory_object_ids": [ids[name] for name in SECRET_NAMES],
        "memory_relation_ids": memory_relation_ids,
        "shared_relation_ids": shared_relation_ids,
    }
