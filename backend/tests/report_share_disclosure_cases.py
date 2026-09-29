"""Report share disclosure scenarios (E7-2, ruling M4), defined once.

``test_report_share_disclosure.py`` runs them on SQLite and
``postgres/test_report_share_disclosure_pg.py`` on PostgreSQL, both through the
real HTTP routes with real users: an owner of a shared notebook, a member
(Alice) who writes the report, and the deployment admin.

Memory is created through the real service (candidate -> confirm) and its
hidden projection source through the real ``ingest_memory_source``.  A report is
created through the API and then finished by writing ``references`` the way
the report engine stores them, so each case controls exactly what is cited.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import Any, Callable

from fastapi.testclient import TestClient

from app.services.share_disclosure import SHARE_DISCLOSURE_REQUIRED

PASSWORD = "pw12345678"
NON_AUTHOR_SHARE_REFUSAL = "报告引用了作者本人的个人记忆，只有作者可以公开分享。"
_USERNAMES = count(1)
_KEYS = count(1)


@dataclass(frozen=True)
class User:
    id: str
    headers: dict[str, str]


@dataclass
class World:
    client: TestClient
    repo: Any
    monkeypatch: Any
    owner: User
    alice: User
    admin: User
    notebook: str
    doc_source: str
    memories: dict[str, tuple[str, str]] = field(default_factory=dict)


def _register(client: TestClient) -> User:
    username = f"s{next(_USERNAMES):08d}"
    client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    return User(client.get("/api/me", headers=headers).json()["id"], headers)


def _admin(client: TestClient) -> User:
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    return User(client.get("/api/me", headers=headers).json()["id"], headers)


def build_world(client: TestClient, monkeypatch: Any) -> World:
    from app.api import deps
    import app.api.report_routes as routes

    # Reports are created through the route; no model and no worker needed.
    monkeypatch.setattr(routes, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_report_llm_ready", lambda repo: True)
    repo = deps.repository()
    service = repo._runtime.memory_service
    # Projections are ingested explicitly by make_memory; no background jobs.
    service.kg_ingest_scheduler = lambda fn, item: None
    service.embedding_scheduler = lambda fn, job: None
    owner, alice, admin = _register(client), _register(client), _admin(client)
    notebook = client.post(
        "/api/notebooks", json={"name": "共享库"}, headers=owner.headers
    ).json()["id"]
    repo.add_member(notebook, alice.id)
    doc_source = f"src-doc-{next(_KEYS)}"
    repo._runtime.source_ingestion.sources.insert_source(
        source_id=doc_source, notebook_id=notebook, title="设计说明",
        source_type="upload", status="active", parse_status="parsed",
        file_name="d.md", file_path="", file_size=1, file_hash="h",
        summary="", doc_type="",
    )
    return World(client, repo, monkeypatch, owner, alice, admin, notebook, doc_source)


def make_memory(world: World, user: User, key: str, *, source_id: str = "") -> str:
    """A confirmed Memory of ``user`` plus its hidden projection source.

    ``source_id`` pins the projection's id (the ingest mints ``src-…`` ids);
    used to model a projection that lands after a report already cites it.
    """
    service = world.repo._runtime.memory_service
    ingestion = world.repo._runtime.source_ingestion
    candidate = service.create_candidate(
        world.notebook, user.id, None, f"req-{key}-{next(_KEYS)}", f"记忆 {key}",
        f"记忆 {key}：环路补偿保持稳定。", [], "reason", {}, [],
    )
    memory = service.confirm(candidate.id, user.id)
    if source_id:
        minted = ingestion.new_id
        world.monkeypatch.setattr(
            ingestion, "new_id",
            lambda prefix: source_id if prefix == "src" else minted(prefix),
        )
    try:
        projected = ingestion.ingest_memory_source(
            world.notebook, memory.id, memory.title, memory.content_md
        )
    finally:
        if source_id:
            world.monkeypatch.setattr(ingestion, "new_id", minted)
    assert projected, "the Memory projection source must exist"
    world.memories[key] = (memory.id, projected)
    return memory.id


def source_ref(source_id: str, key: str, snippet: str = "摘录") -> dict:
    return {
        "key": key, "object_id": f"el-{key}", "object_type": "element",
        "label": "来源", "source_title": "来源", "source_id": source_id,
        "element_id": f"el-{key}", "snippet": snippet, "location_label": "段落",
        "tier": "personal",
    }


def memory_ref(memory_id: str, key: str, snippet: str = "个人记忆摘录") -> dict:
    return {
        "key": key, "object_id": memory_id, "object_type": "memory",
        "label": "记忆", "name": "记忆", "source_title": "", "source_id": "",
        "snippet": snippet, "location_label": "Memory", "tier": "personal",
    }


def make_report(world: World, author: User, references: list[dict], *,
                finished: bool = True) -> str:
    response = world.client.post(
        f"/api/notebooks/{world.notebook}/reports",
        json={"question": "环路为什么稳定？"}, headers=author.headers,
    )
    assert response.status_code == 200, response.text
    report_id = response.json()["report_id"]
    repo = world.repo
    repo.update_report(world.notebook, report_id, status="outline_ready")
    assert repo.claim_report_generation(world.notebook, report_id)
    if finished:
        repo.update_report(
            world.notebook, report_id, status="done", progress="完成",
            content_md="# 结论\n\n环路稳定 [k1]。", references=references,
        )
    else:
        repo.update_report(world.notebook, report_id, references=references)
    return report_id


def _url(world: World, report_id: str, tail: str = "") -> str:
    return f"/api/notebooks/{world.notebook}/reports/{report_id}/share{tail}"


def disclosure(world: World, user: User, report_id: str):
    return world.client.get(_url(world, report_id, "/disclosure"), headers=user.headers)


def share(world: World, user: User, report_id: str, acknowledged: int | None = None,
          *, body: Any = None):
    if acknowledged is not None:
        body = {"acknowledged_memory_count": acknowledged}
    if body is None:
        return world.client.post(_url(world, report_id), headers=user.headers)
    return world.client.post(_url(world, report_id), json=body, headers=user.headers)


def is_shared(world: World, report_id: str) -> bool:
    return bool(world.repo.get_report(world.notebook, report_id).get("shared"))


def _required(memory_count: int, new_memory_count: int) -> dict:
    return {"detail": {
        "code": SHARE_DISCLOSURE_REQUIRED,
        "memory_count": memory_count,
        "new_memory_count": new_memory_count,
    }}


# --- scenarios ---------------------------------------------------------------


def case_report_without_memory_publishes_as_before(world: World) -> None:
    rid = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    assert disclosure(world, world.alice, rid).json() == {"memory_count": 0}
    first = share(world, world.alice, rid)          # no body, as before M4
    assert first.status_code == 200
    assert set(first.json()) == {"share_token"}
    token = first.json()["share_token"]
    again = share(world, world.alice, rid)
    assert again.status_code == 200 and again.json() == {"share_token": token}
    public = world.client.get(f"/api/public/reports/{token}")
    assert public.status_code == 200
    assert [ref["snippet"] for ref in public.json()["references"]] == ["摘录"]


def case_own_memory_needs_the_current_count_acknowledged(world: World) -> None:
    mid = make_memory(world, world.alice, "a1")
    _, projection = world.memories["a1"]
    rid = make_report(world, world.alice, [
        source_ref(world.doc_source, "k1"),
        source_ref(projection, "k2", "来自记忆投影的摘录"),
        memory_ref(mid, "k3"),               # the same Memory, cited directly
    ])
    assert disclosure(world, world.alice, rid).json() == {"memory_count": 1}

    missing = share(world, world.alice, rid)
    assert missing.status_code == 409
    assert missing.json() == _required(1, 1)
    wrong = share(world, world.alice, rid, 2)
    assert wrong.status_code == 409 and wrong.json() == _required(1, 0)
    empty_body = share(world, world.alice, rid, body={})
    assert empty_body.status_code == 409 and empty_body.json() == _required(1, 1)
    assert not is_shared(world, rid)
    assert world.client.get(_url(world, rid), headers=world.alice.headers).status_code == 404

    published = share(world, world.alice, rid, 1)
    assert published.status_code == 200, published.text
    token = published.json()["share_token"]
    assert is_shared(world, rid)
    public = world.client.get(f"/api/public/reports/{token}")
    assert public.status_code == 200
    assert "个人记忆摘录" in [ref["snippet"] for ref in public.json()["references"]]


def case_count_is_distinct_memories_of_the_author_only(world: World) -> None:
    a1 = make_memory(world, world.alice, "a1")
    a2 = make_memory(world, world.alice, "a2")
    make_memory(world, world.owner, "o1")
    rid = make_report(world, world.alice, [
        source_ref(world.memories["a1"][1], "k1"),
        source_ref(world.memories["a1"][1], "k2"),
        memory_ref(a1, "k3"),
        memory_ref(a2, "k4"),
        source_ref(world.memories["o1"][1], "k5"),   # another member's Memory
        source_ref("src-unknown", "k6"),
        source_ref(world.doc_source, "k7"),
    ])
    assert disclosure(world, world.alice, rid).json() == {"memory_count": 2}
    assert share(world, world.alice, rid, 3).json() == _required(2, 0)
    assert share(world, world.alice, rid, 2).status_code == 200


def case_count_never_reveals_another_members_memory(world: World) -> None:
    make_memory(world, world.alice, "a1")
    rid = make_report(world, world.owner, [
        source_ref(world.memories["a1"][1], "k1"),   # Alice's Memory source
        source_ref(world.doc_source, "k2"),
    ])
    assert disclosure(world, world.owner, rid).json() == {"memory_count": 0}
    published = share(world, world.owner, rid)
    assert published.status_code == 200 and set(published.json()) == {"share_token"}


def case_non_author_cannot_publish_or_read_the_count(world: World) -> None:
    mid = make_memory(world, world.alice, "a1")
    rid = make_report(world, world.alice, [memory_ref(mid, "k1")])
    for other in (world.owner, world.admin):
        assert disclosure(world, other, rid).status_code == 404
        assert share(world, other, rid).status_code == 404
        assert share(world, other, rid, 1).status_code == 404
    assert not is_shared(world, rid)


def case_only_the_author_publishes_even_past_the_row_gate(world: World) -> None:
    """M4 is enforced on its own, not only by the row-level creator gate: with
    the gate relaxed so the notebook owner reaches the route, publishing the
    author's Memory is still refused, with the server's sentence."""
    import app.api.report_routes as routes

    mid = make_memory(world, world.alice, "a1")
    rid = make_report(world, world.alice, [memory_ref(mid, "k1")])
    no_memory = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    world.monkeypatch.setattr(
        routes, "_own_report_or_404",
        lambda repo, notebook_id, report_id: repo.get_report(notebook_id, report_id),
    )
    for acknowledged in (None, 1):
        refused = share(world, world.owner, rid, acknowledged)
        assert refused.status_code == 403, refused.text
        assert refused.headers.get("X-User-Message") == "1"
        assert refused.json() == {"detail": NON_AUTHOR_SHARE_REFUSAL}
        # The deployment admin cannot read the notebook at all.
        assert share(world, world.admin, rid, acknowledged).status_code == 404
    assert not is_shared(world, rid)
    # A report citing no Memory has nothing of the author's to protect.
    assert share(world, world.owner, no_memory).status_code == 200


def case_memory_removed_between_disclosure_and_acknowledgement(world: World) -> None:
    make_memory(world, world.alice, "a1")
    a2 = make_memory(world, world.alice, "a2")
    rid = make_report(world, world.alice, [
        source_ref(world.memories["a1"][1], "k1"),
        source_ref(world.memories["a2"][1], "k2"),
    ])
    assert disclosure(world, world.alice, rid).json() == {"memory_count": 2}
    world.repo._runtime.memory_service.deprecate(a2, world.alice.id)
    stale = share(world, world.alice, rid, 2)
    assert stale.status_code == 409 and stale.json() == _required(1, 0)
    assert not is_shared(world, rid)
    assert share(world, world.alice, rid, 1).status_code == 200


def case_memory_confirmed_between_disclosure_and_acknowledgement(world: World) -> None:
    make_memory(world, world.alice, "a1")
    late = f"src-late-{next(_KEYS)}"
    rid = make_report(world, world.alice, [
        source_ref(world.memories["a1"][1], "k1"),
        source_ref(late, "k2", "稍后才落地的记忆投影"),
    ])
    assert disclosure(world, world.alice, rid).json() == {"memory_count": 1}
    make_memory(world, world.alice, "a2", source_id=late)
    stale = share(world, world.alice, rid, 1)
    assert stale.status_code == 409
    assert stale.json() == _required(2, 1)
    assert not is_shared(world, rid)
    assert disclosure(world, world.alice, rid).json() == {"memory_count": 2}
    published = share(world, world.alice, rid, 2)
    assert published.status_code == 200
    token = published.json()["share_token"]
    assert world.client.get(f"/api/public/reports/{token}").status_code == 200


def case_unsharing_never_asks(world: World) -> None:
    mid = make_memory(world, world.alice, "a1")
    rid = make_report(world, world.alice, [memory_ref(mid, "k1")])
    token = share(world, world.alice, rid, 1).json()["share_token"]
    revoked = world.client.delete(_url(world, rid), headers=world.alice.headers)
    assert revoked.status_code == 204
    assert not is_shared(world, rid)
    assert world.client.get(f"/api/public/reports/{token}").status_code == 404
    # Publishing again asks again.
    assert share(world, world.alice, rid).json() == _required(1, 1)


def case_rules_before_the_count_are_unchanged(world: World) -> None:
    mid = make_memory(world, world.alice, "a1")
    unfinished = make_report(world, world.alice, [memory_ref(mid, "k1")], finished=False)
    refused = share(world, world.alice, unfinished, 1)
    assert refused.status_code == 409
    assert refused.json() == {"detail": "只能分享已完成的报告。"}
    rid = make_report(world, world.alice, [memory_ref(mid, "k1")])
    for body in ({"acknowledged_memory_count": -1},
                 {"acknowledged_memory_count": "1"},
                 {"acknowledged_memory_count": 1.5}):
        assert share(world, world.alice, rid, body=body).status_code == 422
    assert not is_shared(world, rid)


def delete_memory(world: World, user: User, memory_id: str) -> None:
    """Hard delete as the Memory hard-delete path does: the row and its
    projection source go; the report keeps its stored citation."""
    world.repo._runtime.memory_service.delete(memory_id, user.id)
    world.repo._runtime.source_ingestion.remove_memory_source(memory_id)


def recorded(reference: dict, memory_id: str, owner: User) -> dict:
    """A citation as the report engine stores it for the author's Memory source."""
    return {**reference, "memory_id": memory_id, "memory_owner_id": owner.id}


