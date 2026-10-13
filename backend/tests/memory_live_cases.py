"""Notebook-lifecycle visibility of Memory and of the self-service ask/report
reads, shared by both backends (K3).

``test_notebook_lifecycle_visibility.py`` runs these on SQLite and
``postgres/test_notebook_lifecycle_visibility_pg.py`` on PostgreSQL. A
notebook that is being deleted, copied or imported (``NOTEBOOK_LIVE_SQL``)
"does not exist yet / any more": its owner's and its members' Memory must not
come back from any Memory reader, the owner's own Memory writes there are
refused before anything is written (404), and its ask jobs and reports leave
the self-service activity overview and the bell. Flipping the notebook back
to ``ready`` makes everything readable again for the same people — the proof
that the predicate never hides anything in a live notebook.

Each reader is driven through the production method that issues the
statement (``repo._runtime.<component>.<method>``), never through a copy of
its SQL.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from tests.model_testkit import bind_all_embedding_clients

EMBED_DIM = 16
NON_LIVE_STATES = ("deleting", "copying", "importing")
_NOW = "2026-10-01T00:00:00+00:00"


@dataclass
class World:
    repo: Any
    postgres: bool
    owner: Any
    member: Any
    notebook_id: str
    # per user id: (candidate memory id, confirmed memory id)
    memories: dict[str, tuple[str, str]]
    ask_ids: dict[str, str]
    report_ids: dict[str, str]

    def sql(self, text: str) -> str:
        return text.replace("?", "%s") if self.postgres else text

    def write(self, text: str, params: tuple = ()) -> None:
        with self.repo._runtime.database.write() as db:
            db.execute(self.sql(text), params)

    def one(self, text: str, params: tuple = ()) -> Any:
        with self.repo._runtime.database.connect() as db:
            return db.execute(self.sql(text), params).fetchone()

    def set_status(self, status: str) -> None:
        self.write("UPDATE notebooks SET status=? WHERE id=?", (status, self.notebook_id))


def _as_user(user: Any, fn: Callable[[], Any]) -> Any:
    token = set_request_user(user)
    try:
        return fn()
    finally:
        reset_request_user(token)


def build_world(repo: Any, *, postgres: bool) -> World:
    bind_all_embedding_clients(repo, FakeEmbedder(dim=EMBED_DIM))
    service = repo._runtime.memory_service
    # Embeddings are written inline so the vector arm has rows; no KG ingest
    # (the ingest job has its own case below).
    service.kg_ingest_scheduler = lambda fn, item: None
    service.embedding_scheduler = lambda fn, job: fn(job)
    owner = repo._runtime.identity.create_user("o00300001", "pw123456")
    member = repo._runtime.identity.create_user("m00300002", "pw123456")
    notebook_id = _as_user(
        owner, lambda: repo.create_notebook(NotebookCreate(name="K3 live"))
    ).id
    repo.add_member(notebook_id, member.id)
    world = World(repo, postgres, owner, member, notebook_id, {}, {}, {})
    for user in (owner, member):
        tag = user.id[-12:]
        candidate = service.create_candidate(
            notebook_id, user.id, None, f"req-cand-{tag}", f"Candidate {tag}",
            f"candidate body {tag} walrus", [], "reason", {}, [],
        )
        confirmed = service.create_candidate(
            notebook_id, user.id, None, f"req-conf-{tag}", f"Confirmed {tag}",
            f"confirmed body {tag} zebra", [], "reason", {}, [],
        )
        service.confirm(confirmed.id, user.id)
        world.write(
            "UPDATE memory_items SET source_answer_id=? WHERE id=?",
            (f"ans-{tag}", confirmed.id),
        )
        world.memories[user.id] = (candidate.id, confirmed.id)
        world.ask_ids[user.id] = f"ask-k3-{tag}"
        world.report_ids[user.id] = f"rep-k3-{tag}"
        world.write(
            "INSERT INTO ask_jobs (id,notebook_id,created_by,mode,question,status,"
            "asked_at,answer_id,error,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (world.ask_ids[user.id], notebook_id, user.id, "chunk", "q?",
             "completed", "", "", "", _NOW, _NOW),
        )
        world.write(
            "INSERT INTO reports (id,notebook_id,question,depth,status,created_by,"
            "understanding_json,created_at,updated_at,submitted_via) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (world.report_ids[user.id], notebook_id, "report?", 2, "outline_ready",
             user.id, "{}", _NOW, _NOW, "web"),
        )
    return world


# --------------------------------------------------------------- the readers
def _found(fn: Callable[[], Any]) -> bool:
    try:
        fn()
    except KeyError:
        return False
    return True


def _promotion_gate_passed(world: World, user_id: str, memory_id: str) -> bool:
    """``propose_promotion_on`` (FOR UPDATE read): a visible row reaches the
    snapshot check (ValueError on a deliberately stale snapshot, nothing
    written); a hidden one is a KeyError before it."""
    store = world.repo._runtime.memory_store
    stale = store.memory_for_user(memory_id, user_id, live_only=False)
    stale = stale.model_copy(update={"title": stale.title + " (stale)"})
    try:
        with world.repo._runtime.database.write() as db:
            store.propose_promotion_on(
                db, memory_id, user_id, "proposal-k3", [], [], stale, _NOW
            )
    except KeyError:
        return False
    except ValueError as exc:
        assert "changed before its promotion snapshot" in str(exc), exc
        return True
    raise AssertionError("a stale snapshot must never be promoted")


def reader_results(world: World, user: Any) -> dict[str, bool]:
    """Whether each Memory / ask / report reader returns this user's rows."""
    runtime = world.repo._runtime
    store, queries = runtime.memory_store, runtime.queries
    candidate_id, confirmed_id = world.memories[user.id]
    tag = user.id[-12:]
    nb = world.notebook_id
    page = store.list_memories(
        user.id, notebook_id=None, status=None, origin=None, query="",
        offset=0, limit=50,
    )
    searched = store.list_memories(
        user.id, notebook_id=None, status=None, origin=None, query="zebra",
        offset=0, limit=50,
    )
    # "walrus" is only in the candidate, which is never embedded: only the
    # lexical arm can return it, so the vector arm cannot mask this cell.
    lexical = store.memory_retrieval_rows(
        user.id, nb, ("candidate", "confirmed"), "walrus",
        lexical_limit=10, vector_limit=1,
    )
    vector_only = store.memory_retrieval_rows(
        user.id, nb, ("candidate", "confirmed"), "nothing-matches-this",
        lexical_limit=10, vector_limit=10,
    )
    asks = queries.list_user_activity(user.id, activity_type="ask")
    reports = queries.list_user_activity(user.id, activity_type="report")
    bell = queries.pending_actions_projection_rows(user.id)
    return {
        "memory_for_user": _found(lambda: store.memory_for_user(confirmed_id, user.id)),
        "revisions_for_user": _found(
            lambda: store.revisions_for_user(candidate_id, user.id)
        ),
        "answer_memory_links": store.answer_memory_links(nb, user.id, [f"ans-{tag}"])
        == {f"ans-{tag}": confirmed_id},
        "propose_promotion_on": _promotion_gate_passed(world, user.id, confirmed_id),
        "export_snapshot": bool(
            store.memory_export_snapshot(nb, user.id, page_size=50, max_bytes=10**6)
        ),
        "list_memories.items": {item.id for item in page.items}
        == {candidate_id, confirmed_id},
        "list_memories.count": page.total_count == 2,
        "list_memories.owner_aggregate": page.owner_total_count == 2
        and page.owner_pending_count == 1,
        "list_memories.notebook_options": [o.notebook_id for o in page.notebook_options]
        == [nb],
        # PG: count and page candidates come from postgres/search.py's own
        # predicate (`_memory_match_predicates`), not from the row query.
        "list_memories.search.items": [item.id for item in searched.items]
        == [confirmed_id],
        "list_memories.search.count": searched.total_count == 1,
        "memory_retrieval_rows.lexical": candidate_id
        in {row["record"].id for row in lexical},
        # Only confirmed Memory is embedded, so the vector arm returns it alone.
        "memory_retrieval_rows.vector": {row["record"].id for row in vector_only}
        == {confirmed_id},
        "activity.ask_only": world.ask_ids[user.id]
        in {item["id"] for item in asks["items"]},
        "activity.report_only": world.report_ids[user.id]
        in {item["id"] for item in reports["items"]},
        "bell.report_outline": world.report_ids[user.id]
        in {item.get("report_id") for item in bell["items"]},
    }


