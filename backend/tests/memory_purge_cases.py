"""Memory hard delete / member exit purge scenarios, shared by both backends.

``test_memory_purge.py`` runs every case on SQLite and
``postgres/test_memory_purge_pg.py`` runs the same cases on PostgreSQL. Each
case drives the production facade (``delete_memory``, ``bulk_delete_memories``,
``leave_notebook``, ``remove_member``, ``kick_all_members``) against a world
whose Memory projections are REAL: the hidden source, its element and
extraction run come from the offline ingest pipeline, and its KG rows come
from ``store_kg`` with the source's running generation (objects, relations,
source-local facts, reverse index, embeddings), plus one cluster member row.

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

from app.models.knowledge import MergeRequest
from app.models.schemas import NotebookCreate, SourceImportFile, SourceImportRequest
from app.services.embedding import FakeEmbedder
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


def make_memory(world: World, key: str, notebook_id: str, user: Any) -> Projection:
    """A confirmed Memory with a real derived source and a full KG projection."""
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
                "payload": {"name": f"{key} loop", "section_path": ""},
                "evidence": evidence,
            },
            {
                "local_id": "b", "object_type": "concept",
                "payload": {"name": f"{key} compensation", "section_path": ""},
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
    sql.write(
        "INSERT INTO concept_clusters "
        "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
        "VALUES (?,?,?,?,?,'concept',?)",
        (
            f"cc-{key}", notebook_id, f"canon-{key}", object_ids[0], f"{key} loop",
            _GRANT_CREATED_AT,
        ),
    )
    projection = Projection(
        memory.id, notebook_id, source_id, element_ids, object_ids, relation_ids
    )
    world.projections[key] = projection
    return projection


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
    # N-1: a curator manually merges Alice's Memory-derived object into the
    # shared document object; the shared object now also cites her Memory.
    alice_projection = world.projections["alice"]
    repo.merge_knowledge(
        shared,
        alice_projection.object_ids[0],
        MergeRequest(into_id=doc_object),
    )
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


def case_leave_purges_member_memory_and_rejoin_starts_empty(world: World) -> None:
    repo = world.repo
    seq = mark_clean(world, world.shared)
    repo.leave_notebook(world.shared, world.alice.id)
    assert not repo.is_member(world.shared, world.alice.id)
    assert_purged(world, "alice")
    assert_shared_object_stripped(world)
    assert_marked_dirty(world, world.shared, seq)
    assert_intact(world, "bob")
    assert_intact(world, "alice_home")
    assert_intact(world, "alice_elsewhere")
    repo.add_member(world.shared, world.alice.id)
    page = repo.list_memories(world.alice.id, notebook_id=world.shared)
    assert page.items == []
    assert_purged(world, "alice")


def case_remove_member_keeps_memory_while_a_grant_still_reads(world: World) -> None:
    grant_user(world, world.shared, world.alice)
    world.repo.remove_member(world.shared, world.alice.id)
    assert not world.repo.is_member(world.shared, world.alice.id)
    assert_intact(world, "alice")
    assert world.repo.get_memory(
        world.projections["alice"].memory_id, world.alice.id
    ).id == world.projections["alice"].memory_id


def case_revoking_authorisation_keeps_memory(world: World) -> None:
    """Grant withdrawal and unsharing are not exits: nothing is deleted."""
    repo = world.repo
    alice = world.projections["alice"]
    grant_id = grant_user(world, world.shared, world.alice)
    repo.remove_member(world.shared, world.alice.id)  # grant still reads
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


def case_kick_all_members_purges_each_exiting_member(world: World) -> None:
    grant_user(world, world.shared, world.bob)  # Bob keeps reading
    world.repo.kick_all_members(world.shared)
    assert not world.repo.is_member(world.shared, world.alice.id)
    assert not world.repo.is_member(world.shared, world.bob.id)
    assert_purged(world, "alice")
    assert_shared_object_stripped(world)
    assert_intact(world, "bob")
    assert_intact(world, "alice_home")
    assert_intact(world, "alice_elsewhere")


def case_failed_purge_keeps_the_membership(world: World, monkeypatch) -> None:
    """Clean first, then drop the row: a purge failure leaves a member."""
    source_ingestion = world.repo._runtime.source_ingestion

    def failing_remove(memory_id: str) -> None:
        raise RuntimeError("injected source removal failure")

    monkeypatch.setattr(source_ingestion, "remove_memory_source", failing_remove)
    try:
        world.repo.leave_notebook(world.shared, world.alice.id)
    except RuntimeError as exc:
        assert "injected" in str(exc)
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("leave_notebook swallowed a failed Memory purge")
    assert world.repo.is_member(world.shared, world.alice.id)
    # Nothing of the Memory's own projection was touched: the pre-removal
    # detach only reaches objects ANOTHER source owns.
    assert_intact(world, "alice")


def case_memory_saved_during_exit_is_swept(world: World, monkeypatch) -> None:
    """A Memory landing between the purge and the row delete is swept after."""
    repo = world.repo
    sharing_store = repo._runtime.sharing_store
    original = sharing_store.remove_member
    late: list[Projection] = []

    def remove_after_a_late_save(notebook_id: str, user_id: str) -> None:
        late.append(make_memory(world, "late", notebook_id, world.alice))
        original(notebook_id, user_id)

    monkeypatch.setattr(sharing_store, "remove_member", remove_after_a_late_save)
    repo.leave_notebook(world.shared, world.alice.id)
    assert late
    assert_purged(world, "alice")
    counts = derived_counts(world, world.projections["late"])
    assert counts["memory_items"] == 0
    assert counts["sources"] == 0
    assert counts["knowledge_objects"] == 0
    assert_intact(world, "bob")


def case_delete_withdraws_active_promotion(world: World) -> None:
    candidate_id = propose(world, "alice_home")
    world.repo.delete_memory(world.projections["alice_home"].memory_id, world.alice.id)
    assert promotion_state(world, candidate_id) == ("rejected", "withdrawn_memory_deleted")
    assert_purged(world, "alice_home")


def case_exit_withdraws_active_promotion(world: World) -> None:
    candidate_id = propose(world, "alice")
    world.repo.remove_member(world.shared, world.alice.id)
    assert promotion_state(world, candidate_id) == ("rejected", "withdrawn_memory_deleted")
    assert_purged(world, "alice")
    assert_intact(world, "bob")
    assert_intact(world, "alice_elsewhere")


CASES: dict[str, Callable[..., None]] = {
    "hard_delete": case_hard_delete_removes_every_derived_row,
    "hard_delete_unbackfilled": case_hard_delete_on_an_unbackfilled_notebook,
    "bulk_delete": case_bulk_delete_removes_only_owned_rows,
    "leave": case_leave_purges_member_memory_and_rejoin_starts_empty,
    "grant_still_reads": case_remove_member_keeps_memory_while_a_grant_still_reads,
    "revocation_keeps": case_revoking_authorisation_keeps_memory,
    "kick_all": case_kick_all_members_purges_each_exiting_member,
    "delete_withdraws_promotion": case_delete_withdraws_active_promotion,
    "exit_withdraws_promotion": case_exit_withdraws_active_promotion,
}
MONKEYPATCH_CASES: dict[str, Callable[..., None]] = {
    "failed_purge_keeps_membership": case_failed_purge_keeps_the_membership,
    "late_save_swept": case_memory_saved_during_exit_is_swept,
}
