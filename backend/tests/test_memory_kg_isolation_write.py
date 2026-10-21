"""E4-3 / M1 写侧:个人记忆派生的知识对象不进共享簇、不与任何对象合并(SQLite)。

计划 §5 E4-3 的验收,逐条对应:

* 确认一条 Memory 后,增量融合(真实抽取收尾 `run_extraction` → `incremental_fuse_source`)
  不把它放进任何共享簇、不产生合并候选;scale fold 走的是同一个方法,直调同样不动。
* 手工合并跨类返回 409(真实路由,策展文案 + `X-User-Message`),store 层直调同样拒绝,
  且拒绝时一行不写。同一成员的两个 Memory 对象、且两份证据都只出自她本人的 Memory 时
  允许合并(§13.2 第 9 条,用户裁决 2026-10-21;判定见 `app.domain.memory_kg_isolation`);
  任一份证据夹着别的来源时照旧拒绝。合并之后删除或编辑其中一条 Memory,另一条 Memory
  的对象只失去这条 Memory 的证据,不被连带删除。
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
    MERGED_INTO_PAYLOAD_KEY,
    MemoryKnowledgeMergeRefused,
)
from app.models.knowledge import KnowledgeUpdate, MergeRequest
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
                {"type": "Concept", "name": "Engram",
                 "evidence": "Engram is a memory architecture"},
                {"type": "Claim", "name": "Engram is a memory architecture",
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
    repo._runtime.knowledge_lifecycle.incremental_fuse_source(nb, memory_source)
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


def _evidence(repo, object_id: str) -> list:
    with repo._connect() as db:
        row = db.execute(
            "SELECT evidence FROM knowledge_objects WHERE id=?", (object_id,)
        ).fetchone()
    return json.loads(row["evidence"]) if row is not None else None


def _reverse_index(repo, object_id: str) -> set:
    with repo._connect() as db:
        return {
            r["source_id"] for r in db.execute(
                "SELECT source_id FROM knowledge_object_sources WHERE object_id=?",
                (object_id,),
            ).fetchall()
        }


@pytest.fixture
def own_pair(world):
    """Two confirmed Memories of u-a (real pipeline), their concepts merged:
    a1's concept folded into a2's."""
    repo, nb, _upload = world
    src_a1 = _confirmed_memory(repo, nb, "mem-a1", "u-a")
    src_a2 = _confirmed_memory(repo, nb, "mem-a2", "u-a")
    a1 = _concept(_objects(repo, nb, src_a1))
    a2 = _concept(_objects(repo, nb, src_a2))
    with repo._connect() as db:
        seq_before = tuple(repo._runtime.unified_kg.graph_seq_row(db, nb))
    row = repo._runtime.knowledge_governance.merge_knowledge(
        nb, a1, MergeRequest(into_id=a2), actor_id="u-a"
    )
    assert row.id == a2
    with repo._connect() as db:
        assert tuple(repo._runtime.unified_kg.graph_seq_row(db, nb)) != seq_before
    return repo, nb, src_a1, src_a2, a1, a2


def test_owner_merges_two_of_her_own_memory_objects(own_pair):
    """§13.2-9 (user ruling 2026-10-21): both sides hers, both supported by
    her Memory alone → merged. The result keeps a2's Memory source (still
    hers alone: u-b and an internal caller get "not found"); its evidence and
    reverse index hold her two Memory sources only."""
    repo, nb, src_a1, src_a2, a1, a2 = own_pair
    snap = _snapshot(repo, nb)
    assert snap[a1][0] == "deprecated" and snap[a2][0] == "approved"
    assert _objects(repo, nb, src_a2)[a2]["status"] == "approved"  # still a2's source
    assert {e["source_id"] for e in _evidence(repo, a2)} == {src_a1, src_a2}
    assert _reverse_index(repo, a2) == {src_a1, src_a2}
    for actor in ("u-b", None):
        with pytest.raises(KeyError):
            _merge_in_store(repo, nb, a2, a1, actor_id=actor)


def test_deleting_one_merged_memory_strips_only_its_evidence(own_pair):
    """Deleting the merged-away Memory: the surviving object (another
    Memory's) loses exactly that Memory's entries and is never deleted with
    it; deleting the surviving Memory then removes everything."""
    repo, nb, src_a1, src_a2, a1, a2 = own_pair
    repo._runtime.source_ingestion.remove_memory_source("mem-a1")
    assert _evidence(repo, a1) is None  # a1's own objects are gone
    assert {e["source_id"] for e in _evidence(repo, a2)} == {src_a2}
    assert _reverse_index(repo, a2) == {src_a2}
    assert _snapshot(repo, nb)[a2][0] == "approved"
    repo._runtime.source_ingestion.remove_memory_source("mem-a2")
    assert _objects(repo, nb, src_a2) == {} and _evidence(repo, a2) is None


def test_editing_one_merged_memory_keeps_the_other_memorys_object(own_pair):
    """An edit re-ingests the Memory: its old graph is cleared under the
    source lock, after its evidence is stripped from the object the other
    Memory owns — which survives with its own evidence only. Without the
    strip the clear deletes it by evidence reference (N-1 within one owner)."""
    repo, nb, src_a1, src_a2, a1, a2 = own_pair
    assert repo._runtime.source_ingestion.ingest_memory_source(
        nb, "mem-a1", "mem-a1", TEXT + " Edited."
    ) == src_a1
    assert _evidence(repo, a1) is None  # a1's old objects were re-extracted away
    assert _snapshot(repo, nb)[a2][0] == "approved"
    assert {e["source_id"] for e in _evidence(repo, a2)} == {src_a2}
    assert _reverse_index(repo, a2) == {src_a2}
    assert _objects(repo, nb, src_a1)  # the edited Memory has fresh objects


def _status(repo, object_id: str) -> "str | None":
    with repo._connect() as db:
        row = db.execute(
            "SELECT status FROM knowledge_objects WHERE id=?", (object_id,)
        ).fetchone()
    return None if row is None else row["status"]


def _listed_approved(repo, nb: str) -> set:
    """The approved concepts u-a's knowledge list shows (her viewer scope)."""
    from app.core.request_context import reset_request_user, set_request_user
    from app.models.identity import UserProfile

    token = set_request_user(UserProfile(
        id="u-a", email="u-a@example.test", display_name="u-a", role="user",
    ))
    try:
        page = repo.list_knowledge(nb, "concept", status="approved", offset=0, limit=200)
    finally:
        reset_request_user(token)
    return {item.id for item in page.items}


def _owned_by(repo, user: str, *notebooks: str) -> None:
    with repo._write() as db:
        for nb in notebooks:
            db.execute("UPDATE notebooks SET created_by=? WHERE id=?", (user, nb))


def _change_memory(repo, nb: str, memory_id: str, change: str) -> None:
    """Edit, transfer (move) or delete one of u-a's Memories through the
    real service paths."""
    if change == "edit":
        repo._runtime.source_ingestion.ingest_memory_source(
            nb, memory_id, memory_id, TEXT + " Edited."
        )
    elif change == "transfer":
        dst = repo.create_notebook(NotebookCreate(name="dst")).id
        _owned_by(repo, "u-a", nb, dst)
        (result,) = repo._runtime.memory_service.transfer(
            "u-a", [memory_id], dst, "move", False
        )
        assert result["ok"] is True, result
    else:
        _owned_by(repo, "u-a", nb)
        repo._runtime.memory_service.delete(memory_id, "u-a")


@pytest.mark.parametrize("change", ["edit", "transfer", "delete"])
def test_clearing_the_merge_result_restores_the_merged_away_object(own_pair, change):
    """User ruling 2026-10-21: a1 was merged into a2 (a2 belongs to mem-a2).
    Editing, transferring or deleting mem-a2 clears a2; just before, a1 —
    whose Memory still exists — is restored to approved with its own
    evidence, so mem-a1's knowledge is retrievable again instead of
    silently vanishing."""
    repo, nb, src_a1, _src_a2, a1, a2 = own_pair
    assert a1 not in _listed_approved(repo, nb)
    _change_memory(repo, nb, "mem-a2", change)
    assert _status(repo, a2) is None
    assert _status(repo, a1) == "approved"
    assert {e["source_id"] for e in _evidence(repo, a1)} == {src_a1}
    assert a1 in _listed_approved(repo, nb)


def test_clearing_the_merged_away_memory_restores_nothing(own_pair):
    """The merged-away side's own Memory is deleted: its object goes with it
    (nothing to restore) and the merge result only loses its entries."""
    repo, nb, _src_a1, src_a2, a1, a2 = own_pair
    _change_memory(repo, nb, "mem-a1", "delete")
    assert _status(repo, a1) is None
    assert _status(repo, a2) == "approved"
    assert {e["source_id"] for e in _evidence(repo, a2)} == {src_a2}


def test_an_orphaned_merged_away_object_is_not_restored(own_pair):
    """mem-a1's row is gone (its source is an orphan): clearing the merge
    result does not bring a1 back."""
    repo, nb, _src_a1, _src_a2, a1, a2 = own_pair
    with repo._write() as db:
        db.execute("UPDATE sources SET memory_id=NULL WHERE memory_id='mem-a1'")
    _change_memory(repo, nb, "mem-a2", "delete")
    assert _status(repo, a2) is None
    assert _status(repo, a1) == "deprecated"


def test_a_hand_deprecated_object_of_the_same_memory_stays_deprecated(own_pair):
    """Only what the merge result absorbed is restored: mem-a1's claim that
    its owner deprecated by hand stays deprecated."""
    repo, nb, src_a1, _src_a2, a1, _a2 = own_pair
    (claim,) = [o for o, r in _objects(repo, nb, src_a1).items() if r["object_type"] == "claim"]
    repo.update_knowledge(nb, claim, KnowledgeUpdate(status="deprecated"), actor_id="u-a")
    _change_memory(repo, nb, "mem-a2", "delete")
    assert _status(repo, a1) == "approved"
    assert _status(repo, claim) == "deprecated"


def _payload(repo, object_id: str) -> dict:
    with repo._connect() as db:
        row = db.execute(
            "SELECT payload FROM knowledge_objects WHERE id=?", (object_id,)
        ).fetchone()
    return json.loads(row["payload"])


def _governance(repo):
    return repo._runtime.knowledge_governance


def test_a_merge_chain_restores_one_level_per_cleared_result(world):
    """a1 → a2 → a3. Each merged-away row records its own target, so
    deleting mem-a3 restores a2 — which still carries a1's evidence — and
    leaves a1 deprecated (a2 represents it); deleting mem-a2 afterwards
    restores a1."""
    repo, nb, _upload = world
    sources = [_confirmed_memory(repo, nb, f"mem-a{n}", "u-a") for n in (1, 2, 3)]
    a1, a2, a3 = (_concept(_objects(repo, nb, s)) for s in sources)
    _governance(repo).merge_knowledge(nb, a1, MergeRequest(into_id=a2), actor_id="u-a")
    _governance(repo).merge_knowledge(nb, a2, MergeRequest(into_id=a3), actor_id="u-a")
    assert _payload(repo, a1)[MERGED_INTO_PAYLOAD_KEY] == a2
    assert _payload(repo, a2)[MERGED_INTO_PAYLOAD_KEY] == a3
    _change_memory(repo, nb, "mem-a3", "delete")
    assert _status(repo, a2) == "approved" and _status(repo, a1) == "deprecated"
    assert MERGED_INTO_PAYLOAD_KEY not in _payload(repo, a2)
    assert {e["source_id"] for e in _evidence(repo, a2)} == set(sources[:2])
    _change_memory(repo, nb, "mem-a2", "delete")
    assert _status(repo, a1) == "approved"
    assert {e["source_id"] for e in _evidence(repo, a1)} == {sources[0]}


def test_clearing_a_merged_away_link_of_a_chain_repoints_its_followers(world):
    """a1 → a2 → a3, then mem-a2 (whose object is itself merged away) is
    deleted: a1 is not restored (a3 still carries its evidence) but now
    points at a3, so deleting mem-a3 restores a1."""
    repo, nb, _upload = world
    sources = [_confirmed_memory(repo, nb, f"mem-a{n}", "u-a") for n in (1, 2, 3)]
    a1, a2, a3 = (_concept(_objects(repo, nb, s)) for s in sources)
    _governance(repo).merge_knowledge(nb, a1, MergeRequest(into_id=a2), actor_id="u-a")
    _governance(repo).merge_knowledge(nb, a2, MergeRequest(into_id=a3), actor_id="u-a")
    _change_memory(repo, nb, "mem-a2", "delete")
    assert _status(repo, a2) is None
    assert _status(repo, a1) == "deprecated"
    assert _payload(repo, a1)[MERGED_INTO_PAYLOAD_KEY] == a3
    _change_memory(repo, nb, "mem-a3", "delete")
    assert _status(repo, a1) == "approved"


def test_a_merged_away_object_with_fully_deduplicated_evidence_is_restored(world):
    """Spec review P2-1: a sibling whose every evidence entry was already in
    the target (deduplicated, so nothing of it is copied) still records the
    target on its own row, and is restored with a1 when the target goes."""
    repo, nb, _upload = world
    s1 = _confirmed_memory(repo, nb, "mem-a1", "u-a")
    s2 = _confirmed_memory(repo, nb, "mem-a2", "u-a")
    a1 = _concept(_objects(repo, nb, s1))
    a2 = _concept(_objects(repo, nb, s2))
    with repo._write() as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(knowledge_objects)").fetchall()]
        picked = ",".join("'ko-sibling'" if c == "id" else c for c in cols)
        db.execute(
            f"INSERT INTO knowledge_objects ({','.join(cols)}) "
            f"SELECT {picked} FROM knowledge_objects WHERE id=?",
            (a1,),
        )
    _governance(repo).merge_knowledge(nb, a1, MergeRequest(into_id=a2), actor_id="u-a")
    before = _evidence(repo, a2)
    _governance(repo).merge_knowledge(
        nb, "ko-sibling", MergeRequest(into_id=a2), actor_id="u-a"
    )
    assert _evidence(repo, a2) == before  # every sibling entry was a duplicate
    _change_memory(repo, nb, "mem-a2", "delete")
    assert _status(repo, a1) == "approved"
    assert _status(repo, "ko-sibling") == "approved"


def test_a_hand_edit_clears_the_merge_marker(own_pair):
    """Quality P3-1 / spec P3-2: only a merge writes the marker; changing the
    merged-away object's status by hand clears it, so an object its owner
    deprecated by hand is never restored automatically."""
    repo, nb, _src_a1, _src_a2, a1, a2 = own_pair
    governance = _governance(repo)
    governance.update_knowledge(nb, a1, KnowledgeUpdate(status="approved"), actor_id="u-a")
    governance.update_knowledge(nb, a1, KnowledgeUpdate(status="deprecated"), actor_id="u-a")
    assert MERGED_INTO_PAYLOAD_KEY not in _payload(repo, a1)
    _change_memory(repo, nb, "mem-a2", "delete")
    assert _status(repo, a2) is None
    assert _status(repo, a1) == "deprecated"


def test_merging_a_merged_away_object_again_moves_its_marker(world):
    """Quality P3-2: a1 → a2, then a1 (deprecated) → a3. The marker follows
    the latest target: clearing a2 restores nothing, clearing a3 restores
    a1 — never two copies of it."""
    repo, nb, _upload = world
    sources = [_confirmed_memory(repo, nb, f"mem-a{n}", "u-a") for n in (1, 2, 3)]
    a1, a2, a3 = (_concept(_objects(repo, nb, s)) for s in sources)
    _governance(repo).merge_knowledge(nb, a1, MergeRequest(into_id=a2), actor_id="u-a")
    _governance(repo).merge_knowledge(nb, a1, MergeRequest(into_id=a3), actor_id="u-a")
    assert _payload(repo, a1)[MERGED_INTO_PAYLOAD_KEY] == a3
    _change_memory(repo, nb, "mem-a2", "delete")
    assert _status(repo, a1) == "deprecated"
    _change_memory(repo, nb, "mem-a3", "delete")
    assert _status(repo, a1) == "approved"


def test_the_startup_backstop_restores_what_a_crash_left_behind(own_pair):
    """Self-healing criterion: deprecated, the marker names an object that no
    longer exists, its own Memory still exists. Simulates a teardown whose
    follow-up never ran (the target deleted behind its back)."""
    repo, nb, _src_a1, _src_a2, a1, a2 = own_pair
    with repo._write() as db:
        db.execute("DELETE FROM knowledge_objects WHERE id=?", (a2,))
    ingestion = repo._runtime.source_ingestion
    assert ingestion.has_memory_merges_to_heal() is True
    assert ingestion.heal_memory_merges(page_size=1) == 1
    assert _status(repo, a1) == "approved"
    assert MERGED_INTO_PAYLOAD_KEY not in _payload(repo, a1)
    assert ingestion.has_memory_merges_to_heal() is False
    assert ingestion.heal_memory_merges() == 0


def test_shared_merges_record_no_merge_marker(world):
    """Quality P3-4: shared objects' merges write no marker — the merged-away
    row's payload and the target's evidence are byte for byte what they were
    without this feature."""
    repo, nb, upload = world
    other = _upload_source(repo, nb, "src-upload-2")
    a = _concept(_objects(repo, nb, upload))
    b = _concept(_objects(repo, nb, other))
    expected = _evidence(repo, b) + _evidence(repo, a)
    with repo._connect() as db:
        payload_before = db.execute(
            "SELECT payload FROM knowledge_objects WHERE id=?", (a,)
        ).fetchone()["payload"]
    repo.merge_knowledge(nb, a, MergeRequest(into_id=b))
    assert _evidence(repo, b) == expected
    with repo._connect() as db:
        payload_after = db.execute(
            "SELECT payload FROM knowledge_objects WHERE id=?", (a,)
        ).fetchone()["payload"]
    assert payload_after == payload_before
    assert MERGED_INTO_PAYLOAD_KEY not in json.loads(payload_after)


def _cite(repo, object_id: str, source_id: str) -> None:
    evidence = _evidence(repo, object_id) + [{
        "source_id": source_id, "element_id": f"el-{source_id}",
        "quoted_span": "cited", "source_title": source_id,
        "element_type": "paragraph", "location_label": "p1", "confidence": 1.0,
    }]
    with repo._write() as db:
        db.execute(
            "UPDATE knowledge_objects SET evidence=? WHERE id=?",
            (json.dumps(evidence), object_id),
        )


@pytest.mark.parametrize("cited", ["shared", "another_members_memory", "no_source_id"])
@pytest.mark.parametrize("carrier", ["source", "target"])
def test_own_memory_pair_not_backed_by_her_memory_alone_is_refused(world, cited, carrier):
    """Same owner is not enough: when either side's evidence cites anything
    but her own Memory sources of this notebook — a shared source (legacy
    mixed evidence), another member's Memory, an entry without a source id —
    the pair is refused with the cross-class copy and nothing is written."""
    repo, nb, upload = world
    a1 = _concept(_objects(repo, nb, _confirmed_memory(repo, nb, "mem-a1", "u-a")))
    a2 = _concept(_objects(repo, nb, _confirmed_memory(repo, nb, "mem-a2", "u-a")))
    foreign = _confirmed_memory(repo, nb, "mem-b", "u-b")
    _cite(
        repo, a1 if carrier == "source" else a2,
        {"shared": upload, "another_members_memory": foreign, "no_source_id": ""}[cited],
    )
    before = _snapshot(repo, nb)
    with pytest.raises(MemoryKnowledgeMergeRefused) as refused:
        _merge_in_store(repo, nb, a1, a2)
    assert refused.value.user_message == CROSS_CLASS_MESSAGE
    assert _snapshot(repo, nb) == before


def test_site_admin_has_no_bypass_on_a_members_memory_pair(world):
    """§13.2-1 stance: a site admin is not the Memory's owner, so both of
    u-a's Memory objects are "not found" for her (404), never merged."""
    repo, nb, _upload = world
    _user(repo, "u-admin")
    with repo._write() as db:
        db.execute("UPDATE users SET role='admin' WHERE id='u-admin'")
    a1 = _concept(_objects(repo, nb, _confirmed_memory(repo, nb, "mem-a1", "u-a")))
    a2 = _concept(_objects(repo, nb, _confirmed_memory(repo, nb, "mem-a2", "u-a")))
    before = _snapshot(repo, nb)
    with pytest.raises(KeyError) as missing:
        _merge_in_store(repo, nb, a1, a2, actor_id="u-admin")
    assert type(missing.value) is KeyError
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


def test_owner_merges_her_own_memory_objects_through_the_route(promotion_world):
    cases, world = promotion_world
    cases.owner_merges_her_own_memory_objects(world)


def test_owner_memory_object_with_shared_evidence_cannot_merge(promotion_world):
    cases, world = promotion_world
    cases.owner_memory_object_with_shared_evidence_cannot_merge(world)


def test_owner_merge_follows_ownership_after_promotion(promotion_world):
    cases, world = promotion_world
    cases.owner_merge_follows_ownership_after_promotion(world)


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
