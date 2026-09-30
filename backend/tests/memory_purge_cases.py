"""Memory hard delete / member exit purge scenarios, shared by both backends.

``test_memory_purge.py`` runs every case on SQLite and
``postgres/test_memory_purge_pg.py`` runs the same cases on PostgreSQL. Each
case drives the production entry points — the facade's ``delete_memory`` /
``bulk_delete_memories`` / ``remove_member`` / ``kick_all_members``, and the
member's own exit ``MemoryService.leave_notebook`` (the route's use case,
with its acknowledged Memory count) — against a world
whose Memory projections are REAL: the hidden source, its element and
extraction run come from the offline ingest pipeline, and its KG rows come
from ``store_kg`` with the source's running generation (objects, relations,
source-local facts, reverse index, embeddings), plus the cluster rows incremental fusion places them in.

The world deliberately contains everything a mis-scoped delete would hit:

* Alice's Memory in the shared notebook (the one under test), one of whose
  KG objects was MANUALLY MERGED into a shared document object (N-1);
* Bob's Memory in the same shared notebook (another member);
* Alice's Memory in her own notebook (same user, other notebook);
* Alice's Memory in Bob's notebook, where she is ALSO a plain member with no
  grant — exactly as exposed as in the shared notebook, so only the notebook
  scope of the exit purge keeps it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from app.models.schemas import NotebookCreate, SourceImportFile, SourceImportRequest
from app.services.embedding import FakeEmbedder
from app.services.memory_service import (
    ExitDisclosureRequired,
    MemberExitFailed,
    MemberExitIncomplete,
)
from app.core.request_context import reset_request_user, set_request_user
from tests.model_testkit import bind_all_embedding_clients

EMBED_DIM = 16
_GRANT_CREATED_AT = "2026-09-29T00:00:00+00:00"


class Sql:
    """Backend-neutral raw reads/writes for assertions (``?`` placeholders)."""

    def __init__(self, repo: Any, *, postgres: bool) -> None:
        self.repo = repo
        self.postgres = postgres

    def _text(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.repo._runtime.database.connect() as db:
            return [dict(row) for row in db.execute(self._text(sql), params).fetchall()]

    def count(self, sql: str, params: tuple = ()) -> int:
        return int(self.rows(sql, params)[0]["c"])

    def write(self, sql: str, params: tuple = ()) -> None:
        with self.repo._runtime.database.write() as db:
            db.execute(self._text(sql), params)


@dataclass
class Projection:
    memory_id: str
    notebook_id: str
    source_id: str
    element_ids: list[str]
    object_ids: list[str]
    relation_ids: list[str]


@dataclass
class World:
    repo: Any
    sql: Sql
    owner: Any
    alice: Any
    bob: Any
    shared: str
    alice_home: str
    elsewhere: str
    shared_doc_object: str
    shared_doc_source: str
    projections: dict[str, Projection] = field(default_factory=dict)


def _evidence(source_id: str, element_id: str, span: str) -> dict:
    return {
        "source_id": source_id,
        "source_title": span,
        "element_id": element_id,
        "element_type": "paragraph",
        "location_label": "p1",
        "quoted_span": span,
        "confidence": 0.9,
    }


def _as_user(user: Any, fn: Callable[[], Any]) -> Any:
    token = set_request_user(user)
    try:
        return fn()
    finally:
        reset_request_user(token)


def _doc_object(repo: Any, sql: Sql, user: Any, notebook_id: str) -> tuple[str, str]:
    """One uploaded document source and one KG object it owns."""
    imported = _as_user(
        user,
        lambda: repo.import_sources(
            notebook_id,
            SourceImportRequest(
                files=[
                    SourceImportFile(
                        file_name=f"doc-{notebook_id}.md",
                        file_size=10,
                        mime_type="text/markdown",
                    )
                ]
            ),
        ),
    )
    source_id = imported[0].id
    repo.store_kg(
        notebook_id,
        source_id,
        [
            {
                "local_id": "doc",
                "object_type": "concept",
                "payload": {"name": "Phase margin", "section_path": ""},
                "evidence": [_evidence(source_id, "", "doc span")],
            }
        ],
        [],
    )
    [row] = sql.rows(
        "SELECT id FROM knowledge_objects WHERE source_id=? AND notebook_id=?",
        (source_id, notebook_id),
    )
    return source_id, row["id"]


def legacy_fuse(
    world: World, notebook_id: str, source_id: str, *, bridge_embedder: Any = None
) -> None:
    """The cluster rows (and, with ``bridge_embedder``, the Tier-2 bridge
    merge candidates) incremental fusion wrote for a Memory source BEFORE the
    isolation work stopped clustering Memory objects — the legacy state the
    purge must clean up. Computed with fusion's own functions
    (``kg_merge.place_new_concepts`` with name seeds and the acronym redirect,
    ``kg_merge.detect_bridge_candidates``) and written with fusion's cluster
    shape and its candidate writer (``insert_merge_candidate``), so the rows
    have exactly the production shape whether or not fusion still clusters
    Memory objects today."""
    from app.services.kg_merge import (
        _norm,
        detect_bridge_candidates,
        place_new_concepts,
    )

    sql = world.sql
    objects = [
        {"object_id": row["id"], "name": str(row["name"] or "")}
        for row in sql.rows(
            ("SELECT id, payload->>'name' AS name FROM knowledge_objects "
             if sql.postgres else
             "SELECT id, json_extract(payload,'$.name') AS name FROM knowledge_objects ")
            + "WHERE source_id=? AND object_type='concept' AND status<>'deprecated' "
            "ORDER BY id",
            (source_id,),
        )
    ]
    clusters = sql.rows(
        "SELECT canonical_id, canonical_name, member_object_id FROM concept_clusters "
        "WHERE notebook_id=? AND object_type='concept'",
        (notebook_id,),
    )
    canon_names = {row["canonical_id"]: row["canonical_name"] for row in clusters}
    placed = place_new_concepts(
        objects, canon_names, seed_fn=lambda item: _norm(item["name"])
    )
    for row in placed:
        sql.write(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at) VALUES (?,?,?,?,?,'concept',?)",
            (f"cc-{row['member_object_id']}", notebook_id, row["canonical_id"],
             row["member_object_id"], row["canonical_name"], _GRANT_CREATED_AT),
        )
    if bridge_embedder is None or not objects:
        return
    own = {item["object_id"] for item in objects}
    existing = [
        {"object_id": row["member_object_id"], "name": row["canonical_name"]}
        for row in clusters if row["member_object_id"] not in own
    ]
    names = {
        row["id"]: str(row["name"] or "")
        for row in sql.rows(
            ("SELECT id, payload->>'name' AS name FROM knowledge_objects "
             if sql.postgres else
             "SELECT id, json_extract(payload,'$.name') AS name FROM knowledge_objects ")
            + "WHERE notebook_id=? AND object_type='concept'",
            (notebook_id,),
        )
    }
    for item in existing:
        item["name"] = names.get(item["object_id"], item["name"])
    vector = lambda text: bridge_embedder.embed_texts([text])[0]  # noqa: E731
    candidates = detect_bridge_candidates(
        objects,
        {item["object_id"]: vector(item["name"]) for item in objects},
        existing,
        {item["object_id"]: vector(item["name"]) for item in existing},
        {row["member_object_id"]: row["canonical_id"] for row in clusters},
        set(),
    )
    governance = world.repo._runtime.governance
    with world.repo._runtime.database.write() as db:
        for candidate in candidates:
            governance.insert_merge_candidate(
                db, notebook_id, candidate["canonical_a"], candidate["canonical_b"],
                candidate["score"], _GRANT_CREATED_AT, id_prefix="cm",
            )


def make_memory(
    world: World,
    key: str,
    notebook_id: str,
    user: Any,
    names: tuple[str, str] | None = None,
    bridge_embedder: Any = None,
) -> Projection:
    """A confirmed Memory with a real derived source and a full KG projection
    (two concept objects, named ``names`` or ``<key> loop`` / ``<key>
    compensation``), clustered the way fusion clustered Memory objects before
    the isolation work (``legacy_fuse``; with ``bridge_embedder`` also the
    bridge merge candidates it wrote)."""
    first_name, second_name = names or (f"{key} loop", f"{key} compensation")
    repo, sql = world.repo, world.sql
    service = repo._runtime.memory_service
    candidate = service.create_candidate(
        notebook_id, user.id, None, f"req-{key}", f"Memory {key}",
        f"Memory {key}: RC compensation keeps the loop stable.", [], "reason", {}, [],
    )
    memory = service.confirm(candidate.id, user.id)
    source_id = repo._runtime.source_ingestion.memory_source_id(memory.id)
    assert source_id, "the offline ingest must create the hidden Memory source"
    element_ids = [
        row["id"]
        for row in sql.rows(
            "SELECT id FROM source_elements WHERE source_id=? ORDER BY id", (source_id,)
        )
    ]
    assert element_ids
    [run] = sql.rows(
        "SELECT id,status FROM extraction_runs WHERE source_id=? "
        "ORDER BY created_at DESC, id DESC",
        (source_id,),
    )
    # Publish a KG generation for this source exactly as extraction would:
    # store_kg only writes source-local facts for the CURRENT running run.
    sql.write("UPDATE extraction_runs SET status='running' WHERE id=?", (run["id"],))
    evidence = [_evidence(source_id, element_ids[0], key)]
    repo._runtime.knowledge_lifecycle.store_kg(
        notebook_id,
        source_id,
        [
            {
                "local_id": "a", "object_type": "concept",
                "payload": {"name": first_name, "section_path": ""},
                "evidence": evidence,
            },
            {
                "local_id": "b", "object_type": "concept",
                "payload": {"name": second_name, "section_path": ""},
                "evidence": evidence,
            },
        ],
        [
            {
                "source_local_id": "a", "target_local_id": "b",
                "edge_type": "depends_on", "evidence": evidence,
            }
        ],
        source_generation=run["id"],
    )
    sql.write("UPDATE extraction_runs SET status=? WHERE id=?", (run["status"], run["id"]))
    object_ids = [
        row["id"]
        for row in sql.rows(
            "SELECT id FROM knowledge_objects WHERE source_id=? ORDER BY id", (source_id,)
        )
    ]
    relation_ids = [
        row["id"]
        for row in sql.rows(
            "SELECT id FROM knowledge_relations WHERE source_id=?", (source_id,)
        )
    ]
    legacy_fuse(world, notebook_id, source_id, bridge_embedder=bridge_embedder)
    projection = Projection(
        memory.id, notebook_id, source_id, element_ids, object_ids, relation_ids
    )
    world.projections[key] = projection
    return projection


def _json_list(value: Any) -> list:
    if isinstance(value, str):
        value = json.loads(value or "[]")
    return list(value or [])


def legacy_merge(world: World, notebook_id: str, object_id: str, into_id: str) -> None:
    """The state a manual ``merge_knowledge`` of ``object_id`` into
    ``into_id`` left before merges of Memory-derived objects were refused
    (E4-3), written directly in SQL on either backend — exactly what
    ``merge_objects_in_transaction`` did: the target's evidence gains the
    source's items (deduplicated by element and quoted span), the target's
    reverse-index rows are replaced from its evidence, the source object is
    deprecated in place, and the notebook's graph is marked dirty."""
    sql = world.sql
    [source] = sql.rows(
        "SELECT evidence FROM knowledge_objects WHERE id=? AND notebook_id=?",
        (object_id, notebook_id),
    )
    [target] = sql.rows(
        "SELECT evidence FROM knowledge_objects WHERE id=? AND notebook_id=?",
        (into_id, notebook_id),
    )
    merged = _json_list(target["evidence"])
    seen = {(item.get("element_id"), item.get("quoted_span")) for item in merged}
    for item in _json_list(source["evidence"]):
        key = (item.get("element_id"), item.get("quoted_span"))
        if key not in seen:
            merged.append(item)
            seen.add(key)
    evidence_value = "CAST(? AS jsonb)" if sql.postgres else "?"
    sql.write(
        f"UPDATE knowledge_objects SET evidence={evidence_value}, updated_at=? WHERE id=?",
        (json.dumps(merged, ensure_ascii=False), _GRANT_CREATED_AT, into_id),
    )
    sql.write("DELETE FROM knowledge_object_sources WHERE object_id=?", (into_id,))
    for source_id in dict.fromkeys(
        item["source_id"] for item in merged
        if isinstance(item, dict) and item.get("source_id")
    ):
        sql.write(
            "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
            "VALUES (?,?,?)",
            (into_id, source_id, notebook_id),
        )
    sql.write(
        "UPDATE knowledge_objects SET status='deprecated', last_reviewed=?, updated_at=? "
        "WHERE id=?",
        (_GRANT_CREATED_AT, _GRANT_CREATED_AT, object_id),
    )
    sql.write(
        "UPDATE unified_kg_state SET dirty=1, kg_mutation_seq=kg_mutation_seq+1 "
        "WHERE notebook_id=?",
        (notebook_id,),
    )


