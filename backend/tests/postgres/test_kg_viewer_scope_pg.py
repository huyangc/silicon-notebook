"""PostgreSQL twin of tests/test_kg_viewer_scope.py (PR-A·A5, rulings Q4 / M1).

Same scenario, seeded through the same repository facade: user A owns a
shared notebook, user B is a member; a visible source and A's confirmed
Memory both yield "Engram" (one cluster with a fused description); A's Memory
also yields "SecretProject", a claim that ``defines`` the visible Engram, and
one step's text of a visible procedure.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.models.notebooks import NotebookCreate


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_kg_viewer_scope"),
]

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
def repo(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _source(db, nb, sid, *, memory_id=None, elements=()):
    db.execute(
        "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
        "file_name,memory_id,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,'extracted','parsed','d.md',%s,%s,%s)",
        (sid, nb, sid, "memory" if memory_id else "markdown", memory_id, T0, T0),
    )
    for index, (eid, text) in enumerate(elements):
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,created_at) VALUES (%s,%s,'paragraph',%s,%s,%s)",
            (eid, sid, f"p{index}", text, T0 + timedelta(seconds=index)),
        )


def _memory(db, nb, memory_id, owner):
    db.execute(
        "INSERT INTO memory_items (id,notebook_id,created_by,origin,status,title,"
        "content_md,created_at,updated_at) VALUES (%s,%s,%s,'ask_answer','confirmed',"
        "'t','c',%s,%s)",
        (memory_id, nb, owner, T0, T0),
    )


def _ev(sid, eid):
    return {"source_id": sid, "source_title": sid, "element_id": eid,
            "element_type": "paragraph", "location_label": "p",
            "quoted_span": f"quote {eid}", "confidence": 1.0}


def _object_id(db, nb, name, source_id):
    return db.execute(
        "SELECT id FROM knowledge_objects WHERE notebook_id=%s AND source_id=%s "
        "AND payload->>'name'=%s", (nb, source_id, name),
    ).fetchone()["id"]


def build_scenario(repo, *, b_memory: bool):
    a = repo.create_user("a00000001", "pw123456")
    b = repo.create_user("b00000002", "pw123456")
    token = set_request_user(a)
    try:
        nb = repo.create_notebook(NotebookCreate(name="shared")).id
        repo.add_member(nb, b.id)
    finally:
        reset_request_user(token)
    database = repo._runtime.database
    with database.write() as db:
        _source(db, nb, "src-s", elements=[
            ("el-s-occ", "VISIBLE occurrence of Engram"),
            ("el-s-def", "VISIBLE definition of Engram"),
            ("el-s-step", "VISIBLE step text"),
        ])
        _memory(db, nb, "mem-a", a.id)
        _source(db, nb, "src-ma", memory_id="mem-a", elements=[
            ("el-ma-occ", "A-PRIVATE occurrence of Engram"),
            ("el-ma-def", "A-PRIVATE definition of Engram"),
            ("el-ma-step", "A-PRIVATE step text"),
            ("el-ma-secret", "A-PRIVATE secret project"),
        ])
        if b_memory:
            _memory(db, nb, "mem-b", b.id)
            _source(db, nb, "src-mb", memory_id="mem-b", elements=[("el-mb", "B-PRIVATE")])
    repo.store_kg(nb, "src-s", [
        {"local_id": "c", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-occ")]},
        {"local_id": "d", "object_type": "claim",
         "payload": {"name": "visible definer", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-def")]},
        {"local_id": "p", "object_type": "procedure",
         "payload": {"name": "Flow", "section_path": "2", "steps": [
             {"name": "s-visible", "element_id": "el-s-step", "quote": "q"},
             {"name": "s-private", "element_id": "el-ma-step", "quote": "q"},
         ]},
         "evidence": [_ev("src-s", "el-s-step")]},
    ], [])
    repo.store_kg(nb, "src-ma", [
        {"local_id": "c", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"},
         "evidence": [_ev("src-ma", "el-ma-occ")]},
        {"local_id": "d", "object_type": "claim",
         "payload": {"name": "private definer", "section_path": "1"},
         "evidence": [_ev("src-ma", "el-ma-def")]},
        {"local_id": "x", "object_type": "concept",
         "payload": {"name": "SecretProject", "section_path": "1"},
         "evidence": [_ev("src-ma", "el-ma-secret")]},
    ], [{"source_local_id": "x", "target_local_id": "c", "edge_type": "related_to",
         "evidence": []}])
    with database.write() as db:
        ids = SimpleNamespace(
            engram_s=_object_id(db, nb, "Engram", "src-s"),
            engram_ma=_object_id(db, nb, "Engram", "src-ma"),
            secret=_object_id(db, nb, "SecretProject", "src-ma"),
            flow=_object_id(db, nb, "Flow", "src-s"),
            definer_s=_object_id(db, nb, "visible definer", "src-s"),
            definer_ma=_object_id(db, nb, "private definer", "src-ma"),
        )
        for rel_id, definer in (("rel-a", ids.definer_ma), ("rel-b", ids.definer_s)):
            db.execute(
                "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
                "source_object_id,target_object_id,edge_type,evidence,created_at) "
                "VALUES (%s,%s,%s,%s,%s,'defines','[]'::jsonb,%s)",
                (rel_id, nb, "src-s", definer, ids.engram_s, T0),
            )
    repo.rebuild_unified_kg(nb)
    cmap = repo.cluster_map(nb)
    ids.engram_canonical = cmap[ids.engram_s]
    assert cmap[ids.engram_ma] == ids.engram_canonical
    ids.secret_canonical = cmap[ids.secret]
    with database.write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_description='FUSED description' "
            "WHERE notebook_id=%s AND canonical_id=%s", (nb, ids.engram_canonical),
        )
    return SimpleNamespace(nb=nb, a=a, b=b, ids=ids)


def as_user(user, fn, *args, **kwargs):
    token = set_request_user(user)
    try:
        return fn(*args, **kwargs)
    finally:
        reset_request_user(token)


def _texts(ctx):
    return [o.get("element_text") for o in ctx["occurrences"]]


def test_pg_member_sees_no_text_from_another_members_memory(repo):
    s = build_scenario(repo, b_memory=False)
    ctx = as_user(s.b, repo.node_context, s.nb, s.ids.engram_s)
    assert ctx["definition"] == "VISIBLE definition of Engram"
    assert ctx["definition_basis"] == "defines_evidence"
    assert _texts(ctx) == ["VISIBLE occurrence of Engram"]
    flow = as_user(s.b, repo.node_context, s.nb, s.ids.flow)
    assert ("s-visible", "VISIBLE step text") in [
        (st["name"], st["element_text"]) for st in flow["steps"]]
    assert "A-PRIVATE" not in repr(ctx) + repr(flow)
    for oid in (s.ids.engram_ma, s.ids.secret, s.ids.definer_ma):
        with pytest.raises(KeyError):
            as_user(s.b, repo.node_context, s.nb, oid)


def test_pg_owner_keeps_own_memory_text_under_foreign_memory(repo):
    s = build_scenario(repo, b_memory=True)
    assert _texts(as_user(s.a, repo.node_context, s.nb, s.ids.engram_ma)) == [
        "A-PRIVATE occurrence of Engram"]
    visible = as_user(s.a, repo.node_context, s.nb, s.ids.engram_s)
    assert visible["definition"] == "FUSED description"


def test_pg_short_circuit_passes_no_ceiling_and_returns_todays_bytes(repo, monkeypatch):
    s = build_scenario(repo, b_memory=False)
    runtime = repo._runtime
    expected = runtime.knowledge.node_context(s.nb, s.ids.engram_s, check_access=False)
    seen = []
    original = runtime.knowledge.node_context

    def spy(*args, **kwargs):
        seen.append(kwargs.get("allowed_source_ids"))
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.knowledge, "node_context", spy)
    assert as_user(s.a, repo.node_context, s.nb, s.ids.engram_s) == expected
    assert seen == [None]


def test_pg_concept_detail_and_neighbours_omit_hidden(repo):
    s = build_scenario(repo, b_memory=False)
    detail = as_user(s.b, repo.concept_detail, s.nb, s.ids.engram_canonical)
    assert [m["id"] for m in detail["members"]] == [s.ids.engram_s]
    assert detail["member_total"] == 1
    assert [a["payload"]["name"] for a in detail["attached"]] == ["visible definer"]
    for secret in ("A-PRIVATE", "private definer", "src-ma"):
        assert secret not in repr(detail)
    with pytest.raises(KeyError):
        as_user(s.b, repo.concept_detail, s.nb, s.ids.secret_canonical)
    member_view = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert s.ids.secret_canonical not in {n["id"] for n in member_view["nodes"]}
    owner_view = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert s.ids.secret_canonical in {n["id"] for n in owner_view["nodes"]}