def assert_readers_follow_lifecycle(world: World, state: str) -> None:
    for user in (world.owner, world.member):
        before = reader_results(world, user)
        assert all(before.values()), {k: v for k, v in before.items() if not v}
    world.set_status(state)
    for user in (world.owner, world.member):
        hidden = reader_results(world, user)
        assert not any(hidden.values()), {k: v for k, v in hidden.items() if v}
    world.set_status("ready")
    for user in (world.owner, world.member):
        again = reader_results(world, user)
        assert all(again.values()), {k: v for k, v in again.items() if not v}


# ---------------------------------------------------------------- the writes
def _memory_row(world: World, memory_id: str) -> Any:
    row = world.one(
        "SELECT title,status,updated_at FROM memory_items WHERE id=?", (memory_id,)
    )
    return None if row is None else (row["title"], row["status"], str(row["updated_at"]))


def _revision_count(world: World, memory_id: str) -> int:
    return int(
        world.one(
            "SELECT COUNT(*) AS c FROM memory_revisions WHERE memory_id=?", (memory_id,)
        )["c"]
    )


def assert_writes_refused_then_allowed(world: World, state: str) -> None:
    service = world.repo._runtime.memory_service
    store = world.repo._runtime.memory_store
    user = world.owner
    candidate_id, confirmed_id = world.memories[user.id]
    removed: list[str] = []

    class _Kg:
        def memory_source_id(self, memory_id):
            return None

        def memory_kg_eligible(self, notebook_id):
            return False

        def remove_memory_source(self, memory_id):
            removed.append(memory_id)

    service.set_memory_kg_service(_Kg())
    snapshot = {
        memory_id: (_memory_row(world, memory_id), _revision_count(world, memory_id))
        for memory_id in (candidate_id, confirmed_id)
    }
    world.set_status(state)
    refused = {
        "update": lambda: service.update(candidate_id, user.id, {"title": "edited"}),
        "confirm": lambda: service.confirm(candidate_id, user.id),
        "reject": lambda: service.reject(candidate_id, user.id),
        "deprecate": lambda: service.deprecate(confirmed_id, user.id),
        "delete": lambda: service.delete(confirmed_id, user.id),
        "propose_promotion": lambda: service.propose_promotion(confirmed_id, user.id),
        "store.update_fields": lambda: store.update_fields(
            candidate_id, user.id, {"title": "edited"}
        ),
        "store.transition": lambda: store.transition(
            candidate_id, user.id, {"candidate"}, "rejected"
        ),
    }
    for name, call in refused.items():
        with pytest.raises(KeyError):
            call()
        after = {
            memory_id: (_memory_row(world, memory_id), _revision_count(world, memory_id))
            for memory_id in (candidate_id, confirmed_id)
        }
        assert after == snapshot, f"{name} wrote in a {state} notebook"
    assert removed == []
    world.set_status("ready")
    edited = service.update(candidate_id, user.id, {"title": "edited"})
    assert edited.title == "edited"
    assert service.confirm(candidate_id, user.id).status == "confirmed"
    assert service.deprecate(confirmed_id, user.id).status == "deprecated"
    assert removed == [confirmed_id]