def build_world(repo: Any, *, postgres: bool) -> World:
    sql = Sql(repo, postgres=postgres)
    bind_all_embedding_clients(repo, FakeEmbedder(dim=EMBED_DIM))
    service = repo._runtime.memory_service
    service.kg_ingest_scheduler = lambda fn, item: fn(item)
    service.embedding_scheduler = lambda fn, job: fn(job)
    owner = repo.create_user("o00100001", "pw123456")
    alice = repo.create_user("a00100002", "pw123456")
    bob = repo.create_user("b00100003", "pw123456")
    shared = _as_user(
        owner, lambda: repo.create_notebook(NotebookCreate(name="Shared"))
    ).id
    alice_home = _as_user(
        alice, lambda: repo.create_notebook(NotebookCreate(name="Alice home"))
    ).id
    elsewhere = _as_user(
        bob, lambda: repo.create_notebook(NotebookCreate(name="Bob elsewhere"))
    ).id
    repo.add_member(shared, alice.id)
    repo.add_member(shared, bob.id)
    repo.add_member(elsewhere, alice.id)
    doc_source, doc_object = _doc_object(repo, sql, owner, shared)
    _doc_object(repo, sql, alice, alice_home)
    _doc_object(repo, sql, bob, elsewhere)
    world = World(
        repo, sql, owner, alice, bob, shared, alice_home, elsewhere,
        doc_object, doc_source,
    )
    make_memory(world, "alice", shared, alice)
    make_memory(world, "bob", shared, bob)
    make_memory(world, "alice_home", alice_home, alice)
    make_memory(world, "alice_elsewhere", elsewhere, alice)
    # N-1: a curator manually merged Alice's Memory-derived object into the
    # shared document object before merges of Memory objects were refused;
    # the shared object now also cites her Memory (legacy state, in SQL).
    alice_projection = world.projections["alice"]
    legacy_merge(world, shared, alice_projection.object_ids[0], doc_object)
    assert alice_projection.source_id in _evidence_sources(world, doc_object)
    # New notebooks are born with a trusted reverse index; the unbackfilled
    # case flips this explicitly.
    assert sql.count(
        "SELECT COUNT(*) AS c FROM unified_kg_state "
        "WHERE notebook_id=? AND source_index_backfilled=1",
        (shared,),
    ) == 1
    return world


def _evidence_sources(world: World, object_id: str) -> set[str]:
    [row] = world.sql.rows(
        "SELECT evidence FROM knowledge_objects WHERE id=?", (object_id,)
    )
    evidence = row["evidence"]
    if isinstance(evidence, str):
        evidence = json.loads(evidence or "[]")
    return {item.get("source_id") for item in evidence if isinstance(item, dict)}


def _in(ids: list[str]) -> str:
    return ",".join("?" for _ in ids)


def derived_counts(world: World, projection: Projection) -> dict[str, int]:
    """Every row derived from one Memory, table by table."""
    sql, src = world.sql, projection.source_id
    objects, relations = projection.object_ids, projection.relation_ids
    counts = {
        "memory_items": sql.count(
            "SELECT COUNT(*) AS c FROM memory_items WHERE id=?", (projection.memory_id,)
        ),
        "memory_revisions": sql.count(
            "SELECT COUNT(*) AS c FROM memory_revisions WHERE memory_id=?",
            (projection.memory_id,),
        ),
        "memory_provenance": sql.count(
            "SELECT COUNT(*) AS c FROM memory_provenance WHERE memory_id=?",
            (projection.memory_id,),
        ),
        "memory_embeddings": sql.count(
            "SELECT COUNT(*) AS c FROM memory_embeddings WHERE memory_id=?",
            (projection.memory_id,),
        ),
        "sources": sql.count("SELECT COUNT(*) AS c FROM sources WHERE id=?", (src,)),
        "sources_by_memory_id": sql.count(
            "SELECT COUNT(*) AS c FROM sources WHERE memory_id=?",
            (projection.memory_id,),
        ),
        "element_embeddings_of_elements": sql.count(
            f"SELECT COUNT(*) AS c FROM element_embeddings "
            f"WHERE element_id IN ({_in(projection.element_ids)})",
            tuple(projection.element_ids),
        ),
        "knowledge_embeddings": sql.count(
            f"SELECT COUNT(*) AS c FROM knowledge_embeddings "
            f"WHERE object_id IN ({_in(objects)})",
            tuple(objects),
        ),
        "relation_embeddings": sql.count(
            f"SELECT COUNT(*) AS c FROM relation_embeddings "
            f"WHERE relation_id IN ({_in(relations)})",
            tuple(relations),
        ),
        "concept_clusters": sql.count(
            f"SELECT COUNT(*) AS c FROM concept_clusters "
            f"WHERE member_object_id IN ({_in(objects)})",
            tuple(objects),
        ),
        "knowledge_objects_by_id": sql.count(
            f"SELECT COUNT(*) AS c FROM knowledge_objects WHERE id IN ({_in(objects)})",
            tuple(objects),
        ),
        "knowledge_relations_by_id": sql.count(
            f"SELECT COUNT(*) AS c FROM knowledge_relations WHERE id IN ({_in(relations)})",
            tuple(relations),
        ),
        "active_promotions": sql.count(
            "SELECT COUNT(*) AS c FROM promotion_candidates WHERE object_id=? "
            "AND status IN ('proposed','under_review')",
            (projection.memory_id,),
        ),
    }
    for table in (
        "source_elements",
        "element_embeddings",
        "extraction_runs",
        "knowledge_objects",
        "knowledge_relations",
        "knowledge_object_sources",
        "knowledge_source_facts",
        "knowledge_source_fact_elements",
        "kg_relation_completion_state",
        "chunks",
    ):
        counts[table] = sql.count(
            f"SELECT COUNT(*) AS c FROM {table} WHERE source_id=?", (src,)
        )
    if not sql.postgres:
        counts["kg_objects_fts"] = sql.count(
            f"SELECT COUNT(*) AS c FROM kg_objects_fts WHERE object_id IN ({_in(objects)})",
            tuple(objects),
        )
    return counts