def case_memory_confirmed_between_the_count_and_the_flip(world: World) -> None:
    """A confirm (and its projection) committing after the route's count but
    before ``share_report`` runs: the store's own count, in the transaction
    that would set the token, refuses with the new count and issues nothing."""
    make_memory(world, world.alice, "a1")
    late = f"src-late-{next(_KEYS)}"
    rid = make_report(world, world.alice, [
        source_ref(world.memories["a1"][1], "k1"),
        source_ref(late, "k2", "稍后才落地的记忆投影"),
    ])
    assert disclosure(world, world.alice, rid).json() == {"memory_count": 1}
    original = world.repo.share_report
    landed: list[str] = []

    def share_after_a_concurrent_confirm(notebook_id, report_id, **kwargs):
        if not landed:
            landed.append(make_memory(world, world.alice, "a2", source_id=late))
        return original(notebook_id, report_id, **kwargs)

    world.monkeypatch.setattr(world.repo, "share_report", share_after_a_concurrent_confirm)
    stale = share(world, world.alice, rid, 1)
    assert landed, "the concurrent confirm must land between the two counts"
    assert stale.status_code == 409, stale.text
    assert stale.json() == _required(2, 1)
    assert not is_shared(world, rid)
    assert world.repo.report_share_token(world.notebook, rid) == ""
    published = share(world, world.alice, rid, 2)
    assert published.status_code == 200


