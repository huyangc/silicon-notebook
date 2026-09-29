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

from app.services.share_disclosure import (
    NON_AUTHOR_SHARE_REFUSAL,
    SHARE_DISCLOSURE_REQUIRED,
)

PASSWORD = "pw12345678"
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


CASES: dict[str, Callable[[World], None]] = {
    name.removeprefix("case_"): value
    for name, value in dict(globals()).items()
    if name.startswith("case_") and callable(value)
}