def assert_purged(world: World, key: str) -> None:
    counts = derived_counts(world, world.projections[key])
    assert counts == {name: 0 for name in counts}, counts


def assert_intact(world: World, key: str) -> None:
    """The fixture's projection is fully present (populated tables only)."""
    counts = derived_counts(world, world.projections[key])
    for name in (
        "memory_items", "memory_revisions", "memory_provenance", "sources",
        "source_elements", "element_embeddings", "extraction_runs",
        "knowledge_objects", "knowledge_relations", "knowledge_object_sources",
        "knowledge_source_facts", "knowledge_source_fact_elements",
        "knowledge_embeddings", "relation_embeddings", "concept_clusters",
    ):
        assert counts[name] > 0, (key, name, counts)
    if not world.sql.postgres:
        # SQLite's KG lexical index of the surviving objects is untouched.
        assert counts["kg_objects_fts"] > 0, (key, counts)


def assert_shared_object_stripped(world: World) -> None:
    """N-1: the merged shared object survives, minus the Memory's evidence."""
    alice = world.projections["alice"]
    sql = world.sql
    assert sql.count(
        "SELECT COUNT(*) AS c FROM knowledge_objects WHERE id=?",
        (world.shared_doc_object,),
    ) == 1
    assert _evidence_sources(world, world.shared_doc_object) == {world.shared_doc_source}
    reverse = {
        row["source_id"]
        for row in sql.rows(
            "SELECT source_id FROM knowledge_object_sources WHERE object_id=?",
            (world.shared_doc_object,),
        )
    }
    assert reverse == {world.shared_doc_source}
    assert alice.source_id not in reverse
    if not sql.postgres:
        assert sql.count(
            "SELECT COUNT(*) AS c FROM kg_objects_fts WHERE object_id=?",
            (world.shared_doc_object,),
        ) == 1


def graph_state(world: World, notebook_id: str) -> tuple[int, int]:
    [row] = world.sql.rows(
        "SELECT dirty,kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
        (notebook_id,),
    )
    return int(row["dirty"]), int(row["kg_mutation_seq"])


def mark_clean(world: World, notebook_id: str) -> int:
    world.sql.write(
        "UPDATE unified_kg_state SET dirty=0 WHERE notebook_id=?", (notebook_id,)
    )
    return graph_state(world, notebook_id)[1]


def assert_marked_dirty(world: World, notebook_id: str, seq_before: int) -> None:
    dirty, seq = graph_state(world, notebook_id)
    assert dirty == 1
    assert seq > seq_before


def grant_user(world: World, notebook_id: str, user: Any) -> str:
    grant_id = f"gnt-{notebook_id}-{user.id}"
    world.sql.write(
        "INSERT INTO notebook_grants "
        "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
        "VALUES (?,?,'user',?,'reader',?,?)",
        (grant_id, notebook_id, user.id, world.owner.id, _GRANT_CREATED_AT),
    )
    return grant_id


def propose(world: World, key: str) -> str:
    """Put a Memory into the curator queue (mount a public base first)."""
    repo = world.repo
    projection = world.projections[key]
    base = repo.create_notebook(NotebookCreate(name=f"Base {key}"))
    repo.mark_notebook_base(base.id)
    notebook_owner = world.owner if projection.notebook_id == world.shared else world.alice
    repo.replace_notebook_bases(projection.notebook_id, [base.id], notebook_owner.id)
    owner_id = world.sql.rows(
        "SELECT created_by FROM memory_items WHERE id=?", (projection.memory_id,)
    )[0]["created_by"]
    return repo.propose_memory_promotion(projection.memory_id, owner_id)["id"]


def promotion_state(world: World, candidate_id: str) -> tuple[str, str]:
    [row] = world.sql.rows(
        "SELECT status,reason FROM promotion_candidates WHERE id=?", (candidate_id,)
    )
    return row["status"], row["reason"]




def service(world: World) -> Any:
    return world.repo._runtime.memory_service


def self_exit(world: World, user: Any, notebook_id: str, ack: int | None = None) -> int:
    """``DELETE /notebooks/{id}/membership`` below the route; returns what
    this exit deleted (the 200 body's ``deleted_memory_count``)."""
    return service(world).leave_notebook(notebook_id, user.id, ack)


def disclosure(world: World, user: Any, notebook_id: str) -> int:
    return service(world).exit_disclosure(notebook_id, user.id)


def expect_disclosure_required(fn: Callable[[], Any]) -> int:
    try:
        fn()
    except ExitDisclosureRequired as exc:
        return exc.memory_count
    raise AssertionError("the exit went ahead without the required acknowledgement")


def member_memory_count(world: World, notebook_id: str, user: Any) -> int:
    return world.sql.count(
        "SELECT COUNT(*) AS c FROM memory_items WHERE notebook_id=? AND created_by=?",
        (notebook_id, user.id),
    )


def grant_group(world: World, notebook_id: str, user: Any) -> None:
    world.sql.write(
        "INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
        "VALUES ('grp-exit','Exit group','project','',?,?,?)",
        (world.owner.id, _GRANT_CREATED_AT, _GRANT_CREATED_AT),
    )
    world.sql.write(
        "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
        "VALUES ('grp-exit',?,'member',?,?)",
        (user.id, _GRANT_CREATED_AT, world.owner.id),
    )
    world.sql.write(
        "INSERT INTO notebook_grants "
        "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
        "VALUES ('gnt-group-exit',?,'group','grp-exit','reader',?,?)",
        (notebook_id, world.owner.id, _GRANT_CREATED_AT),
    )


def grant_everyone(world: World, notebook_id: str) -> None:
    world.sql.write(
        "INSERT INTO notebook_grants "
        "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
        "VALUES ('gnt-everyone-exit',?,'everyone','','reader',?,?)",
        (notebook_id, world.owner.id, _GRANT_CREATED_AT),
    )


def plain_memory(world: World, notebook_id: str, user: Any, key: str, status: str) -> str:
    """A Memory in ``status`` without any derived projection."""
    svc = service(world)
    item = svc.create_candidate(
        notebook_id, user.id, None, f"req-plain-{key}", f"Plain {key}",
        f"Plain body {key}.", [], "reason", {}, [],
    )
    if status in ("confirmed", "deprecated"):
        item = svc.confirm(item.id, user.id, {"extract_kg": False})
    if status == "deprecated":
        item = svc.deprecate(item.id, user.id)
    if status == "rejected":
        item = svc.reject(item.id, user.id)
    assert item.status == status
    return item.id


def export_text(world: World, user: Any, notebook_id: str) -> str:
    return "".join(service(world).export_markdown(notebook_id, user.id, "Shared"))