def case_recorded_memory_citation_counts_after_the_memory_is_deleted(world: World) -> None:
    mid = make_memory(world, world.alice, "a1")
    projection = world.memories["a1"][1]
    rid = make_report(world, world.alice, [
        recorded(source_ref(projection, "k1", "私有记忆里的原话"), mid, world.alice),
        source_ref(world.doc_source, "k2"),
    ])
    delete_memory(world, world.alice, mid)
    assert world.repo._runtime.memory_store.memory_ids_for_source_ids(
        [projection], world.alice.id
    ) == []
    assert disclosure(world, world.alice, rid).json() == {"memory_count": 1}
    assert share(world, world.alice, rid).json() == _required(1, 1)
    published = share(world, world.alice, rid, 1)
    assert published.status_code == 200
    public = world.client.get(f"/api/public/reports/{published.json()['share_token']}")
    assert public.status_code == 200
    body = public.json()
    assert "私有记忆里的原话" in [ref["snippet"] for ref in body["references"]]
    for ref in body["references"]:
        assert "memory_id" not in ref and "memory_owner_id" not in ref
    # A record naming someone else never counts for this author.
    other = make_report(world, world.alice, [
        recorded(source_ref("src-x", "k1"), "mem-foreign", world.owner),
    ])
    assert disclosure(world, world.alice, other).json() == {"memory_count": 0}


