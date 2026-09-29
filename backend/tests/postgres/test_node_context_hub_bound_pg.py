"""PostgreSQL twin of the hub-bound, name-only and ``ceiling_binds`` tests
(tests/test_node_context.py, tests/test_ceiling_binds_verdict.py).

Re-review F1: under a binding ceiling a cluster's fused description is verified
by reading at most ``NODE_CONTEXT_CLUSTER_MEMBER_PROBE`` member rows in one
statement; a bigger cluster fails closed to the in-ceiling ``defines``
fallback.  An all-ticked, un-drifted run without foreign hidden sources gets no
ceiling at all and keeps the hub's fused description.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.domain.knowledge_contracts import NODE_CONTEXT_CLUSTER_MEMBER_PROBE as BOUND
from app.domain.retrieval import RetrievedKnowledge
from app.models.notebooks import NotebookCreate
from app.services.source_scope import source_scope_context


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_node_context_hub_bound"),
]

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
def repo(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    postgres_settings.postgres_statement_timeout_seconds = 30
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _ev(source_id, element_id):
    return {"source_id": source_id, "source_title": source_id, "element_id": element_id,
            "element_type": "paragraph", "location_label": "p",
            "quoted_span": "q", "confidence": 1.0}


def _seed_hub(repo, members, *, owner=None):
    token = set_request_user(owner) if owner else None
    try:
        nb = repo.create_notebook(NotebookCreate(name=f"hub-{members}")).id
    finally:
        if token is not None:
            reset_request_user(token)
    src, el, el_def = f"src-{nb}", f"el-{nb}", f"el-def-{nb}"
    ids = [f"ko-hub-{nb}-{index:05d}" for index in range(members)]
    definer = f"ko-hub-definer-{nb}"
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,created_at,updated_at) VALUES (%s,%s,'hub','markdown','extracted',"
            "'parsed','d.md',%s,%s)", (src, nb, T0, T0))
        for element, text in ((el, "HUB occurrence"), (el_def, "HUB definition text")):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,"
                "text,created_at) VALUES (%s,%s,'paragraph','p',%s,%s)",
                (element, src, text, T0))
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) SELECT oid, %s, 'concept', "
            "'approved', '{\"name\":\"Hub\"}'::jsonb, %s::jsonb, %s, %s, %s "
            "FROM unnest(%s::text[]) AS oid",
            (nb, json.dumps([_ev(src, el)]), src, T0, T0, ids))
        db.execute(
            "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
            "SELECT oid, %s, %s FROM unnest(%s::text[]) AS oid", (src, nb, ids))
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,canonical_description,created_at,generation) "
            "SELECT 'cc-'||oid, %s, 'K-hub', oid, 'Hub', 'concept', "
            "'HUB fused description', %s, 0 FROM unnest(%s::text[]) AS oid",
            (nb, T0, ids))
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) VALUES (%s,%s,'claim','approved',"
            "'{\"name\":\"definer\"}'::jsonb,%s::jsonb,%s,%s,%s)",
            (definer, nb, json.dumps([_ev(src, el_def)]), src, T0, T0))
        db.execute(
            "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
            "VALUES (%s,%s,%s)", (definer, src, nb))
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,evidence,created_at) VALUES "
            "(%s,%s,%s,%s,%s,'defines','[]'::jsonb,%s)",
            (f"rel-{nb}", nb, src, definer, ids[0], T0))
    return nb, ids[0], src


def _nc(repo, nb, oid, allowed=None, **kwargs):
    return repo._runtime.knowledge.node_context(
        nb, oid, check_access=False, allowed_source_ids=allowed, **kwargs)


@pytest.mark.parametrize("backfilled", [True, False])
def test_pg_hub_description_is_verified_only_within_the_member_bound(repo, backfilled):
    for members, verified in ((BOUND - 1, True), (BOUND, True), (BOUND + 1, False)):
        nb, hub, src = _seed_hub(repo, members)
        with repo._runtime.database.write() as db:
            db.execute(
                "INSERT INTO unified_kg_state (notebook_id,updated_at,source_index_backfilled) "
                "VALUES (%s,%s,%s) ON CONFLICT (notebook_id) DO UPDATE "
                "SET source_index_backfilled=EXCLUDED.source_index_backfilled",
                (nb, T0, 1 if backfilled else 0))
        ctx = _nc(repo, nb, hub, frozenset([src]))
        if verified:
            assert (ctx["definition"], ctx["definition_basis"]) == (
                "HUB fused description", "cluster_description"), members
        else:
            assert (ctx["definition"], ctx["definition_basis"], ctx["definition_source_id"]) == (
                "HUB definition text", "defines_evidence", src), members
        assert _nc(repo, nb, hub)["definition"] == "HUB fused description"


def test_pg_name_only_returns_name_and_occurrences_only(repo):
    nb, hub, src = _seed_hub(repo, 3)
    slim = _nc(repo, nb, hub, frozenset([src]), name_only=True)
    assert (slim["name"], slim["definition"], slim["steps"]) == ("Hub", None, None)
    assert [o["source_id"] for o in slim["occurrences"]] == [src]
    assert _nc(repo, nb, hub, frozenset(), name_only=True)["occurrences"] == []


def test_pg_all_ticked_undrifted_run_gets_no_ceiling_and_keeps_the_hub_description(
    repo, monkeypatch,
):
    owner = repo.create_user("a00000001", "pw123456")
    nb, hub, src = _seed_hub(repo, BOUND + 1, owner=owner)
    runtime = repo._runtime
    service = runtime.evidence_context_component
    pushed = []
    original = runtime.knowledge.node_context

    def spy(*args, **kwargs):
        pushed.append(kwargs.get("allowed_source_ids", "<absent>"))
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.knowledge, "node_context", spy)
    hit = RetrievedKnowledge(object_id=hub, object_type="concept", payload={"name": "Hub"},
                             evidence=[], notebook_id=nb, tier="personal", relevance=0.9)
    plain = service.knowledge_context(nb, [hit], budget_chars=10_000)
    visible = runtime.source_store.all_visible_source_ids(nb)
    hidden = runtime.source_store.hidden_source_ids(nb, owner.id)
    with source_scope_context(nb, {"mode": "include", "source_ids": visible,
                                   "hidden_source_ids": hidden, "narrowed": False,
                                   "owner_id": owner.id}, None):
        ticked = service.knowledge_context(nb, [hit], budget_chars=10_000)
    assert ticked == plain and "HUB fused description" in ticked[0]
    assert pushed == ["<absent>", "<absent>"]
    with source_scope_context(nb, {"mode": "include", "source_ids": [src],
                                   "narrowed": True, "owner_id": owner.id}, None):
        narrowed = service.knowledge_context(nb, [hit], budget_chars=10_000)
    assert "HUB fused description" not in narrowed[0]
    assert "HUB definition text" in narrowed[0]
    assert pushed[-1] == frozenset([src])