class _CountingConnection:
    def __init__(self, inner: Any, counter: list[int]) -> None:
        self._inner = inner
        self._counter = counter

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self._counter[0] += 1
        return self._inner.execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> Any:
        self._counter[0] += 1
        return self._inner.executemany(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def count_statements(world: World, monkeypatch, fn: Callable[[], Any]) -> int:
    """SQL statements issued through the runtime database while ``fn`` runs."""
    from contextlib import contextmanager

    database = world.repo._runtime.database
    counter = [0]
    for name in ("connect", "write"):
        original = getattr(database, name)

        def counted(*args: Any, _original=original, **kwargs: Any):
            @contextmanager
            def manager():
                with _original(*args, **kwargs) as db:
                    yield _CountingConnection(db, counter)

            return manager()

        monkeypatch.setattr(database, name, counted)
    try:
        fn()
    finally:
        monkeypatch.undo()
    return counter[0]


# ---------------------------------------------------------------- cases


def case_hard_delete_removes_every_derived_row(world: World) -> None:
    seq = mark_clean(world, world.shared)
    world.repo.delete_memory(world.projections["alice"].memory_id, world.alice.id)
    assert_purged(world, "alice")
    assert_shared_object_stripped(world)
    assert_marked_dirty(world, world.shared, seq)
    assert_intact(world, "bob")
    assert_intact(world, "alice_home")
    assert_intact(world, "alice_elsewhere")


def case_hard_delete_on_an_unbackfilled_notebook(world: World) -> None:
    """Same result when the reverse index is not trusted (legacy notebook):
    both the strip and the delete fall back to scanning evidence."""
    world.sql.write(
        "UPDATE unified_kg_state SET source_index_backfilled=0 WHERE notebook_id=?",
        (world.shared,),
    )
    world.repo.delete_memory(world.projections["alice"].memory_id, world.alice.id)
    assert_purged(world, "alice")
    assert_shared_object_stripped(world)
    assert_intact(world, "bob")


def case_bulk_delete_removes_only_owned_rows(world: World) -> None:
    alice = world.projections["alice"]
    home = world.projections["alice_home"]
    deleted = world.repo.bulk_delete_memories(
        world.alice.id,
        [alice.memory_id, world.projections["bob"].memory_id, "missing",
         home.memory_id, alice.memory_id],
    )
    assert deleted == 2
    assert_purged(world, "alice")
    assert_purged(world, "alice_home")
    assert_shared_object_stripped(world)
    assert_intact(world, "bob")
    assert_intact(world, "alice_elsewhere")


def case_store_delete_enforces_the_creator(world: World) -> None:
    """Defence in depth: the store's own deletes are creator-scoped, whatever
    the caller pre-filtered."""
    store = service(world).store
    alice = world.projections["alice"]
    assert store.bulk_delete_memories(world.bob.id, [alice.memory_id]) == 0
    try:
        store.delete_memory(alice.memory_id, world.bob.id)
    except KeyError:
        pass
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("a non-creator deleted another user's Memory row")
    assert_intact(world, "alice")


def case_self_exit_requires_the_exact_acknowledgement(world: World) -> None:
    assert disclosure(world, world.alice, world.shared) == 1
    for ack in (None, 0, 2):
        assert expect_disclosure_required(
            lambda ack=ack: self_exit(world, world.alice, world.shared, ack)
        ) == 1
    assert world.repo.is_member(world.shared, world.alice.id)
    assert_intact(world, "alice")


def case_self_exit_purges_and_rejoin_starts_empty(world: World) -> None:
    repo = world.repo
    seq = mark_clean(world, world.shared)
    self_exit(world, world.alice, world.shared, 1)
    assert not repo.is_member(world.shared, world.alice.id)
    assert_purged(world, "alice")
    assert_shared_object_stripped(world)
    assert_marked_dirty(world, world.shared, seq)
    assert_intact(world, "bob")
    assert_intact(world, "alice_home")
    assert_intact(world, "alice_elsewhere")
    repo.add_member(world.shared, world.alice.id)
    assert disclosure(world, world.alice, world.shared) == 0
    assert repo.list_memories(world.alice.id, notebook_id=world.shared).items == []


def case_self_exit_with_nothing_to_delete_needs_no_acknowledgement(world: World) -> None:
    dave = world.repo.create_user("d00100004", "pw123456")
    world.repo.add_member(world.shared, dave.id)
    assert disclosure(world, dave, world.shared) == 0
    self_exit(world, dave, world.shared)
    assert not world.repo.is_member(world.shared, dave.id)
    self_exit(world, dave, world.shared)  # not a member: a no-op, like before
    assert_intact(world, "alice")
    assert_intact(world, "bob")


def _assert_exit_keeps_memory(world: World) -> None:
    assert disclosure(world, world.alice, world.shared) == 0
    self_exit(world, world.alice, world.shared)
    assert not world.repo.is_member(world.shared, world.alice.id)
    assert_intact(world, "alice")
    alice = world.projections["alice"]
    assert world.repo.get_memory(alice.memory_id, world.alice.id).id == alice.memory_id


def case_self_exit_keeps_memory_while_a_user_grant_reads(world: World) -> None:
    grant_user(world, world.shared, world.alice)
    _assert_exit_keeps_memory(world)


def case_self_exit_keeps_memory_while_a_group_grant_reads(world: World) -> None:
    grant_group(world, world.shared, world.alice)
    _assert_exit_keeps_memory(world)


def case_self_exit_keeps_memory_while_an_everyone_grant_reads(world: World) -> None:
    grant_everyone(world, world.shared)
    _assert_exit_keeps_memory(world)


def case_removal_by_others_never_deletes_memory(world: World) -> None:
    """Only the member's own acknowledged exit deletes Memory: an owner's
    removal, a notebook-wide kick and the facade's membership-only leave end
    the membership and keep every Memory."""
    repo = world.repo
    repo.remove_member(world.shared, world.alice.id)
    assert not repo.is_member(world.shared, world.alice.id)
    assert_intact(world, "alice")
    repo.kick_all_members(world.shared)
    assert not repo.is_member(world.shared, world.bob.id)
    assert_intact(world, "bob")
    repo.leave_notebook(world.elsewhere, world.alice.id)
    assert not repo.is_member(world.elsewhere, world.alice.id)
    assert_intact(world, "alice_elsewhere")
    assert world.sql.count(
        "SELECT COUNT(*) AS c FROM knowledge_objects WHERE id=?",
        (world.shared_doc_object,),
    ) == 1


def case_revoking_authorisation_keeps_memory(world: World) -> None:
    """Grant withdrawal and unsharing are not exits: nothing is deleted."""
    repo = world.repo
    alice = world.projections["alice"]
    grant_id = grant_user(world, world.shared, world.alice)
    self_exit(world, world.alice, world.shared)  # grant still reads: keeps
    assert repo._runtime.groups.delete_grant(world.shared, grant_id)
    assert_intact(world, "alice")
    repo.unshare_notebook(world.shared)  # drops Bob's membership row
    assert not repo.is_member(world.shared, world.bob.id)
    assert_intact(world, "bob")
    # Invisible while access is gone, back as soon as it returns.
    try:
        repo.get_memory(alice.memory_id, world.alice.id)
    except KeyError:
        pass
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("a Memory stayed readable after access was revoked")
    grant_user(world, world.shared, world.alice)
    assert repo.get_memory(alice.memory_id, world.alice.id).id == alice.memory_id


def case_contract_a_finished_exit_reports_what_it_deleted(world: World) -> None:
    """Contract v2, 200: the number returned is what this exit deleted."""
    plain_memory(world, world.shared, world.alice, "contract-200", "candidate")
    assert disclosure(world, world.alice, world.shared) == 2
    assert self_exit(world, world.alice, world.shared, 2) == 2
    assert not world.repo.is_member(world.shared, world.alice.id)
    assert member_memory_count(world, world.shared, world.alice) == 0


def case_contract_zero_claim_refuses_a_positive_acknowledgement(world: World) -> None:
    """Contract v2, spec P2-1: the member acknowledged 1, then gained a grant
    (or lost the membership) while the panel was open. The exit would delete
    0, so the acknowledgement is refused with 0 and nothing changes; only an
    acknowledgement of 0 (or none) ends the membership, deleting nothing."""
    grant_user(world, world.shared, world.alice)
    assert expect_disclosure_required(
        lambda: self_exit(world, world.alice, world.shared, 1)
    ) == 0
    assert world.repo.is_member(world.shared, world.alice.id)
    assert_intact(world, "alice")
    assert self_exit(world, world.alice, world.shared, 0) == 0
    assert not world.repo.is_member(world.shared, world.alice.id)
    assert_intact(world, "alice")
    # No longer a member at all: a stale acknowledgement is refused too.
    assert expect_disclosure_required(
        lambda: self_exit(world, world.bob, world.alice_home, 3)
    ) == 0
    assert_intact(world, "bob")


def _promotion_row(world: World, candidate_id: str) -> dict:
    [row] = world.sql.rows(
        "SELECT status,reason,reviewed_by,target_base_id FROM promotion_candidates "
        "WHERE id=?",
        (candidate_id,),
    )
    return row


def case_delete_withdraws_active_promotion(world: World) -> None:
    candidate_id = propose(world, "alice_home")
    world.repo.delete_memory(world.projections["alice_home"].memory_id, world.alice.id)
    row = _promotion_row(world, candidate_id)
    # A withdrawal is not a review: no reviewer is recorded.
    assert (row["status"], row["reason"], row["reviewed_by"]) == (
        "rejected", "withdrawn_memory_deleted", "",
    )
    assert_purged(world, "alice_home")


def case_exit_withdraws_active_promotion(world: World) -> None:
    candidate_id = propose(world, "alice")
    self_exit(world, world.alice, world.shared, 1)
    row = _promotion_row(world, candidate_id)
    assert (row["status"], row["reason"], row["reviewed_by"]) == (
        "rejected", "withdrawn_memory_deleted", "",
    )
    assert_purged(world, "alice")
    assert_intact(world, "bob")
    assert_intact(world, "alice_elsewhere")


def case_approved_promotion_survives_deletion(world: World) -> None:
    candidate_id = propose(world, "alice_home")
    world.repo.approve_promotion(candidate_id)
    before = _promotion_row(world, candidate_id)
    base_objects = world.sql.count(
        "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
        (before["target_base_id"],),
    )
    assert before["status"] == "approved" and base_objects > 0
    world.repo.delete_memory(world.projections["alice_home"].memory_id, world.alice.id)
    assert _promotion_row(world, candidate_id) == before
    assert world.sql.count(
        "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
        (before["target_base_id"],),
    ) == base_objects
    assert_purged(world, "alice_home")


def _seed_conflict_candidates(world: World) -> None:
    """Conflict candidates in the shape the conflict detector writes: object
    or relation ids on both sides. The detector itself needs a model; the
    shape (``left_ref``/``right_ref``/``winner_ref`` = ids) is what matters."""
    alice = world.projections["alice"]
    bob = world.projections["bob"]
    rows = [
        ("kc-alice-obj", "node", alice.object_ids[1], world.shared_doc_object, None, "pending"),
        ("kc-alice-rel", "edge", world.shared_doc_object, alice.relation_ids[0], None, "resolved"),
        ("kc-alice-winner", "node", world.shared_doc_object, bob.object_ids[0],
         alice.object_ids[1], "resolved"),
        ("kc-bob-obj", "node", bob.object_ids[1], world.shared_doc_object, None, "pending"),
        # A curator's decision between shared objects only: never touched.
        ("kc-shared-only", "node", world.shared_doc_object, world.shared_doc_object,
         world.shared_doc_object, "resolved"),
    ]
    for cid, kind, left, right, winner, status in rows:
        world.sql.write(
            "INSERT INTO kg_conflict_candidates "
            "(id,notebook_id,kind,left_ref,right_ref,winner_ref,rationale,status,"
            "created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,'quotes the Memory text',?,?,?)",
            (cid, world.shared, kind, left, right, winner, status,
             _GRANT_CREATED_AT, _GRANT_CREATED_AT),
        )


def case_review_candidates_of_deleted_objects_are_deleted(world: World) -> None:
    """Conflict candidates referencing the Memory's objects or relations go
    (any status: the rationale can quote the Memory); a decision between
    shared objects only and another member's candidates stay."""
    _seed_conflict_candidates(world)
    world.repo.delete_memory(world.projections["alice"].memory_id, world.alice.id)
    conflicts = {
        row["id"] for row in world.sql.rows("SELECT id FROM kg_conflict_candidates")
    }
    assert conflicts == {"kc-bob-obj", "kc-shared-only"}
    assert_purged(world, "alice")
    assert_intact(world, "bob")


class _TwinEmbedder(FakeEmbedder):
    """Texts naming "twin" share one direction (cosine 1 with each other,
    about 0 with the hash vectors of everything else), so incremental fusion's
    Tier-2 bridge detector really proposes merges between them."""

    def _vec(self, text: str) -> list[float]:
        if "twin" in text.lower():
            return [1.0 if index % 2 == 0 else -1.0 for index in range(self.dim)]
        return super()._vec(text)


def _fused_doc_object(world: World, name: str) -> str:
    """A document source with one concept object, fused like extraction does."""
    repo = world.repo
    imported = _as_user(
        world.owner,
        lambda: repo.import_sources(
            world.shared,
            SourceImportRequest(files=[SourceImportFile(
                file_name=f"{name}.md", file_size=10, mime_type="text/markdown",
            )]),
        ),
    )
    source_id = imported[0].id
    repo.store_kg(
        world.shared,
        source_id,
        [{
            "local_id": "d", "object_type": "concept",
            "payload": {"name": name, "section_path": ""},
            "evidence": [_evidence(source_id, "", name)],
        }],
        [],
    )
    repo._runtime.knowledge_lifecycle.incremental_fuse_source(world.shared, source_id)
    [row] = world.sql.rows(
        "SELECT id FROM knowledge_objects WHERE source_id=?", (source_id,)
    )
    return row["id"]


def _merge_candidates(world: World) -> set[tuple[str, str]]:
    return {
        (row["canonical_a"], row["canonical_b"])
        for row in world.sql.rows(
            "SELECT canonical_a,canonical_b FROM concept_merge_candidates "
            "WHERE notebook_id=?",
            (world.shared,),
        )
    }


def case_merge_candidates_written_by_fusion_are_deleted(world: World) -> None:
    """Item 3: merge candidates hold CLUSTER canonical ids (``K-<seed>``),
    written here by fusion's own functions and writer (``legacy_fuse``: the
    shape fusion wrote for Memory objects before the isolation work). The Memory's "TLL" is placed
    (acronym redirect) into the shared "Twin locked loop (TLL)" cluster, while
    the bridge detector names it ``K-tll`` — an id no cluster row carries.
    After the purge no candidate names the Memory's clusters (the shared
    cluster it joined goes whole) or its bridge id; the candidate between two
    untouched shared clusters stays."""
    bind_all_embedding_clients(world.repo, _TwinEmbedder(dim=EMBED_DIM))
    _fused_doc_object(world, "Twin locked loop (TLL)")
    _fused_doc_object(world, "Twin shared notes")
    _fused_doc_object(world, "Twin other notes")
    make_memory(
        world, "twin", world.shared, world.alice, names=("TLL", "twin private idea"),
        bridge_embedder=_TwinEmbedder(dim=EMBED_DIM),
    )
    before = _merge_candidates(world)
    shared_pair = ("K-twin other notes", "K-twin shared notes")
    assert shared_pair in before, before
    assert any("K-tll" in pair for pair in before), before
    assert world.sql.count(
        "SELECT COUNT(*) AS c FROM concept_clusters WHERE canonical_id='K-tll'"
    ) == 0
    memory_names = {"K-tll", "K-twin private idea", "K-twin locked loop"}
    world.repo.delete_memory(world.projections["twin"].memory_id, world.alice.id)
    after = _merge_candidates(world)
    assert not any(set(pair) & memory_names for pair in after), after
    assert shared_pair in after
    assert_purged(world, "twin")


def _seed_memory_named_cluster(world: World) -> str:
    """The B2 legacy state (E5-1 spec review and the fix2 re-review's probe):
    Alice's live Memory object seeded the cluster ``K-alice-private plan`` —
    name and description are her text — whose other members are two SHARED
    document objects, one of them in the building generation; a pending and
    a rejected merge candidate name that cluster. Returns the second shared
    object's id."""
    alice = world.projections["alice"]
    memory_object = next(
        object_id for object_id in alice.object_ids
        if world.sql.count(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE id=? AND status<>'deprecated'",
            (object_id,),
        )
    )
    _other_source, other_shared = _doc_object(
        world.repo, world.sql, world.owner, world.shared
    )
    world.sql.write(
        "DELETE FROM concept_clusters WHERE member_object_id IN (?,?,?)",
        (memory_object, world.shared_doc_object, other_shared),
    )
    for member, generation in (
        (world.shared_doc_object, 0), (memory_object, 0), (other_shared, 1),
    ):
        world.sql.write(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,canonical_description,created_at,generation) "
            "VALUES (?,?,'K-alice-private plan',?,'ALICE-PRIVATE plan','concept',"
            "'ALICE-PRIVATE description',?,?)",
            (f"cc-b2-{member}", world.shared, member, _GRANT_CREATED_AT, generation),
        )
    for mid, status in (("mc-b2-pending", "pending"), ("mc-b2-rejected", "rejected")):
        world.sql.write(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
            "VALUES (?,?,'K-alice-private plan','K-bob loop',0.9,?,?,?)",
            (mid, world.shared, status, _GRANT_CREATED_AT, _GRANT_CREATED_AT),
        )
    # The seed arm (``memory_sql.cluster_seed_object_id``): a cluster whose
    # canonical id was MINTED from the Memory object (``K-~<object id>``) but
    # which no longer has it as a member — only a shared one — plus a
    # candidate naming that minted id.
    seed_id = f"K-~{memory_object}"
    world.sql.write(
        "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
        "canonical_name,object_type,canonical_description,created_at,generation) "
        "VALUES ('cc-b2-seed',?,?,?,'ALICE-PRIVATE seed','concept','',?,1)",
        (world.shared, seed_id, world.shared_doc_object, _GRANT_CREATED_AT),
    )
    world.sql.write(
        "INSERT INTO concept_merge_candidates "
        "(id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
        "VALUES ('mc-b2-seed',?,'K-bob loop',?,0.9,'pending',?,?)",
        (world.shared, seed_id, _GRANT_CREATED_AT, _GRANT_CREATED_AT),
    )
    return other_shared


