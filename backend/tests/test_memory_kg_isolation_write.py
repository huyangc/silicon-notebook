"""E4-3 / M1 写侧:个人记忆派生的知识对象不进共享簇、不与任何对象合并(SQLite)。

计划 §5 E4-3 的验收,逐条对应:

* 确认一条 Memory 后,增量融合(真实抽取收尾 `run_extraction` → `incremental_fuse_source`)
  不把它放进任何共享簇、不产生合并候选;scale fold 走的是同一个方法,直调同样不动。
* 手工合并跨类返回 409(真实路由,策展文案 + `X-User-Message`),store 层直调同样拒绝,
  且拒绝时一行不写。同一成员的两个 Memory 对象同样拒绝(判定与理由见
  `app.domain.memory_kg_isolation`)。
* N-1 回归:先尝试把 Memory 对象并进共享对象(被拒),再删除该 Memory —— 共享对象一个
  不少、状态不变。去掉 store 内的判定,这条必须红(合并成功后删除 Memory 会按证据引用把
  共享对象一起删掉)。
* 冲突候选不含 Memory 对象或关系:见 `test_resolve_notebook_conflicts.py` 的扩展用例。
* Tier2 候选池剔除 Memory 派生 concept:见 `test_incremental_fusion.py` 的扩展用例。

PostgreSQL 孪生在 `postgres/test_memory_kg_isolation_write_pg.py`。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.memory_kg_isolation import (
    CROSS_CLASS_MESSAGE,
    SAME_OWNER_MESSAGE,
    MemoryKnowledgeMergeRefused,
)
from app.models.knowledge import MergeRequest
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client

NOW = "2026-09-29T00:00:00+00:00"
TEXT = "Engram is a memory architecture."


class _KgExtractLLM:
    """Every source extracts the same concept + claim — so without the M1 guard
    the Memory's concept lands in the shared concept's name-seed cluster."""

    configured = True

    def chat_json(self, messages, response_schema_hint=None, **_kwargs) -> str:
        return json.dumps({
            "nodes": [
                {"local_id": "c", "type": "Concept", "name": "Engram",
                 "evidence": "Engram is a memory architecture"},
                {"local_id": "k", "type": "Claim", "name": "Engram is a memory architecture",
                 "evidence": "Engram is a memory architecture"},
            ],
            "edges": [],
        })


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    bind_chat_client(r, "kg_extract", _KgExtractLLM())
    return r


def _user(repo, uid: str) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (uid, f"{uid}@example.test", uid, "user", "active", NOW, NOW),
        )


def _upload_source(repo, notebook_id: str, source_id: str, text: str = TEXT) -> str:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, status, parse_status, "
            "file_name, file_path, file_size, file_hash, summary, doc_type, created_at, updated_at) "
            "VALUES (?, ?, ?, 'markdown', 'extracted', 'parsed', ?, '', 0, '', '', "
            "'academic_paper', ?, ?)",
            (source_id, notebook_id, source_id, f"{source_id}.md", NOW, NOW),
        )
        db.execute(
            "INSERT INTO source_elements (id, source_id, element_type, location_label, text, "
            "metadata, created_at) VALUES (?, ?, 'paragraph', 'p1', ?, '{}', ?)",
            (f"el-{source_id}", source_id, text, NOW),
        )
    repo._run_extraction(source_id)
    return source_id


def _confirmed_memory(repo, notebook_id: str, memory_id: str, owner: str) -> str:
    """A confirmed Memory, ingested through the REAL pipeline
    (`ingest_memory_source` → parse → `run_extraction` → store_kg →
    `incremental_fuse_source`). Returns its synthetic source id."""
    with repo._write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (memory_id, notebook_id, owner, "ask_answer", "confirmed", memory_id,
             TEXT, NOW, NOW),
        )
    source_id = repo._runtime.source_ingestion.ingest_memory_source(
        notebook_id, memory_id, memory_id, TEXT
    )
    assert source_id is not None
    assert repo._runtime.source_ingestion.sources.get_source(source_id).parse_status == "extracted"
    return source_id


def _objects(repo, notebook_id: str, source_id: str) -> dict[str, dict]:
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, object_type, status, evidence FROM knowledge_objects "
            "WHERE notebook_id=? AND source_id=?",
            (notebook_id, source_id),
        ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def _concept(objs: dict[str, dict]) -> str:
    (oid,) = [o for o, row in objs.items() if row["object_type"] == "concept"]
    return oid


@pytest.fixture
def world(repo):
    _user(repo, "u-a")
    _user(repo, "u-b")
    nb = repo.create_notebook(NotebookCreate(name="shared"))
    upload = _upload_source(repo, nb.id, "src-upload")
    return repo, nb.id, upload


def test_confirmed_memory_does_not_enter_a_shared_cluster(world):
    repo, nb, upload = world
    shared = _objects(repo, nb, upload)
    shared_concept = _concept(shared)
    cmap = repo.cluster_map(nb)
    assert shared_concept in cmap  # the shared concept did fuse (control)
    shared_cluster = cmap[shared_concept]

    memory_source = _confirmed_memory(repo, nb, "mem-a", "u-a")
    memory = _objects(repo, nb, memory_source)
    assert {row["object_type"] for row in memory.values()} == {"concept", "claim"}

    def assert_isolated() -> None:
        with repo._connect() as db:
            member_rows = db.execute(
                "SELECT member_object_id, canonical_id FROM concept_clusters "
                "WHERE notebook_id=?", (nb,),
            ).fetchall()
            candidates = db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=?", (nb,),
            ).fetchall()
        members = {r["member_object_id"] for r in member_rows}
        # No cluster row at all for any Memory-derived object (singleton, the
        # readers' COALESCE path) — so in particular not in the shared cluster.
        assert members.isdisjoint(memory), member_rows
        in_shared = {
            r["member_object_id"] for r in member_rows if r["canonical_id"] == shared_cluster
        }
        assert in_shared == {shared_concept}
        assert [tuple(r) for r in candidates] == []

    assert_isolated()
    # The scale fold calls the very same method per delta source.
    repo.incremental_fuse_source(nb, memory_source)
    assert_isolated()


def test_memory_source_fusion_issues_no_cluster_write(world, monkeypatch):
    """The entry return is BEFORE any work: no orphan sweep, no append."""
    repo, nb, _upload = world
    memory_source = _confirmed_memory(repo, nb, "mem-a", "u-a")
    lifecycle = repo._runtime.knowledge_lifecycle
    touched: list[str] = []
    monkeypatch.setattr(
        lifecycle, "append_clusters",
        lambda *a, **k: touched.append("append_clusters") or 0,
    )
    monkeypatch.setattr(
        lifecycle.knowledge, "incremental_object_rows",
        lambda *a, **k: touched.append("incremental_object_rows") or [],
    )
    lifecycle.incremental_fuse_source(nb, memory_source)
    assert touched == []


# --------------------------------------------------------------------------
# Manual merge (N-1)
# --------------------------------------------------------------------------

def _merge_in_store(repo, nb: str, source_id: str, into_id: str, actor_id="u-a"):
    store = repo._runtime.governance
    with repo._write() as db:
        return store.merge_objects_in_transaction(
            db, nb, source_id, into_id, NOW, actor_id=actor_id
        )


def _snapshot(repo, nb: str) -> dict:
    with repo._connect() as db:
        return {
            r["id"]: (r["status"], r["evidence"])
            for r in db.execute(
                "SELECT id, status, evidence FROM knowledge_objects WHERE notebook_id=?",
                (nb,),
            ).fetchall()
        }


@pytest.mark.parametrize("direction", ["memory_into_shared", "shared_into_memory"])
def test_store_refuses_merge_across_memory_and_shared(world, direction):
    repo, nb, upload = world
    shared_concept = _concept(_objects(repo, nb, upload))
    memory_concept = _concept(_objects(repo, nb, _confirmed_memory(repo, nb, "mem-a", "u-a")))
    source_id, into_id = (
        (memory_concept, shared_concept)
        if direction == "memory_into_shared"
        else (shared_concept, memory_concept)
    )
    before = _snapshot(repo, nb)
    with pytest.raises(MemoryKnowledgeMergeRefused) as refused:
        _merge_in_store(repo, nb, source_id, into_id)
    assert refused.value.user_message == CROSS_CLASS_MESSAGE
    assert _snapshot(repo, nb) == before  # nothing written


def test_store_treats_another_members_memory_as_missing(world):
    """F3: whoever calls, the OTHER member's Memory object is "not found" —
    the same KeyError as an unknown id, so the merge route is no existence
    probe; nothing is written."""
    repo, nb, upload = world
    shared = _concept(_objects(repo, nb, upload))
    a = _concept(_objects(repo, nb, _confirmed_memory(repo, nb, "mem-a", "u-a")))
    b = _concept(_objects(repo, nb, _confirmed_memory(repo, nb, "mem-b", "u-b")))
    before = _snapshot(repo, nb)
    for actor, source_id, into_id in (
        ("u-a", a, b), ("u-a", b, a), ("u-b", a, b),
        ("u-a", b, shared), ("u-a", shared, b), ("u-c", a, shared),
        (None, a, shared),
    ):
        with pytest.raises(KeyError) as missing:
            _merge_in_store(repo, nb, source_id, into_id, actor_id=actor)
        assert not isinstance(missing.value, MemoryKnowledgeMergeRefused)
    with pytest.raises(KeyError) as unknown:
        _merge_in_store(repo, nb, "ko-does-not-exist", shared)
    assert type(unknown.value) is KeyError
    assert _snapshot(repo, nb) == before


def test_store_refuses_merge_between_one_members_two_memories(world):
    repo, nb, _upload = world
    a1 = _concept(_objects(repo, nb, _confirmed_memory(repo, nb, "mem-a1", "u-a")))
    a2 = _concept(_objects(repo, nb, _confirmed_memory(repo, nb, "mem-a2", "u-a")))
    before = _snapshot(repo, nb)
    with pytest.raises(MemoryKnowledgeMergeRefused) as refused:
        _merge_in_store(repo, nb, a1, a2)
    assert refused.value.user_message == SAME_OWNER_MESSAGE
    assert _snapshot(repo, nb) == before


def test_store_treats_an_orphan_memory_source_as_nobodys(world):
    """An orphan Memory source (memory row gone) is still Memory-derived and
    has no owner: "not found" for everyone, never waved through."""
    repo, nb, _upload = world
    a = _concept(_objects(repo, nb, _confirmed_memory(repo, nb, "mem-a", "u-a")))
    orphan = _concept(_objects(repo, nb, _confirmed_memory(repo, nb, "mem-o", "u-a")))
    with repo._write() as db:
        db.execute("UPDATE sources SET memory_id=NULL WHERE memory_id='mem-o'")
    before = _snapshot(repo, nb)
    with pytest.raises(KeyError):
        _merge_in_store(repo, nb, a, orphan)
    assert _snapshot(repo, nb) == before


def test_shared_objects_still_merge(world):
    repo, nb, upload = world
    other = _upload_source(repo, nb, "src-upload-2")
    a = _concept(_objects(repo, nb, upload))
    b = _concept(_objects(repo, nb, other))
    row = repo.merge_knowledge(nb, a, MergeRequest(into_id=b))
    assert row.id == b
    assert _snapshot(repo, nb)[a][0] == "deprecated"


def test_service_merge_surfaces_the_refusal_and_marks_nothing_dirty(world):
    repo, nb, upload = world
    shared_concept = _concept(_objects(repo, nb, upload))
    memory_concept = _concept(_objects(repo, nb, _confirmed_memory(repo, nb, "mem-a", "u-a")))
    with repo._connect() as db:
        seq_before = tuple(repo._runtime.unified_kg.graph_seq_row(db, nb))
    with pytest.raises(MemoryKnowledgeMergeRefused):
        repo.merge_knowledge(
            nb, memory_concept, MergeRequest(into_id=shared_concept), actor_id="u-a"
        )
    with pytest.raises(KeyError):
        repo.merge_knowledge(
            nb, memory_concept, MergeRequest(into_id=shared_concept), actor_id="u-b"
        )
    with repo._connect() as db:
        assert tuple(repo._runtime.unified_kg.graph_seq_row(db, nb)) == seq_before


def test_deleting_the_memory_after_a_refused_merge_loses_no_shared_object(world):
    """N-1 regression. Before E4-3 the merge succeeded, the shared object's
    evidence (and reverse index) gained the Memory source, and deleting that
    Memory deleted the WHOLE shared object by evidence reference."""
    repo, nb, upload = world
    other = _upload_source(repo, nb, "src-upload-2")
    memory_source = _confirmed_memory(repo, nb, "mem-a", "u-a")
    shared_before = {**_objects(repo, nb, upload), **_objects(repo, nb, other)}
    memory = _objects(repo, nb, memory_source)
    shared_concept = _concept(_objects(repo, nb, upload))

    for memory_object in memory:
        target = shared_concept if memory[memory_object]["object_type"] == "concept" else next(
            o for o, r in shared_before.items() if r["object_type"] == "claim"
        )
        with pytest.raises(MemoryKnowledgeMergeRefused):
            repo.merge_knowledge(
                nb, memory_object, MergeRequest(into_id=target), actor_id="u-a"
            )

    repo._runtime.source_ingestion.remove_memory_source("mem-a")

    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, status, evidence FROM knowledge_objects WHERE notebook_id=?", (nb,)
        ).fetchall()
    after = {r["id"]: dict(r) for r in rows}
    assert set(after) == set(shared_before)  # not one shared object missing
    for oid, row in shared_before.items():
        assert after[oid]["status"] == row["status"]
        assert after[oid]["evidence"] == row["evidence"]


# --------------------------------------------------------------------------
# Route: 409 with the curated copy
# --------------------------------------------------------------------------

def test_merge_route_answers_409_with_curated_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'route.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api.deps import repository
    from app.main import create_app

    client = TestClient(create_app())
    registered = client.post(
        "/api/auth/register", json={"username": "a00100001", "password": "pw"}
    ).json()
    headers = {"Authorization": f"Bearer {registered['token']}"}
    owner = registered["user"]["id"]
    nb = client.post("/api/notebooks", headers=headers, json={"name": "M1"}).json()["id"]

    repo = repository()
    with repo._write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) VALUES ('mem-r',?,?,'ask_answer',"
            "'confirmed','t','x',?,?)",
            (nb, owner, NOW, NOW),
        )
        for sid, stype, mid in (("src-m", "memory", "mem-r"), ("src-s", "markdown", None)):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                (sid, nb, sid, stype, mid, NOW, NOW),
            )
        for oid, sid in (("ko-m", "src-m"), ("ko-s", "src-s"), ("ko-s2", "src-s")):
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,'concept','approved',?,'[]',?,?,?)",
                (oid, nb, json.dumps({"name": oid}), sid, NOW, NOW),
            )

    for source_id, into_id in (("ko-m", "ko-s"), ("ko-s", "ko-m")):
        response = client.post(
            f"/api/notebooks/{nb}/knowledge/{source_id}/merge",
            headers=headers, json={"into_id": into_id},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == CROSS_CLASS_MESSAGE
        assert response.headers.get("X-User-Message") == "1"

    ok = client.post(
        f"/api/notebooks/{nb}/knowledge/ko-s/merge",
        headers=headers, json={"into_id": "ko-s2"},
    )
    assert ok.status_code == 200, ok.text


# --------------------------------------------------------------------------
# Promotion: the generic path never carries a Memory-derived object
# (scenarios shared with the PostgreSQL twin: memory_promotion_isolation_cases)
# --------------------------------------------------------------------------

@pytest.fixture
def promotion_world(tmp_path, monkeypatch):
    from app.api.deps import repository
    from app.main import create_app
    from tests import memory_promotion_isolation_cases as cases

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'promotion.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    client = TestClient(create_app())
    return cases, cases.build(client, repository(), lambda sql: sql)


def test_member_cannot_propose_an_object_derived_from_anothers_memory(promotion_world):
    cases, world = promotion_world
    cases.member_b_cannot_propose_a_memory_object(world)


def test_owner_is_told_why_her_memory_object_cannot_be_proposed(promotion_world):
    cases, world = promotion_world
    cases.owner_cannot_propose_her_memory_object_here(world)


def test_non_owner_write_routes_answer_like_a_missing_id(promotion_world):
    cases, world = promotion_world
    cases.non_owner_write_routes_answer_like_a_missing_id(world)


def test_owner_keeps_the_reason_on_write_routes(promotion_world):
    cases, world = promotion_world
    cases.owner_keeps_the_reason(world)


def test_proposal_of_a_vanished_object_is_closed_on_approval(promotion_world):
    cases, world = promotion_world
    cases.proposal_of_a_vanished_object_is_closed_on_approval(world)


def test_publishing_a_notebook_with_memory_is_refused(promotion_world):
    cases, world = promotion_world
    cases.publishing_a_notebook_with_memory_is_refused(world)


def test_checkup_counts_memory_in_a_public_library(promotion_world):
    cases, world = promotion_world
    cases.checkup_counts_memory_in_a_public_library(world)


def test_shared_review_surfaces_leave_memory_rows_out(promotion_world):
    cases, world = promotion_world
    cases.shared_review_surfaces_leave_memory_rows_out(world)


def test_queued_memory_object_proposal_is_refused_and_closed_on_approval(promotion_world):
    cases, world = promotion_world
    cases.queued_memory_proposal_is_refused_on_approval(world)


def test_withdrawn_memory_object_proposal_stays_withdrawn(promotion_world):
    cases, world = promotion_world
    cases.withdrawn_memory_proposal_stays_withdrawn(world)


def test_creator_memory_promotion_still_works_end_to_end(promotion_world):
    cases, world = promotion_world
    cases.creator_memory_promotion_still_works(world)


def test_ordinary_object_still_promotes(promotion_world):
    cases, world = promotion_world
    cases.ordinary_object_still_promotes(world)


# --------------------------------------------------------------------------
# A Memory confirmed before its notebook is published never lands in the
# public library (the publish window is the job-queue delay, not an instant)
# --------------------------------------------------------------------------

def _events(repo, monkeypatch) -> list:
    seen: list = []
    original = repo._runtime.event_log.emit
    monkeypatch.setattr(
        repo._runtime.event_log, "emit",
        lambda event: (seen.append(dict(event)), original(event))[1],
    )
    return seen


def _memory_rows(repo, nb: str) -> tuple[list, list]:
    with repo._connect() as db:
        sources = [r["id"] for r in db.execute(
            "SELECT id FROM sources WHERE notebook_id=? AND source_type='memory'", (nb,)
        ).fetchall()]
        objects = [r["id"] for r in db.execute(
            "SELECT ko.id FROM knowledge_objects ko JOIN sources s ON s.id = ko.source_id "
            "WHERE ko.notebook_id=? AND s.source_type='memory'", (nb,)
        ).fetchall()]
    return sources, objects


def test_memory_queued_before_publish_never_reaches_the_public_library(world, monkeypatch):
    """Confirm (job queued while the notebook is shared) → publish (allowed:
    no Memory source yet) → the job runs: it re-checks eligibility, ends with
    a content-free event, and the public library holds no Memory source, no
    object from it, and checkup H11 stays 0."""
    repo, nb, _upload = world
    with repo._write() as db:  # u-a owns the shared notebook
        db.execute("UPDATE notebooks SET created_by='u-a' WHERE id=?", (nb,))
    service = repo._runtime.memory_service
    queued: list = []
    monkeypatch.setattr(service, "kg_ingest_scheduler", lambda fn, item: queued.append((fn, item)))
    memory = repo.create_memory_candidate(nb, "u-a", None, "req-queued", "Queued", TEXT, [], "", {}, [])
    repo.confirm_memory(memory.id, "u-a")
    assert len(queued) == 1  # eligible when confirmed: the job is queued

    repo.mark_notebook_base(nb)  # no Memory source exists yet: publishing works
    events = _events(repo, monkeypatch)
    fn, item = queued[0]
    fn(item)

    assert _memory_rows(repo, nb) == ([], [])
    assert {c.code: c.count for c in repo.checkup.run(nb).checks}["H11"] == 0
    skipped = [e for e in events if e.get("kind") == "memory_kg" and e.get("status") == "skipped"]
    assert skipped == [{"kind": "memory_kg", "notebook_id": nb, "memory_id": memory.id,
                        "status": "skipped", "reason": "not_eligible"}]


def test_memory_source_insert_is_refused_in_a_public_library(world, monkeypatch):
    """The last line of defence, without the job's re-check: the insert itself
    is conditional on the notebook not being public."""
    repo, nb, _upload = world
    with repo._write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) VALUES ('mem-pub',?,'u-a','ask_answer',"
            "'confirmed','t',?,?,?)",
            (nb, TEXT, NOW, NOW),
        )
    repo.mark_notebook_base(nb)
    events = _events(repo, monkeypatch)
    assert repo._runtime.source_ingestion.ingest_memory_source(nb, "mem-pub", "t", TEXT) is None
    assert _memory_rows(repo, nb) == ([], [])
    assert [e for e in events if e.get("status") == "skipped"] == [
        {"kind": "memory_kg", "notebook_id": nb, "memory_id": "mem-pub",
         "status": "skipped", "reason": "public_library"}
    ]
    # Control: the same call on a shared notebook does create the source.
    repo.set_notebook_personal(nb)
    assert repo._runtime.source_ingestion.ingest_memory_source(nb, "mem-pub", "t", TEXT)
    assert len(_memory_rows(repo, nb)[0]) == 1