# ----------------------------------------------------- the KG ingest recheck
class _IngestKg:
    """Duck-typed memory-KG service whose ingest flips the notebook mid-run."""

    def __init__(self, world: World, flip_to: str | None) -> None:
        self.world = world
        self.flip_to = flip_to
        self.calls: list[tuple[str, str]] = []

    def memory_kg_eligible(self, notebook_id):
        return True

    def memory_source_id(self, memory_id):
        return None

    def ingest_memory_source(self, notebook_id, memory_id, title, content_md):
        self.calls.append(("ingest", memory_id))
        if self.flip_to is not None:
            self.world.set_status(self.flip_to)
        return f"src-{memory_id}"

    def remove_memory_source(self, memory_id):
        self.calls.append(("remove", memory_id))


def assert_ingest_recheck_survives_lifecycle_flip(world: World, state: str) -> None:
    """``_kg_ingest_job``'s post-ingest recheck asks "is this Memory still
    there and confirmed", not "may I read it in a live notebook": a notebook
    turning non-live during the ingest must NOT read as "Memory gone" and
    start a derived-source cleanup racing the notebook delete job."""
    service = world.repo._runtime.memory_service
    user = world.owner
    _candidate_id, confirmed_id = world.memories[user.id]

    kg = _IngestKg(world, flip_to=state)
    service.set_memory_kg_service(kg)
    service._kg_ingest_job((confirmed_id, user.id))
    assert kg.calls == [("ingest", confirmed_id)]

    # Already non-live when the job starts: nothing is ingested into it.
    kg = _IngestKg(world, flip_to=None)
    service.set_memory_kg_service(kg)
    service._kg_ingest_job((confirmed_id, user.id))
    assert kg.calls == []

    # Control: a Memory that really is gone after the ingest is still cleaned.
    world.set_status("ready")
    kg = _IngestKg(world, flip_to=None)
    original = kg.ingest_memory_source

    def ingest_then_deprecate(notebook_id, memory_id, title, content_md):
        result = original(notebook_id, memory_id, title, content_md)
        world.write(
            "UPDATE memory_items SET status='deprecated' WHERE id=?", (memory_id,)
        )
        return result

    kg.ingest_memory_source = ingest_then_deprecate
    service.set_memory_kg_service(kg)
    service._kg_ingest_job((confirmed_id, user.id))
    assert kg.calls == [("ingest", confirmed_id), ("remove", confirmed_id)]