def _memory_named_leftovers(world: World, other_shared: str) -> dict[str, int]:
    """Everything the B2 state must not leave behind, as counts; the purge of
    any path must turn it into ``_CLEAN``."""
    return {
        "shared_doc_object": world.sql.count(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE id=?",
            (world.shared_doc_object,),
        ),
        "other_shared_object": world.sql.count(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE id=?", (other_shared,)
        ),
        "memory_named_cluster_rows": world.sql.count(
            "SELECT COUNT(*) AS c FROM concept_clusters WHERE notebook_id=? AND ("
            "canonical_id='K-alice-private plan' OR canonical_name LIKE ? "
            "OR canonical_description LIKE ?)",
            (world.shared, "ALICE-PRIVATE%", "ALICE-PRIVATE%"),
        ),
        "memory_named_candidates": world.sql.count(
            "SELECT COUNT(*) AS c FROM concept_merge_candidates "
            "WHERE canonical_a='K-alice-private plan' OR canonical_b='K-alice-private plan' "
            "OR id='mc-b2-seed'"
        ),
        "memory_evidence_on_shared": int(
            world.projections["alice"].source_id
            in _evidence_sources(world, world.shared_doc_object)
        ),
    }


_CLEAN = {
    "shared_doc_object": 1, "other_shared_object": 1,
    "memory_named_cluster_rows": 0, "memory_named_candidates": 0,
    "memory_evidence_on_shared": 0,
}


def case_whole_clusters_of_the_memory_are_removed(world: World) -> None:
    """B2 (E5-1 spec review): a cluster seeded by a Memory object keeps the
    Memory's canonical id, name and description on its SHARED members' rows.
    The exit removes every cluster the Memory's object belongs to — all
    member rows, in EVERY generation — and the merge candidates naming it;
    the shared objects survive and the notebook is marked for a rebuild."""
    other_shared = _seed_memory_named_cluster(world)
    mark_clean(world, world.shared)
    self_exit(world, world.alice, world.shared, 1)
    assert _memory_named_leftovers(world, other_shared) == _CLEAN
    assert graph_state(world, world.shared)[0] == 1
    assert_intact(world, "bob")


def case_deprecate_then_exit_leaves_nothing_memory_named(world: World) -> None:
    """P1-1 (fix2 re-review): deprecating a confirmed Memory removes its
    derived source through ``remove_memory_source``; it must strip the
    shared object's Memory evidence (the shared object survives, N-1) and
    purge the Memory-named cluster and candidates — the later exit no longer
    finds a source to clean."""
    other_shared = _seed_memory_named_cluster(world)
    service(world).deprecate(world.projections["alice"].memory_id, world.alice.id)
    assert _memory_named_leftovers(world, other_shared) == _CLEAN
    self_exit(world, world.alice, world.shared, disclosure(world, world.alice, world.shared))
    assert _memory_named_leftovers(world, other_shared) == _CLEAN
    assert_purged(world, "alice")