def case_unrecorded_citation_of_a_deleted_memory_source_cannot_be_recognised(
    world: World,
) -> None:
    """Reports generated before citations were recorded: while the Memory
    source exists it is found live; once it is gone the fact is unrecoverable
    and the citation no longer counts (pinned, documented)."""
    mid = make_memory(world, world.alice, "a1")
    rid = make_report(world, world.alice, [
        source_ref(world.memories["a1"][1], "k1", "私有记忆里的原话"),
    ])
    assert disclosure(world, world.alice, rid).json() == {"memory_count": 1}
    delete_memory(world, world.alice, mid)
    assert disclosure(world, world.alice, rid).json() == {"memory_count": 0}
    assert share(world, world.alice, rid).status_code == 200


def case_generation_records_memory_citations(world: World) -> None:
    """Through the engine's generate → final audit → persist: the citation of
    the author's Memory projection is recorded; every other citation is stored
    byte for byte as before; the count survives deleting the Memory."""
    from app.services.reasoning_retrieval import ReasoningResult
    from tests.model_testkit import bind_chat_client

    mid = make_memory(world, world.alice, "a1")
    make_memory(world, world.owner, "o1")
    rid = make_report(world, world.alice, [], finished=False)
    world.repo.update_report(
        world.notebook, rid, status="outline_ready",
        outline=[{"title": "A", "scope": "s", "sub_queries": ["q"]}],
    )
    doc = source_ref(world.doc_source, "k1")
    foreign = source_ref(world.memories["o1"][1], "k2")
    own = source_ref(world.memories["a1"][1], "k3", "私有记忆里的原话")
    direct = memory_ref("mem-direct", "k4")   # a Memory citation needs no record

    class _Sections:
        configured = True

        def chat_json(self, messages, schema_hint, **kwargs):
            if "ONLY this section" in messages[-1]["content"]:
                return '{"markdown": "## A\\n正文", "grounded": false}'
            return '{"summary": "总"}'

    for workload_id in ("report_outline", "report_sufficiency", "report_section",
                        "report_summary", "query_rewrite", "reasoning_agent",
                        "ask_answer"):
        bind_chat_client(world.repo, workload_id, _Sections())
    engine = world.repo._runtime.report_execution.engine_factory(
        user_id=world.alice.id, cancel_event=None
    )
    world.monkeypatch.setattr(engine, "_deep_dive", lambda *a, **k: ReasoningResult())
    world.monkeypatch.setattr(
        engine, "_assemble",
        lambda *a, **k: ("# 报告\n\n正文", [], [dict(doc), dict(foreign), dict(own),
                                                 dict(direct)]),
    )
    engine.generate(world.notebook, rid, "环路为什么稳定？", depth=2)
    stored = world.repo.get_report(world.notebook, rid)
    assert stored["status"] == "done", stored.get("error")
    assert stored["references"] == [
        doc, foreign, recorded(own, mid, world.alice), direct,
    ]
    assert disclosure(world, world.alice, rid).json() == {"memory_count": 2}
    delete_memory(world, world.alice, mid)
    assert disclosure(world, world.alice, rid).json() == {"memory_count": 2}


CASES: dict[str, Callable[[World], None]] = {
    name.removeprefix("case_"): value
    for name, value in dict(globals()).items()
    if name.startswith("case_") and callable(value)
}