# ------------------------------------------- the read after a landed write
def assert_post_write_read_survives_lifecycle_flip(world: World, state: str) -> None:
    """A revision write checks the notebook is live under its lock and then
    commits; a delete (or copy/import) flip committing after that must not
    turn the landed write into a 404 and skip what follows it: the derived
    source removal on deprecate, the KG ingest and embedding on confirm.

    The flip is committed in the write's own transaction, right after the
    lifecycle check, which is exactly what the read after the commit sees
    when a delete's status CAS lands between the two."""
    service = world.repo._runtime.memory_service
    store = world.repo._runtime.memory_store
    user = world.owner
    candidate_id, confirmed_id = world.memories[user.id]
    removed: list[str] = []
    ingests: list[Any] = []
    embeds: list[Any] = []

    class _Kg:
        def memory_source_id(self, memory_id):
            return None

        def memory_kg_eligible(self, notebook_id):
            return True

        def remove_memory_source(self, memory_id):
            removed.append(memory_id)

    service.set_memory_kg_service(_Kg())
    service.kg_ingest_scheduler = lambda fn, item: ingests.append(item)
    service.embedding_scheduler = lambda fn, job: embeds.append(job)
    original = store._append_revision_on

    def append_then_flip(db, memory_id, *args, **kwargs):
        result = original(db, memory_id, *args, **kwargs)
        db.execute(
            world.sql("UPDATE notebooks SET status=? WHERE id=?"),
            (state, world.notebook_id),
        )
        return result

    store._append_revision_on = append_then_flip
    try:
        deprecated = service.deprecate(confirmed_id, user.id)
        assert deprecated.status == "deprecated"
        assert removed == [confirmed_id]
        world.set_status("ready")
        confirmed = service.confirm(candidate_id, user.id)
        assert confirmed.status == "confirmed"
        assert [item[0] for item in ingests] == [candidate_id]
        assert embeds, "confirm must still queue the embedding"
    finally:
        del store._append_revision_on
    world.set_status("ready")


# ------------------------------------------------ the curator's approval
def assert_promotion_approval_refused_in_non_live(world: World, state: str) -> None:
    """Approving a Memory promotion copies it into a base library: a notebook
    being deleted/copied/imported refuses it (KeyError -> the route's 404)
    without writing anything; back to ready, the same proposal approves."""
    repo = world.repo
    runtime = repo._runtime
    user = world.owner
    _candidate_id, confirmed_id = world.memories[user.id]
    base = _as_user(
        user, lambda: repo.create_notebook(NotebookCreate(name="K3 base"))
    ).id
    runtime.catalog.mark_notebook_base(base)
    runtime.notebook_store.replace_mounts(world.notebook_id, [base], user.id)
    proposal = runtime.memory_service.propose_promotion(confirmed_id, user.id)["id"]

    def base_objects() -> int:
        return int(world.one(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?", (base,)
        )["c"])

    def candidate_status() -> str:
        return str(world.one(
            "SELECT status FROM promotion_candidates WHERE id=?", (proposal,)
        )["status"])

    before = (base_objects(), candidate_status(), _memory_row(world, confirmed_id))
    world.set_status(state)
    with pytest.raises(KeyError):
        runtime.knowledge_governance.approve_promotion(proposal, reviewer_id=user.id)
    assert (base_objects(), candidate_status(), _memory_row(world, confirmed_id)) == before
    assert before[0] == 0
    world.set_status("ready")
    runtime.knowledge_governance.approve_promotion(proposal, reviewer_id=user.id)
    assert candidate_status() == "approved"
    assert base_objects() >= 1