def case_transfer_move_leaves_nothing_memory_named(world: World) -> None:
    """P1-1: the exit panel's "transfer" (move) removes the Memory's source
    through ``remove_memory_source`` inside ``transfer``: same cleanup."""
    other_shared = _seed_memory_named_cluster(world)
    result = service(world).transfer(
        world.alice.id, [world.projections["alice"].memory_id], world.alice_home,
        "move", extract_kg=False,
    )
    assert [item["status"] for item in result] == ["moved"]
    assert _memory_named_leftovers(world, other_shared) == _CLEAN
    assert disclosure(world, world.alice, world.shared) == 0


def case_post_ingest_cleanup_leaves_nothing_memory_named(
    world: World, monkeypatch
) -> None:
    """P1-1: a KG ingest that finishes after the Memory stopped being
    confirmed removes the source it just rebuilt (``_kg_ingest_job``'s
    recheck) through ``remove_memory_source``: same cleanup."""
    other_shared = _seed_memory_named_cluster(world)
    svc = service(world)
    alice = world.projections["alice"]

    def deprecated_meanwhile(*_args, **_kwargs):
        # A concurrent deprecate committed while the ingest ran (its own
        # removal found nothing yet); the ingest leaves the source in place.
        world.sql.write(
            "UPDATE memory_items SET status='deprecated' WHERE id=?", (alice.memory_id,)
        )
        return alice.source_id

    monkeypatch.setattr(svc.memory_kg, "ingest_memory_source", deprecated_meanwhile)
    svc._kg_ingest_job((alice.memory_id, world.alice.id))
    assert svc.memory_kg.memory_source_id(alice.memory_id) is None
    assert _memory_named_leftovers(world, other_shared) == _CLEAN


def case_one_page_strips_every_merged_source(world: World) -> None:
    """P2-2 (fix2 re-review): one purge page holding two Memory sources, each
    merged into a DIFFERENT shared object: both shared objects survive and
    both lose exactly that Memory's evidence."""
    _src_x, shared_x = _doc_object(world.repo, world.sql, world.owner, world.shared)
    _src_y, shared_y = _doc_object(world.repo, world.sql, world.owner, world.shared)
    first = make_memory(world, "merged-x", world.shared, world.alice)
    second = make_memory(world, "merged-y", world.shared, world.alice)
    legacy_merge(world, world.shared, first.object_ids[0], shared_x)
    legacy_merge(world, world.shared, second.object_ids[0], shared_y)
    count = disclosure(world, world.alice, world.shared)
    assert self_exit(world, world.alice, world.shared, count) == count
    for shared, projection in ((shared_x, first), (shared_y, second)):
        assert world.sql.count(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE id=?", (shared,)
        ) == 1
        assert projection.source_id not in _evidence_sources(world, shared)
    assert_purged(world, "merged-x")
    assert_purged(world, "merged-y")


def case_a_live_cluster_with_the_bridge_id_keeps_its_candidates(world: World) -> None:
    """P2-3 (fix2 re-review): a Memory concept named like a shared concept
    ("Phase margin") has the bridge id ``K-phase margin`` — which is also a
    LIVE shared cluster. A curator's decision between that shared cluster and
    another one is not the Memory's and stays; only a bridge id no cluster
    carries is purged. (The Memory object here is unclustered, as the
    isolation work leaves every Memory object.)"""
    world.repo._runtime.knowledge_lifecycle.incremental_fuse_source(
        world.shared, world.shared_doc_source
    )
    assert world.sql.count(
        "SELECT COUNT(*) AS c FROM concept_clusters WHERE canonical_id='K-phase margin'"
    ) >= 1
    twin = make_memory(
        world, "phase", world.shared, world.alice, names=("Phase margin", "phase private")
    )
    world.sql.write(
        f"DELETE FROM concept_clusters WHERE member_object_id IN ({_in(twin.object_ids)})",
        tuple(twin.object_ids),
    )
    for mid, other in (("mc-live-shared", "K-shared other"), ("mc-bridge-only", "K-phase private")):
        world.sql.write(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
            "VALUES (?,?,'K-phase margin',?,0.9,'rejected',?,?)",
            (mid, world.shared, other, _GRANT_CREATED_AT, _GRANT_CREATED_AT),
        )
    world.repo.delete_memory(twin.memory_id, world.alice.id)
    remaining = {
        row["id"] for row in world.sql.rows(
            "SELECT id FROM concept_merge_candidates WHERE id IN ('mc-live-shared','mc-bridge-only')"
        )
    }
    # K-phase private is the Memory's own bridge id with no cluster: its
    # candidate goes; the shared pair stays.
    assert remaining == {"mc-live-shared"}, remaining


def case_mixed_statuses_are_counted_exported_and_deleted_together(world: World) -> None:
    """The disclosure counts every status; the export lists exactly those rows,
    each labelled; transfer carries only confirmed ones; the exit deletes
    exactly what was counted."""
    shared, alice = world.shared, world.alice
    candidate = plain_memory(world, shared, alice, "cand", "candidate")
    confirmed = plain_memory(world, shared, alice, "conf", "confirmed")
    plain_memory(world, shared, alice, "rej", "rejected")
    plain_memory(world, shared, alice, "dep", "deprecated")
    assert disclosure(world, alice, shared) == member_memory_count(world, shared, alice) == 5
    text = export_text(world, alice, shared)
    for title in ("Memory alice", "Plain cand", "Plain conf", "Plain rej", "Plain dep"):
        assert f". {title}\n" in text, title
    for label in ("候选（尚未确认）", "已确认", "已拒绝", "已弃用"):
        assert f"- 状态：{label}" in text, label
    assert text.count("\n## ") == 5
    for foreign in ("Memory bob", "Memory alice_home", "Memory alice_elsewhere"):
        assert foreign not in text
    results = service(world).transfer(
        alice.id,
        [world.projections["alice"].memory_id, confirmed, candidate],
        world.alice_home,
        "move",
    )
    assert [result["status"] for result in results] == ["moved", "moved", "failed"]
    assert disclosure(world, alice, shared) == 3
    home_before = member_memory_count(world, world.alice_home, alice)
    self_exit(world, alice, shared, 3)
    assert member_memory_count(world, shared, alice) == 0
    assert member_memory_count(world, world.alice_home, alice) == home_before
    assert not world.repo.is_member(shared, alice.id)


def case_export_needs_read_access_and_is_lazy(world: World) -> None:
    stranger = world.repo.create_user("s00100005", "pw123456")
    try:
        service(world).export_markdown(world.shared, stranger.id, "Shared")
    except PermissionError:
        pass
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("a non-reader was handed an export")
    text = export_text(world, world.bob, world.shared)
    assert "Memory bob" in text and "Memory alice" not in text
    assert text.rstrip().endswith("共导出 1 条记忆。")


def _member_job_row(world: World, notebook_id: str, user: Any) -> None:
    """A per-member overlay job row — part of the state ``forget_member_state``
    clears when a membership ends."""
    world.sql.write(
        "INSERT INTO agent_profile_jobs "
        "(notebook_id,owner_id,status,pending_signal,created_at,updated_at) "
        "VALUES (?,?,'idle',3,?,?)",
        (notebook_id, user.id, _GRANT_CREATED_AT, _GRANT_CREATED_AT),
    )


def _member_job_rows(world: World, notebook_id: str, user: Any) -> int:
    return world.sql.count(
        "SELECT COUNT(*) AS c FROM agent_profile_jobs WHERE notebook_id=? AND owner_id=?",
        (notebook_id, user.id),
    )


def case_a_finished_exit_clears_the_members_overlay(world: World) -> None:
    """The membership this exit ended loses its per-member overlay state."""
    _member_job_row(world, world.shared, world.alice)
    self_exit(world, world.alice, world.shared, 1)
    assert _member_job_rows(world, world.shared, world.alice) == 0


_TEARDOWN_TABLES = (
    "sources", "source_elements", "element_embeddings", "extraction_runs",
    "knowledge_objects", "knowledge_relations", "knowledge_object_sources",
    "knowledge_source_facts", "knowledge_source_fact_elements",
    "kg_relation_completion_state", "knowledge_embeddings", "relation_embeddings",
    "concept_clusters",
)


def _table_counts(world: World) -> dict[str, int]:
    return {
        table: world.sql.count(f"SELECT COUNT(*) AS c FROM {table}")
        for table in _TEARDOWN_TABLES
    }


def case_single_and_batched_teardown_have_the_same_outcome(world: World) -> None:
    """Item 8: ``delete_source`` (one source) and ``remove_memory_sources``
    (a page) are one implementation. On a mixed fixture — three Memory
    projections removed as one batch, three removed one ``delete_source`` at
    a time — every table loses exactly the same number of rows, and each
    projection is gone entirely."""
    ingestion = world.repo._runtime.source_ingestion
    batch = [make_memory(world, f"batch-{n}", world.shared, world.alice) for n in range(3)]
    single = [make_memory(world, f"single-{n}", world.shared, world.alice) for n in range(3)]
    before = _table_counts(world)
    assert ingestion.remove_memory_sources([p.source_id for p in batch]) == 3
    middle = _table_counts(world)
    for projection in single:
        world.repo.delete_source(projection.source_id)
    after = _table_counts(world)
    batch_delta = {table: before[table] - middle[table] for table in _TEARDOWN_TABLES}
    single_delta = {table: middle[table] - after[table] for table in _TEARDOWN_TABLES}
    assert batch_delta == single_delta, (batch_delta, single_delta)
    assert batch_delta["sources"] == 3 and batch_delta["knowledge_objects"] > 0
    for projection in (*batch, *single):
        counts = derived_counts(world, projection)
        for table in ("sources", "source_elements", "knowledge_objects_by_id",
                      "knowledge_relations_by_id", "knowledge_embeddings",
                      "relation_embeddings", "concept_clusters",
                      "knowledge_object_sources", "knowledge_source_facts",
                      "extraction_runs", "element_embeddings"):
            assert counts[table] == 0, (projection.memory_id, table, counts)


def case_the_memory_purge_refuses_a_document_source(world: World) -> None:
    """``remove_memory_sources`` is not a general source delete: a batch
    holding a document source is refused as a whole (the Memory test is
    ``memory_sql``'s predicate) and nothing is removed."""
    ingestion = world.repo._runtime.source_ingestion
    alice = world.projections["alice"]
    try:
        ingestion.remove_memory_sources([alice.source_id, world.shared_doc_source])
    except ValueError:
        pass
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("a document source went through the Memory purge")
    assert world.sql.count(
        "SELECT COUNT(*) AS c FROM sources WHERE id IN (?,?)",
        (alice.source_id, world.shared_doc_source),
    ) == 2
    assert_intact(world, "alice")


CASES: dict[str, Callable[..., None]] = {
    "hard_delete": case_hard_delete_removes_every_derived_row,
    "hard_delete_unbackfilled": case_hard_delete_on_an_unbackfilled_notebook,
    "bulk_delete": case_bulk_delete_removes_only_owned_rows,
    "store_creator_scope": case_store_delete_enforces_the_creator,
    "exit_requires_ack": case_self_exit_requires_the_exact_acknowledgement,
    "exit_purges": case_self_exit_purges_and_rejoin_starts_empty,
    "exit_without_memory": case_self_exit_with_nothing_to_delete_needs_no_acknowledgement,
    "exit_user_grant_keeps": case_self_exit_keeps_memory_while_a_user_grant_reads,
    "exit_group_grant_keeps": case_self_exit_keeps_memory_while_a_group_grant_reads,
    "exit_everyone_grant_keeps": case_self_exit_keeps_memory_while_an_everyone_grant_reads,
    "removal_by_others_keeps": case_removal_by_others_never_deletes_memory,
    "revocation_keeps": case_revoking_authorisation_keeps_memory,
    "delete_withdraws_promotion": case_delete_withdraws_active_promotion,
    "exit_withdraws_promotion": case_exit_withdraws_active_promotion,
    "approved_promotion_survives": case_approved_promotion_survives_deletion,
    "review_candidates_deleted": case_review_candidates_of_deleted_objects_are_deleted,
    "merge_candidates_real_writer": case_merge_candidates_written_by_fusion_are_deleted,
    "whole_memory_clusters_removed": case_whole_clusters_of_the_memory_are_removed,
    "deprecate_then_exit_clean": case_deprecate_then_exit_leaves_nothing_memory_named,
    "transfer_move_clean": case_transfer_move_leaves_nothing_memory_named,
    "one_page_strips_every_source": case_one_page_strips_every_merged_source,
    "live_bridge_cluster_keeps_candidates": case_a_live_cluster_with_the_bridge_id_keeps_its_candidates,
    "one_teardown_same_outcome": case_single_and_batched_teardown_have_the_same_outcome,
    "purge_refuses_document_source": case_the_memory_purge_refuses_a_document_source,
    "mixed_statuses": case_mixed_statuses_are_counted_exported_and_deleted_together,
    "export_scope": case_export_needs_read_access_and_is_lazy,
    "contract_200_counts": case_contract_a_finished_exit_reports_what_it_deleted,
    "contract_zero_claim_refuses_ack": case_contract_zero_claim_refuses_a_positive_acknowledgement,
    "exit_clears_member_overlay": case_a_finished_exit_clears_the_members_overlay,
}


def case_failed_purge_keeps_the_membership(world: World, monkeypatch) -> None:
    """A purge failure raises and leaves the leaver a member; the Memory's
    own projection is untouched by the detach that ran first."""
    source_ingestion = world.repo._runtime.source_ingestion

    def failing_remove(source_ids) -> int:
        raise RuntimeError("injected source removal failure")

    monkeypatch.setattr(source_ingestion, "remove_memory_sources", failing_remove)
    try:
        self_exit(world, world.alice, world.shared, 1)
    except MemberExitFailed as exc:
        assert (exc.deleted_memory_count, exc.memory_count) == (0, 1)
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("the exit swallowed a failed Memory purge")
    assert world.repo.is_member(world.shared, world.alice.id)
    assert_intact(world, "alice")


def case_a_purge_failing_part_way_reports_both_numbers(
    world: World, monkeypatch
) -> None:
    """Contract v2, 503: one page deleted, the next failed. The caller is told
    1 deleted and 1 left, is still a member, and the retry — which starts
    from the disclosure — finishes."""
    from app.services import memory_service as memory_service_module

    plain_memory(world, world.shared, world.alice, "contract-503", "confirmed")
    store = service(world).store
    original = store.bulk_delete_memories
    pages: list[int] = []

    def fail_second_page(user_id, memory_ids):
        pages.append(len(memory_ids))
        if len(pages) > 1:
            raise RuntimeError("injected failure on the second page")
        return original(user_id, memory_ids)

    monkeypatch.setattr(memory_service_module, "_PURGE_PAGE", 1)
    monkeypatch.setattr(store, "bulk_delete_memories", fail_second_page)
    try:
        self_exit(world, world.alice, world.shared, 2)
    except MemberExitFailed as exc:
        assert (exc.deleted_memory_count, exc.memory_count) == (1, 1)
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("a failed purge was reported as finished")
    assert world.repo.is_member(world.shared, world.alice.id)
    monkeypatch.undo()
    assert disclosure(world, world.alice, world.shared) == 1
    assert self_exit(world, world.alice, world.shared, 1) == 1
    assert not world.repo.is_member(world.shared, world.alice.id)
    assert member_memory_count(world, world.shared, world.alice) == 0


def case_a_failed_purge_counts_what_remains_on_the_server(
    world: World, monkeypatch
) -> None:
    """BM4 (fix2 re-review): the 503's remaining count is counted by the
    server, not claimed minus deleted: one page deleted, a Memory saved
    meanwhile, the next page failed — 1 deleted, 2 remain (1 claimed + 1
    new), still a member."""
    from app.services import memory_service as memory_service_module

    plain_memory(world, world.shared, world.alice, "bm4", "confirmed")
    store = service(world).store
    original = store.bulk_delete_memories
    pages: list[int] = []

    def save_then_fail(user_id, memory_ids):
        pages.append(len(memory_ids))
        if len(pages) > 1:
            plain_memory(world, world.shared, world.alice, "bm4-new", "candidate")
            raise RuntimeError("injected failure on the second page")
        return original(user_id, memory_ids)

    monkeypatch.setattr(memory_service_module, "_PURGE_PAGE", 1)
    monkeypatch.setattr(store, "bulk_delete_memories", save_then_fail)
    try:
        self_exit(world, world.alice, world.shared, 2)
    except MemberExitFailed as exc:
        assert (exc.deleted_memory_count, exc.memory_count) == (1, 2)
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("a failed purge was reported as finished")
    assert member_memory_count(world, world.shared, world.alice) == 2


def case_a_cleanup_failure_after_the_delete_counts_the_deleted_rows(
    world: World, monkeypatch
) -> None:
    """codex r1 #1: the Memory row's delete committed, then the second
    derived-row removal failed. The exit reports what really happened — 503
    with 1 deleted and 0 remaining, not 0 and 0 — the audit says 1, the
    leaver is still a member, and the retry (from the disclosure) finishes.
    A bulk delete failing the same way audits its committed rows and raises
    the real cause."""
    svc = service(world)
    original = svc._remove_derived_rows
    calls: list[int] = []

    def fail_after_the_delete(refs):
        calls.append(len(refs))
        if len(calls) in (2, 4):  # the exit's and the bulk delete's second call
            raise RuntimeError("injected cleanup failure after the row delete")
        return original(refs)

    events: list[dict] = []
    monkeypatch.setattr(
        svc.event_log, "emit", lambda event, **_kwargs: events.append(dict(event))
    )
    monkeypatch.setattr(svc, "_remove_derived_rows", fail_after_the_delete)
    try:
        self_exit(world, world.alice, world.shared, 1)
    except MemberExitFailed as exc:
        assert (exc.deleted_memory_count, exc.memory_count) == (1, 0)
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("a failed cleanup was reported as finished")
    assert world.repo.is_member(world.shared, world.alice.id)
    assert member_memory_count(world, world.shared, world.alice) == 0
    assert disclosure(world, world.alice, world.shared) == 0
    assert self_exit(world, world.alice, world.shared, 0) == 0
    assert not world.repo.is_member(world.shared, world.alice.id)

    home = world.projections["alice_home"].memory_id
    try:
        svc.bulk_delete(world.alice.id, [home])
    except RuntimeError as exc:
        assert "injected cleanup failure" in str(exc)
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("a failed cleanup was reported as finished")
    assert member_memory_count(world, world.alice_home, world.alice) == 0
    purges = [event for event in events if event.get("kind") == "memory_purge"]
    assert purges == [
        {"kind": "memory_purge", "action": "member_exit_partial",
         "notebook_id": world.shared, "user_id": world.alice.id, "count": 1},
        {"kind": "memory_purge", "action": "bulk_delete",
         "notebook_id": world.alice_home, "user_id": world.alice.id, "count": 1},
    ]


def case_claimed_memories_gone_meanwhile_finish_with_zero_deleted(
    world: World, monkeypatch
) -> None:
    """BM6 (fix2 re-review): every claimed Memory left by another path before
    the purge reached it (d = 0 while A = C > 0): the exit still finishes and
    reports 0 deleted by THIS request."""
    svc = service(world)
    alice = world.projections["alice"]
    original = svc._purge_page

    def moved_away_first(user_id, refs):
        svc.transfer(user_id, [alice.memory_id], world.alice_home, "move", extract_kg=False)
        return original(user_id, refs)

    monkeypatch.setattr(svc, "_purge_page", moved_away_first)
    assert self_exit(world, world.alice, world.shared, 1) == 0
    assert not world.repo.is_member(world.shared, world.alice.id)


def case_memory_saved_during_exit_is_never_deleted_unacknowledged(
    world: World, monkeypatch
) -> None:
    store = service(world).store
    original = store.bulk_delete_memories
    late: list[Projection] = []

    def delete_then_save(user_id, memory_ids):
        deleted = original(user_id, memory_ids)
        if not late:
            late.append(make_memory(world, "late", world.shared, world.alice))
        return deleted

    monkeypatch.setattr(store, "bulk_delete_memories", delete_then_save)
    try:
        self_exit(world, world.alice, world.shared, 1)
    except MemberExitIncomplete as exc:
        # The acknowledged one is gone and the caller is told so, next to
        # the one saved meanwhile that stays.
        assert (exc.deleted_memory_count, exc.memory_count) == (1, 1)
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("an incomplete exit was reported as finished")
    assert world.repo.is_member(world.shared, world.alice.id)
    assert_purged(world, "alice")
    assert_intact(world, "late")
    monkeypatch.undo()
    assert disclosure(world, world.alice, world.shared) == 1
    assert self_exit(world, world.alice, world.shared, 1) == 1
    assert not world.repo.is_member(world.shared, world.alice.id)
    assert_purged(world, "late")


def case_remove_and_readd_during_the_purge_keeps_the_new_membership(
    world: World, monkeypatch
) -> None:
    """S4 (quality review): while the purge runs, the owner removes the
    member and adds them back. The finish ends only the membership this exit
    claimed (same ``added_at``): the new membership, created after the
    claim, survives, with what is saved under it."""
    store = service(world).store
    original = store.bulk_delete_memories

    def delete_then_readd(user_id, memory_ids):
        deleted = original(user_id, memory_ids)
        world.repo.remove_member(world.shared, world.alice.id)
        world.repo.add_member(world.shared, world.alice.id)
        plain_memory(world, world.shared, world.alice, "readded", "candidate")
        _member_job_row(world, world.shared, world.alice)  # the new membership's
        return deleted

    monkeypatch.setattr(store, "bulk_delete_memories", delete_then_readd)
    assert self_exit(world, world.alice, world.shared, 1) == 1
    assert world.repo.is_member(world.shared, world.alice.id)
    assert_purged(world, "alice")
    assert member_memory_count(world, world.shared, world.alice) == 1
    # BM3: the exit did not end the new membership, so it must not clear the
    # new membership's per-member state either.
    assert _member_job_rows(world, world.shared, world.alice) == 1


def case_rejoin_after_the_membership_ends_keeps_new_memory(
    world: World, monkeypatch
) -> None:
    """Nothing after the membership delete may touch Memory: a rejoin that
    lands right after it keeps what is saved next."""
    sharing = world.repo._runtime.sharing
    original = sharing.forget_member_state

    def rejoin_then_forget(notebook_id, user_id):
        world.repo.add_member(notebook_id, user_id)
        make_memory(world, "after_rejoin", notebook_id, world.alice)
        original(notebook_id, user_id)

    monkeypatch.setattr(sharing, "forget_member_state", rejoin_then_forget)
    self_exit(world, world.alice, world.shared, 1)
    assert world.repo.is_member(world.shared, world.alice.id)
    assert_purged(world, "alice")
    assert_intact(world, "after_rejoin")


def case_purges_leave_a_content_free_audit_trail(world: World, monkeypatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr(
        service(world).event_log, "emit",
        lambda event, **_kwargs: events.append(dict(event)),
    )
    world.repo.bulk_delete_memories(
        world.alice.id,
        [world.projections["alice_home"].memory_id,
         world.projections["alice_elsewhere"].memory_id],
    )
    self_exit(world, world.alice, world.shared, 1)
    purges = [event for event in events if event.get("kind") == "memory_purge"]
    assert purges == [
        {"kind": "memory_purge", "action": "bulk_delete",
         "notebook_id": notebook_id, "user_id": world.alice.id, "count": 1}
        for notebook_id in sorted([world.alice_home, world.elsewhere])
    ] + [
        {"kind": "memory_purge", "action": "member_exit",
         "notebook_id": world.shared, "user_id": world.alice.id, "count": 1}
    ]


def case_statements_per_page_do_not_grow_with_sourceless_memories(
    world: World, monkeypatch
) -> None:
    """Memories without a derived source add no statements to an exit page."""
    counts = []
    for index, size in enumerate((3, 40)):
        user = world.repo.create_user(f"p0010010{index}", "pw123456")
        world.repo.add_member(world.shared, user.id)
        for number in range(size):
            plain_memory(world, world.shared, user, f"{index}-{number}", "confirmed")
        counts.append(count_statements(
            world, monkeypatch,
            lambda user=user, size=size: self_exit(world, user, world.shared, size),
        ))
        assert member_memory_count(world, world.shared, user) == 0
    assert counts[0] == counts[1] and counts[0] > 5, counts


def case_a_source_removed_concurrently_counts_as_removed(
    world: World, monkeypatch
) -> None:
    """The lookup found a source that a concurrent purge deleted before this
    removal ran: that is "already deleted", not a KeyError (formerly a 500)."""
    ingestion = world.repo._runtime.source_ingestion
    monkeypatch.setattr(
        ingestion.sources, "source_id_for_memory", lambda memory_id: "src-gone"
    )
    ingestion.remove_memory_source("memory-whatever")
    assert ingestion.remove_memory_sources(["src-gone"]) == 0
    assert_intact(world, "alice")


def case_a_source_published_just_before_the_row_delete_is_removed_after(
    world: World, monkeypatch
) -> None:
    """An in-flight KG ingest can publish a Memory's source after the first
    removal pass and before the row delete (its own post-ingest recheck
    already saw the Memory alive). The pass after the row delete removes it:
    no orphan source, on the single delete and on the bulk/exit path."""
    svc = service(world)
    ingestion = world.repo._runtime.source_ingestion
    store = svc.store

    def orphans() -> int:
        return world.sql.count(
            "SELECT COUNT(*) AS c FROM sources s WHERE s.source_type='memory' "
            "AND NOT EXISTS (SELECT 1 FROM memory_items m WHERE m.id=s.memory_id)"
        )

    single = plain_memory(world, world.shared, world.alice, "late-src-1", "confirmed")
    bulk = plain_memory(world, world.shared, world.alice, "late-src-2", "confirmed")
    for name in ("delete_memory", "bulk_delete_memories"):
        original = getattr(store, name)

        def publish_then_delete(user_id_or_id, *args, _original=original, _name=name):
            memory_id = user_id_or_id if _name == "delete_memory" else args[0][0]
            item = svc.get(memory_id, world.alice.id)
            ingestion.ingest_memory_source(
                item.notebook_id, item.id, item.title, item.content_md
            )
            assert ingestion.memory_source_id(item.id) is not None
            return _original(user_id_or_id, *args)

        monkeypatch.setattr(store, name, publish_then_delete)
    world.repo.delete_memory(single, world.alice.id)
    world.repo.bulk_delete_memories(world.alice.id, [bulk])
    assert ingestion.memory_source_id(single) is None
    assert ingestion.memory_source_id(bulk) is None
    assert orphans() == 0


MONKEYPATCH_CASES: dict[str, Callable[..., None]] = {
    "late_source_removed": case_a_source_published_just_before_the_row_delete_is_removed_after,
    "gone_source_is_removed": case_a_source_removed_concurrently_counts_as_removed,
    "failed_purge_keeps_membership": case_failed_purge_keeps_the_membership,
    "contract_503_counts": case_a_purge_failing_part_way_reports_both_numbers,
    "post_ingest_cleanup_clean": case_post_ingest_cleanup_leaves_nothing_memory_named,
    "failed_purge_counts_remaining": case_a_failed_purge_counts_what_remains_on_the_server,
    "cleanup_failure_counts_deleted": case_a_cleanup_failure_after_the_delete_counts_the_deleted_rows,
    "claimed_gone_zero_deleted": case_claimed_memories_gone_meanwhile_finish_with_zero_deleted,
    "save_during_exit_kept": case_memory_saved_during_exit_is_never_deleted_unacknowledged,
    "rejoin_keeps_new_memory": case_rejoin_after_the_membership_ends_keeps_new_memory,
    "remove_readd_keeps_membership": case_remove_and_readd_during_the_purge_keeps_the_new_membership,
    "audit_trail": case_purges_leave_a_content_free_audit_trail,
    "statements_bounded": case_statements_per_page_do_not_grow_with_sourceless_memories,
}
