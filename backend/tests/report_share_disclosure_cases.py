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

import json
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Callable

from fastapi.testclient import TestClient

from app.services.share_disclosure import SHARE_DISCLOSURE_REQUIRED

PASSWORD = "pw12345678"
NON_AUTHOR_SHARE_REFUSAL = "报告引用了作者本人的个人记忆，只有作者可以公开分享。"
FOREIGN_MEMORY_SHARE_REFUSAL = "报告引用了其他成员的个人记忆，不能公开"
MEMORY_TEXT = "环路补偿保持稳定"
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


def counts(memory_count: int, foreign_memory_count: int = 0) -> dict:
    """The disclosure GET body."""
    return {"memory_count": memory_count, "foreign_memory_count": foreign_memory_count}


def _required(memory_count: int, new_memory_count: int) -> dict:
    return {"detail": {
        "code": SHARE_DISCLOSURE_REQUIRED,
        "memory_count": memory_count,
        "new_memory_count": new_memory_count,
    }}


# --- scenarios ---------------------------------------------------------------


def case_report_without_memory_publishes_as_before(world: World) -> None:
    rid = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    assert disclosure(world, world.alice, rid).json() == counts(0)
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
    assert disclosure(world, world.alice, rid).json() == counts(1)

    missing = share(world, world.alice, rid)
    assert missing.status_code == 409
    assert missing.json() == _required(1, 1)
    too_few = share(world, world.alice, rid, 0)
    assert too_few.status_code == 409 and too_few.json() == _required(1, 1)
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
        source_ref("src-unknown", "k6"),
        source_ref(world.doc_source, "k7"),
    ])
    assert disclosure(world, world.alice, rid).json() == counts(2)
    assert share(world, world.alice, rid, 1).json() == _required(2, 1)
    assert share(world, world.alice, rid, 2).status_code == 200


def _refused_as_foreign(response) -> None:
    assert response.status_code == 403, response.text
    assert response.headers.get("X-User-Message") == "1"
    assert response.json() == {"detail": FOREIGN_MEMORY_SHARE_REFUSAL}


def case_another_members_memory_is_never_published(world: World) -> None:
    """B2: the owner's report cites Alice's Memory.  The disclosure says so
    before anyone clicks, publishing is refused whatever is acknowledged, and
    nothing is shared.  Unsharing an earlier link stays allowed."""
    a1 = make_memory(world, world.alice, "a1")
    alice_source = world.memories["a1"][1]
    rid = make_report(world, world.owner, [
        source_ref(alice_source, "k1", "记忆 a1：环路补偿保持稳定。"),
        source_ref(world.doc_source, "k2"),
    ])
    assert disclosure(world, world.owner, rid).json() == counts(0, 1)
    for acknowledged in (None, 0, 1):
        _refused_as_foreign(share(world, world.owner, rid, acknowledged))
    assert not is_shared(world, rid)
    assert world.repo.report_share_token(world.notebook, rid) == ""

    # A citation recorded at generation time as Alice's keeps the report
    # refused after her Memory is gone.
    recorded_rid = make_report(world, world.owner, [
        recorded(source_ref(alice_source, "k1"), a1, world.alice),
    ])
    delete_memory(world, world.alice, a1)
    assert disclosure(world, world.owner, recorded_rid).json() == counts(0, 1)
    _refused_as_foreign(share(world, world.owner, recorded_rid))
    # Without a record, a citation whose Memory source is gone can no longer
    # be recognised (documented): that old report publishes.
    assert disclosure(world, world.owner, rid).json() == counts(0, 0)

    # A link issued before this rule: the anonymous page refuses it on every
    # open (the same 404 as a revoked link), and it can still be revoked.
    make_memory(world, world.alice, "a2")
    earlier = make_report(world, world.owner, [source_ref(world.memories["a2"][1], "k1")])
    token = world.repo.share_report(world.notebook, earlier)
    gone = world.client.get(f"/api/public/reports/{token}")
    assert gone.status_code == 404
    assert gone.json() == {"detail": "shared report not found"}
    recorded_earlier = make_report(world, world.owner, [
        recorded(source_ref("src-gone", "k1"), "mem-gone", world.alice),
    ])
    recorded_token = world.repo.share_report(world.notebook, recorded_earlier)
    assert world.client.get(f"/api/public/reports/{recorded_token}").status_code == 404
    # A link to a report carrying only its author's Memory keeps serving.
    make_memory(world, world.owner, "o1")
    own = make_report(world, world.owner, [
        recorded(source_ref(world.memories["o1"][1], "k1", "主人自己的记忆"),
                 world.memories["o1"][0], world.owner),
    ])
    own_token = world.repo.share_report(world.notebook, own)
    served = world.client.get(f"/api/public/reports/{own_token}")
    assert served.status_code == 200
    assert served.headers.get("Cache-Control") == "no-store"
    assert [ref["snippet"] for ref in served.json()["references"]] == ["主人自己的记忆"]
    assert disclosure(world, world.owner, earlier).json() == counts(0, 1)
    # Re-sharing it is refused too (it does not hand the link out again).
    _refused_as_foreign(share(world, world.owner, earlier))
    revoked = world.client.delete(_url(world, earlier), headers=world.owner.headers)
    assert revoked.status_code == 204
    assert world.client.get(f"/api/public/reports/{token}").status_code == 404
    _refused_as_foreign(share(world, world.owner, earlier))


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
    """Downward: a report generated before recording loses one recognisable
    Memory between disclosure and click.  The author acknowledged 2, the
    recount says 1, the page still carries both stored excerpts — the
    acknowledgement covers it, so it publishes (P2-2)."""
    make_memory(world, world.alice, "a1")
    a2 = make_memory(world, world.alice, "a2")
    rid = make_report(world, world.alice, [
        source_ref(world.memories["a1"][1], "k1", "第一条记忆的摘录"),
        source_ref(world.memories["a2"][1], "k2", "第二条记忆的摘录"),
    ])
    assert disclosure(world, world.alice, rid).json() == counts(2)
    world.repo._runtime.memory_service.deprecate(a2, world.alice.id)
    assert disclosure(world, world.alice, rid).json() == counts(1)
    published = share(world, world.alice, rid, 2)
    assert published.status_code == 200, published.text
    public = world.client.get(f"/api/public/reports/{published.json()['share_token']}")
    assert [ref["snippet"] for ref in public.json()["references"]] == [
        "第一条记忆的摘录", "第二条记忆的摘录",
    ]


def case_memory_confirmed_between_disclosure_and_acknowledgement(world: World) -> None:
    make_memory(world, world.alice, "a1")
    late = f"src-late-{next(_KEYS)}"
    rid = make_report(world, world.alice, [
        source_ref(world.memories["a1"][1], "k1"),
        source_ref(late, "k2", "稍后才落地的记忆投影"),
    ])
    assert disclosure(world, world.alice, rid).json() == counts(1)
    make_memory(world, world.alice, "a2", source_id=late)
    stale = share(world, world.alice, rid, 1)
    assert stale.status_code == 409
    assert stale.json() == _required(2, 1)
    assert not is_shared(world, rid)
    assert disclosure(world, world.alice, rid).json() == counts(2)
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
    assert disclosure(world, world.alice, rid).json() == counts(1)
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


def case_memory_removed_between_the_count_and_the_flip(world: World) -> None:
    """Downward inside the share transaction: a cited Memory stops being
    recognisable after the route's count; the store's recount is lower than
    the acknowledgement and the report publishes."""
    make_memory(world, world.alice, "a1")
    a2 = make_memory(world, world.alice, "a2")
    rid = make_report(world, world.alice, [
        source_ref(world.memories["a1"][1], "k1"),
        source_ref(world.memories["a2"][1], "k2"),
    ])
    original = world.repo.share_report
    removed: list[str] = []

    def share_after_a_concurrent_removal(notebook_id, report_id, **kwargs):
        if not removed:
            world.repo._runtime.memory_service.deprecate(a2, world.alice.id)
            removed.append(a2)
        return original(notebook_id, report_id, **kwargs)

    world.monkeypatch.setattr(world.repo, "share_report", share_after_a_concurrent_removal)
    published = share(world, world.alice, rid, 2)
    assert removed and published.status_code == 200, published.text
    assert is_shared(world, rid)


def case_resharing_a_public_report_returns_its_link(world: World) -> None:
    """P3-5: an already public report publishes nothing new; any POST returns
    the existing link without asking."""
    mid = make_memory(world, world.alice, "a1")
    rid = make_report(world, world.alice, [memory_ref(mid, "k1")])
    token = share(world, world.alice, rid, 1).json()["share_token"]
    for acknowledged in (None, 0, 5):
        again = share(world, world.alice, rid, acknowledged)
        assert again.status_code == 200 and again.json() == {"share_token": token}


def case_a_report_deleted_while_the_share_waits_is_not_found(world: World) -> None:
    """P3-2: the report row disappears inside the share transaction (deleted
    with its notebook while the share waited for a cited row): 404, not 500,
    and nothing is issued."""
    import app.repositories.postgres.report_store as pg_store
    import app.repositories.sqlite.report_store as sqlite_store

    make_memory(world, world.alice, "a1")
    rid = make_report(world, world.alice, [source_ref(world.memories["a1"][1], "k1")])
    postgres = "postgres" in type(world.repo._runtime.database).__module__
    placeholder = "%s" if postgres else "?"
    store_module = pg_store if postgres else sqlite_store
    real = store_module.MemoryStore.memory_sources_on

    def delete_then_read(db, source_ids, owner_id, *, lock=False):
        if lock:     # the share transaction's own read, after it found the row
            db.execute(f"DELETE FROM reports WHERE id={placeholder}", (rid,))
        return real(db, source_ids, owner_id, lock=lock)

    world.monkeypatch.setattr(
        store_module.MemoryStore, "memory_sources_on", staticmethod(delete_then_read)
    )
    gone = share(world, world.alice, rid, 1)
    assert gone.status_code == 404, gone.text
    assert gone.json() == {"detail": "Report not found"}


def case_generation_fails_closed_when_the_record_lookup_fails(world: World) -> None:
    """P3-3: a report whose Memory record cannot be taken is not stored as
    done (it would later under-count what its page carries); it fails, keeps
    its outline for a retry, and cannot be shared."""
    make_memory(world, world.alice, "a1")
    models = _Models(section_markdown="## 结论\n正文")
    engine = _engine(world, world.alice, models)
    rid = _new_report(world, world.alice)
    _outline_ready(world, rid)
    _serve_memory(world, [])
    world.monkeypatch.setattr(
        engine, "_assemble",
        lambda *a, **k: ("# 报告\n\n正文", [], [source_ref(world.doc_source, "k1")]),
    )

    def lookup_down(*args, **kwargs):
        raise RuntimeError("lookup down")

    world.monkeypatch.setattr(
        world.repo._runtime.memory_store, "memory_sources_for_source_ids", lookup_down
    )
    assert world.repo.claim_report_generation(world.notebook, rid)
    engine.generate(world.notebook, rid, "环路为什么稳定？", depth=2)
    stored = world.repo.get_report(world.notebook, rid)
    assert stored["status"] == "failed"
    assert stored["outline"]
    refused = share(world, world.alice, rid)
    assert refused.status_code == 409
    assert refused.json() == {"detail": "只能分享已完成的报告。"}


def case_admin_audit_detail_does_not_carry_the_memory_record(world: World) -> None:
    """P3-8: the admin activity detail shows the citation and its excerpt,
    not the engine's internal record handles."""
    mid = make_memory(world, world.alice, "a1")
    rid = make_report(world, world.alice, [
        recorded(source_ref(world.memories["a1"][1], "k1", "私有记忆里的原话"),
                 mid, world.alice),
    ])
    detail = world.client.get(
        f"/api/admin/users/{world.alice.id}/reports/{rid}", headers=world.admin.headers
    )
    assert detail.status_code == 200, detail.text
    references = detail.json()["references"]
    assert [ref["snippet"] for ref in references] == ["私有记忆里的原话"]
    assert all("memory_id" not in ref and "memory_owner_id" not in ref
               for ref in references)


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
    assert disclosure(world, world.alice, rid).json() == counts(1)
    assert share(world, world.alice, rid).json() == _required(1, 1)
    published = share(world, world.alice, rid, 1)
    assert published.status_code == 200
    public = world.client.get(f"/api/public/reports/{published.json()['share_token']}")
    assert public.status_code == 200
    body = public.json()
    assert "私有记忆里的原话" in [ref["snippet"] for ref in body["references"]]
    for ref in body["references"]:
        assert "memory_id" not in ref and "memory_owner_id" not in ref
    # A record naming someone else never counts as the author's own; it is
    # another member's Memory, and the report is not published.
    other = make_report(world, world.alice, [
        recorded(source_ref("src-x", "k1"), "mem-foreign", world.owner),
    ])
    assert disclosure(world, world.alice, other).json() == counts(0, 1)
    _refused_as_foreign(share(world, world.alice, other))


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
    assert disclosure(world, world.alice, rid).json() == counts(1)
    delete_memory(world, world.alice, mid)
    assert disclosure(world, world.alice, rid).json() == counts(0)
    assert share(world, world.alice, rid).status_code == 200


def case_generation_records_memory_citations(world: World) -> None:
    """Through the engine's generate → final audit → persist: citations of a
    Memory projection are recorded with their owner (the author's own, and
    another member's, which blocks publication); every other citation is
    stored byte for byte as before; the count survives deleting the Memory."""
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
        doc, recorded(foreign, world.memories["o1"][0], world.owner),
        recorded(own, mid, world.alice), direct,
    ]
    assert disclosure(world, world.alice, rid).json() == counts(2, 1)
    delete_memory(world, world.alice, mid)
    delete_memory(world, world.owner, world.memories["o1"][0])
    assert disclosure(world, world.alice, rid).json() == counts(2, 1)
    _refused_as_foreign(share(world, world.alice, rid, 2))


# --- B1: Memory that entered a prompt counts, cited or not ------------------------
#
# Each case below runs the real report engine (planning and/or generation) with
# fake models, lets exactly ONE path carry the author's Memory into a prompt,
# has the model restate it WITHOUT a citation marker, and then goes through
# the real disclosure / share / public routes.


class _Models:
    """Fake chat models for a real plan → generate run; records every prompt."""

    configured = True

    def __init__(self, *, outline_title: str = "结论",
                 section_markdown: str = "## 结论\n正文") -> None:
        self.outline_title = outline_title
        self.section_markdown = section_markdown
        self.prompts: list[str] = []
        self.synthesis_prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        self.prompts.append(content)
        if "must_contrast" in str(schema_hint):
            self.synthesis_prompts.append(content)
        if "ONLY this section" in content:
            return json.dumps(
                {"markdown": self.section_markdown, "grounded": True}, ensure_ascii=False
            )
        if "PRE-WRITING" in content:
            return json.dumps({"sections": [{
                "title": self.outline_title, "scope": "s", "sub_queries": ["环路补偿"],
            }]}, ensure_ascii=False)
        if "EXECUTIVE SUMMARY" in content:
            return json.dumps({"summary": "总"}, ensure_ascii=False)
        return "{}"


def _engine(world: World, author: User, models: _Models, deep_dive=None):
    from app.services.reasoning_retrieval import ReasoningResult
    from tests.model_testkit import bind_chat_client

    for workload_id in ("report_outline", "report_sufficiency", "report_section",
                        "report_summary", "query_rewrite", "reasoning_agent",
                        "ask_answer"):
        bind_chat_client(world.repo, workload_id, models)
    engine = world.repo._runtime.report_execution.engine_factory(
        user_id=author.id, cancel_event=None
    )
    world.monkeypatch.setattr(
        engine, "_deep_dive", deep_dive or (lambda *a, **k: ReasoningResult())
    )
    return engine


def _serve_memory(world: World, keys: list[str]) -> list[str]:
    """Make the author's Memory retrieval return exactly these Memory entries
    (from now on; call again with [] to stop).  Returns their ids."""
    from app.models.memory import MemoryHit

    hits = [
        MemoryHit(memory_id=world.memories[key][0], title=f"记忆 {key}",
                  text=f"记忆 {key}：{MEMORY_TEXT}。", status="confirmed",
                  authority=1, score=1.0)
        for key in keys
    ]
    world.monkeypatch.setattr(
        world.repo._runtime.memory_retriever, "notebook_memory_hits",
        lambda *args, **kwargs: list(hits),
    )
    return [hit.memory_id for hit in hits]


def _new_report(world: World, author: User) -> str:
    response = world.client.post(
        f"/api/notebooks/{world.notebook}/reports",
        json={"question": "环路为什么稳定？"}, headers=author.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["report_id"]


def _generate(world: World, engine, rid: str) -> dict:
    assert world.repo.claim_report_generation(world.notebook, rid)
    engine.generate(world.notebook, rid, "环路为什么稳定？", depth=2)
    stored = world.repo.get_report(world.notebook, rid)
    assert stored["status"] == "done", stored.get("error")
    return stored


def _outline_ready(world: World, rid: str) -> None:
    world.repo.update_report(
        world.notebook, rid, status="outline_ready",
        outline=[{"title": "结论", "scope": "s", "sub_queries": ["环路补偿"]}],
    )


def _published_only_after_acknowledging(
    world: World, rid: str, memory_ids: list[str], *, cited: bool = False
) -> dict:
    """The recorded use is on the stored report but not in its API shape, the
    count covers it, nothing is issued without the acknowledgement, and the
    anonymous page carries the Memory sentence only after it."""
    stored = world.repo.get_report(world.notebook, rid)
    assert stored["memory_used"] == sorted(memory_ids)
    assert "_memory_used" not in stored["understanding"]
    assert all("memory_used" not in section for section in stored["sections"])
    if not cited:
        assert not any(
            ref.get("memory_id") or ref.get("object_type") == "memory"
            for ref in stored["references"]
        ), stored["references"]
    detail = world.client.get(
        f"/api/notebooks/{world.notebook}/reports/{rid}", headers=world.alice.headers
    )
    assert detail.status_code == 200
    assert "memory_used" not in detail.text and "_memory_used" not in detail.text
    assert MEMORY_TEXT in stored["content_md"]

    assert disclosure(world, world.alice, rid).json() == counts(len(memory_ids))
    missing = share(world, world.alice, rid)
    assert missing.status_code == 409
    assert missing.json() == _required(len(memory_ids), len(memory_ids))
    assert world.repo.report_share_token(world.notebook, rid) == ""
    published = share(world, world.alice, rid, len(memory_ids))
    assert published.status_code == 200, published.text
    public = world.client.get(f"/api/public/reports/{published.json()['share_token']}")
    assert public.status_code == 200
    assert MEMORY_TEXT in public.json()["content_md"]
    assert "memory_used" not in public.text and "memory_id" not in public.text
    return public.json()


def case_memory_shown_to_the_outline_planner_counts(world: World) -> None:
    """Path 1: the corpus map shows the planner the author's Memory; the
    planner copies it into a section title; no citation ever names it."""
    make_memory(world, world.alice, "a1")
    models = _Models(outline_title=MEMORY_TEXT,
                     section_markdown=f"## {MEMORY_TEXT}\n正文")
    engine = _engine(world, world.alice, models)
    # Retrieval hands the planner nothing (it would otherwise find the Memory
    # projection by its text and record it): only the Memory lines carry it.
    for method in ("federated_retrieve", "retrieve_elements", "ppr_retrieve"):
        world.monkeypatch.setattr(
            engine.dependencies.retrieval, method, lambda *a, **k: []
        )
    rid = _new_report(world, world.alice)
    memory_ids = _serve_memory(world, ["a1"])
    engine.plan_outline(world.notebook, rid, "环路为什么稳定？")
    assert world.repo.get_report(world.notebook, rid)["status"] == "outline_ready"
    assert any("PRE-WRITING" in p and f"记忆 a1：{MEMORY_TEXT}" in p
               for p in models.prompts), "the planner never saw the Memory"
    # The outline edit round-trip (the route rebuilds understanding and outline
    # from what the client sends) keeps the planner's record.
    outline = world.repo.get_report(world.notebook, rid)["outline"]
    edited = world.client.patch(
        f"/api/notebooks/{world.notebook}/reports/{rid}/outline",
        json={"sections": outline}, headers=world.alice.headers,
    )
    assert edited.status_code == 200, edited.text
    _serve_memory(world, [])                  # generation sees no Memory at all
    _generate(world, engine, rid)
    _published_only_after_acknowledging(world, rid, memory_ids)


def case_memory_block_in_a_section_prompt_counts(world: World) -> None:
    """Path 2: the section's confirmed-Memory block; the section restates it
    without a marker."""
    make_memory(world, world.alice, "a1")
    make_memory(world, world.alice, "a2")
    models = _Models(section_markdown=f"## 结论\n{MEMORY_TEXT}。")
    engine = _engine(world, world.alice, models)
    rid = _new_report(world, world.alice)
    _outline_ready(world, rid)
    memory_ids = _serve_memory(world, ["a1", "a2"])
    _generate(world, engine, rid)
    assert any("ONLY this section" in p and "[Confirmed Memory]" in p
               and f"记忆 a2：{MEMORY_TEXT}" in p for p in models.prompts)
    _published_only_after_acknowledging(world, rid, memory_ids)


def case_memory_projection_evidence_in_a_section_prompt_counts(world: World) -> None:
    """Path 3: evidence from the author's Memory projection source reaches the
    section prompt as a source element; the section restates it uncited."""
    from app.domain.retrieval import RetrievedElement
    from app.services.reasoning_retrieval import ReasoningResult

    mid = make_memory(world, world.alice, "a1")
    projection = world.memories["a1"][1]
    elements = [
        RetrievedElement(
            element_id=element.id, source_id=projection, source_title="记忆 a1",
            location_label=element.location_label or "段落",
            element_type=element.element_type, text=element.text, score=1.0,
        )
        for element in world.repo.source_elements(projection)
    ]
    assert elements, "the Memory projection has elements"
    models = _Models(section_markdown=f"## 结论\n{MEMORY_TEXT}。")
    engine = _engine(world, world.alice, models,
                     deep_dive=lambda *a, **k: ReasoningResult(elements=list(elements)))
    rid = _new_report(world, world.alice)
    _outline_ready(world, rid)
    _serve_memory(world, [])                  # no confirmed-Memory block
    _generate(world, engine, rid)
    assert any("ONLY this section" in p and "[source-element]" in p
               and MEMORY_TEXT in p and "[Confirmed Memory]" not in p
               for p in models.prompts)
    _published_only_after_acknowledging(world, rid, [mid])


def case_compound_marker_keeps_its_known_memory_citation(world: World) -> None:
    """``[k3001, k99]`` where k3001 is the author's Memory and k99 is made up:
    only k99 is dropped; the Memory stays cited and the page lists it."""
    make_memory(world, world.alice, "a1")
    models = _Models(section_markdown=f"## 结论\n{MEMORY_TEXT} [k3001, k99]。")
    engine = _engine(world, world.alice, models)
    rid = _new_report(world, world.alice)
    _outline_ready(world, rid)
    memory_ids = _serve_memory(world, ["a1"])
    stored = _generate(world, engine, rid)
    assert [(ref["key"], ref["object_type"], ref["object_id"])
            for ref in stored["references"]] == [("k1", "memory", memory_ids[0])]
    assert f"{MEMORY_TEXT} [k1]。" in stored["content_md"]
    assert "k99" not in stored["content_md"]
    public = _published_only_after_acknowledging(world, rid, memory_ids, cited=True)
    assert [ref["snippet"] for ref in public["references"]] == [
        f"记忆 a1：{MEMORY_TEXT}。"
    ]


def _projection_elements(world: World, key: str) -> list:
    from app.domain.retrieval import RetrievedElement

    projection = world.memories[key][1]
    elements = [
        RetrievedElement(
            element_id=element.id, source_id=projection, source_title=f"记忆 {key}",
            location_label=element.location_label or "段落",
            element_type=element.element_type, text=element.text, score=1.0,
        )
        for element in world.repo.source_elements(projection)
    ]
    assert elements, "the Memory projection has elements"
    return elements


def case_memory_retrieved_for_the_outline_planner_counts(world: World) -> None:
    """Planning sites fed by retrieval: the corpus map's knowledge lines and the
    coverage / sufficiency probes that share its calls.  A knowledge hit whose
    evidence is the author's Memory projection reaches the planner by name;
    there is no Memory line and no citation.  Generation runs in a separate
    engine (a separate job in production), so only the planning record can
    carry it."""
    from app.domain.retrieval import RetrievedKnowledge
    from app.models.common import Evidence

    mid = make_memory(world, world.alice, "a1")
    projection = world.memories["a1"][1]
    hit = RetrievedKnowledge(
        object_id="ko-memory", object_type="concept",
        payload={"name": MEMORY_TEXT, "source_id": projection},
        evidence=[Evidence(
            source_id=projection, source_title="记忆 a1", element_id="",
            element_type="paragraph", location_label="段落",
            quoted_span=MEMORY_TEXT, confidence=1.0,
        )],
        score=1.0, relevance=1.0,
    )
    models = _Models(outline_title=MEMORY_TEXT, section_markdown=f"## {MEMORY_TEXT}\n正文")
    planner = _engine(world, world.alice, models)
    rid = _new_report(world, world.alice)
    _serve_memory(world, [])
    world.monkeypatch.setattr(
        planner.dependencies.retrieval, "federated_retrieve", lambda *a, **k: [hit]
    )
    planner.plan_outline(world.notebook, rid, "环路为什么稳定？")
    assert world.repo.get_report(world.notebook, rid)["status"] == "outline_ready"
    assert any("PRE-WRITING" in p and MEMORY_TEXT in p for p in models.prompts)
    world.monkeypatch.setattr(
        planner.dependencies.retrieval, "federated_retrieve", lambda *a, **k: []
    )
    _generate(world, _engine(world, world.alice, models), rid)
    _published_only_after_acknowledging(world, rid, [mid])


def case_memory_the_deep_dive_observed_counts_even_if_dropped(world: World) -> None:
    """Section deep-dive agent: it retrieves the author's Memory projection
    through the engine's retrieval port (what its planning and reflection
    turns are shown) and ends up keeping nothing, so no section context, no
    synthesis payload and no citation carries it.  The report still records
    it."""
    from app.services.reasoning_retrieval import ReasoningResult

    mid = make_memory(world, world.alice, "a1")
    elements = _projection_elements(world, "a1")
    models = _Models(section_markdown=f"## 结论\n{MEMORY_TEXT}。")
    observed: list[int] = []

    def observe_then_drop(*args, **kwargs):
        found = engine.dependencies.retrieval.retrieve_elements(
            world.notebook, "环路补偿", limit=5
        )
        observed.append(len(found))
        return ReasoningResult()

    engine = _engine(world, world.alice, models, deep_dive=observe_then_drop)
    world.monkeypatch.setattr(
        engine.dependencies.retrieval, "retrieve_elements",
        lambda *a, **k: list(elements),
    )
    rid = _new_report(world, world.alice)
    _outline_ready(world, rid)
    _serve_memory(world, [])
    _generate(world, engine, rid)
    assert observed and observed[0] > 0
    assert not any("[source-element]" in p for p in models.prompts)
    _published_only_after_acknowledging(world, rid, [mid])


def case_memory_in_the_synthesis_payload_counts(world: World) -> None:
    """Report-wide synthesis: two sections, the deep dive keeps the author's
    Memory projection element, the synthesis prompt carries it, and section
    drafting (replaced here) sees no evidence at all — the synthesis payload
    is the only prompt that read it."""
    from app.services.reasoning_retrieval import ReasoningResult

    mid = make_memory(world, world.alice, "a1")
    elements = _projection_elements(world, "a1")
    models = _Models()

    def keep_the_memory(*args, **kwargs):
        return ReasoningResult(elements=list(
            engine.dependencies.retrieval.retrieve_elements(
                world.notebook, "环路补偿", limit=5
            )
        ))

    def draft_without_evidence(notebook_id, section, question, result, depth=None, **kw):
        return {
            "title": section["title"], "scope": section["scope"],
            "markdown": f"## {section['title']}\n{MEMORY_TEXT}。", "grounded": True,
            "id_map": {}, "attempted": [], "claims": [],
            "claim_ledger_status": "missing", "synthesis_used": True,
            "synthesis_requested": True,
        }

    engine = _engine(world, world.alice, models, deep_dive=keep_the_memory)
    world.monkeypatch.setattr(engine, "_draft_section", draft_without_evidence)
    world.monkeypatch.setattr(
        engine.dependencies.retrieval, "retrieve_elements",
        lambda *a, **k: list(elements),
    )
    rid = _new_report(world, world.alice)
    world.repo.update_report(
        world.notebook, rid, status="outline_ready",
        outline=[{"title": "结论", "scope": "s", "sub_queries": ["环路补偿"]},
                 {"title": "对比", "scope": "c", "sub_queries": ["环路稳定"]}],
    )
    _serve_memory(world, [])
    _generate(world, engine, rid)
    assert any(MEMORY_TEXT in p for p in models.synthesis_prompts), \
        "the synthesis prompt never carried the Memory"
    _published_only_after_acknowledging(world, rid, [mid])


def case_planning_and_generation_records_are_joined(world: World) -> None:
    """The planner records one Memory (its Memory lines), the generation run
    another (the deep dive retrieved its projection): the report carries
    both — the run record joins the planning record, it does not replace it."""
    from app.services.reasoning_retrieval import ReasoningResult

    planned = make_memory(world, world.alice, "a1")
    retrieved = make_memory(world, world.alice, "a2")
    elements = _projection_elements(world, "a2")
    models = _Models(outline_title=MEMORY_TEXT, section_markdown=f"## {MEMORY_TEXT}\n正文")
    planner = _engine(world, world.alice, models)
    for method in ("federated_retrieve", "retrieve_elements", "ppr_retrieve"):
        world.monkeypatch.setattr(planner.dependencies.retrieval, method, lambda *a, **k: [])
    rid = _new_report(world, world.alice)
    _serve_memory(world, ["a1"])
    planner.plan_outline(world.notebook, rid, "环路为什么稳定？")
    assert world.repo.get_report(world.notebook, rid)["memory_used"] == [planned]

    def observe_then_drop(*args, **kwargs):
        generator.dependencies.retrieval.retrieve_elements(world.notebook, "环路补偿", limit=5)
        return ReasoningResult()

    generator = _engine(world, world.alice, models, deep_dive=observe_then_drop)
    world.monkeypatch.setattr(
        generator.dependencies.retrieval, "retrieve_elements", lambda *a, **k: list(elements)
    )
    _serve_memory(world, [])
    _generate(world, generator, rid)
    _published_only_after_acknowledging(world, rid, [planned, retrieved])


def case_a_retrieval_record_overflow_fails_the_planning(world: World) -> None:
    """A retrieval result too large to record completely fails the planning
    (no outline to generate, nothing to share) instead of under-counting."""
    import app.services.report_memory_use as memory_use

    make_memory(world, world.alice, "a1")
    models = _Models()
    planner = _engine(world, world.alice, models)
    world.monkeypatch.setattr(memory_use, "_MAX_CONTAINERS", 3)
    from app.domain.retrieval import RetrievedKnowledge

    hits = [
        RetrievedKnowledge(object_id=f"o{index}", object_type="concept",
                           payload={"name": f"n{index}", "source_id": f"src-{index}"})
        for index in range(5)
    ]
    world.monkeypatch.setattr(
        planner.dependencies.retrieval, "federated_retrieve", lambda *a, **k: list(hits)
    )
    rid = _new_report(world, world.alice)
    _serve_memory(world, [])
    planner.plan_outline(world.notebook, rid, "环路为什么稳定？")
    stored = world.repo.get_report(world.notebook, rid)
    assert stored["status"] == "failed", (stored["status"], stored["error"])
    assert "too large to record" in stored["error"]


def case_a_retrieval_record_overflow_fails_the_generation(world: World) -> None:
    """The generation twin: a retrieval result the deep dive was handed is too
    large to record completely, so the report fails (its outline kept for a
    retry, nothing to share) instead of being stored with a short record."""
    import app.services.report_memory_use as memory_use
    from app.domain.retrieval import RetrievedKnowledge
    from app.services.reasoning_retrieval import ReasoningResult

    hits = [
        RetrievedKnowledge(object_id=f"o{index}", object_type="concept",
                           payload={"name": f"n{index}", "source_id": f"src-{index}"})
        for index in range(5)
    ]

    def observe(*args, **kwargs):
        generator.dependencies.retrieval.federated_retrieve(world.notebook, "环路")
        return ReasoningResult()

    generator = _engine(world, world.alice, _Models(), deep_dive=observe)
    world.monkeypatch.setattr(
        generator.dependencies.retrieval, "federated_retrieve", lambda *a, **k: list(hits)
    )
    rid = _new_report(world, world.alice)
    _outline_ready(world, rid)
    _serve_memory(world, [])
    world.monkeypatch.setattr(memory_use, "_MAX_CONTAINERS", 3)
    assert world.repo.claim_report_generation(world.notebook, rid)
    generator.generate(world.notebook, rid, "环路为什么稳定？", depth=2)
    stored = world.repo.get_report(world.notebook, rid)
    assert stored["status"] == "failed", (stored["status"], stored["error"])
    assert "too large to record" in stored["error"]
    assert stored["outline"]
    refused = share(world, world.alice, rid)
    assert refused.status_code == 409
    assert refused.json() == {"detail": "只能分享已完成的报告。"}


def case_planner_record_is_kept_by_the_store(world: World) -> None:
    """The planner's Memory record lives in ``understanding_json`` but belongs
    to the store: every later understanding write that does not carry it keeps
    it, a new plan's record replaces it, a new intent claim drops it, and a
    report without one never gains one."""
    repo, nb = world.repo, world.notebook
    rid = _new_report(world, world.alice)
    repo.update_report(nb, rid, understanding={"a": 1, "_memory_used": ["m1"]})
    assert repo.get_report(nb, rid)["memory_used"] == ["m1"]
    repo.update_report(nb, rid, understanding={"a": 2})
    stored = repo.get_report(nb, rid)
    assert stored["memory_used"] == ["m1"] and stored["understanding"] == {"a": 2}
    repo.update_report(nb, rid, understanding={"a": 3, "_memory_used": ["m2"]})
    assert repo.get_report(nb, rid)["memory_used"] == ["m2"]
    repo.update_report(nb, rid, status="outline_ready")
    assert repo.claim_report_generation(nb, rid, {"a": 4})
    stored = repo.get_report(nb, rid)
    assert stored["memory_used"] == ["m2"] and stored["understanding"] == {"a": 4}
    repo.update_report(nb, rid, status="intent_ready")
    assert repo.claim_report_intent(nb, rid, {"b": 1})
    assert "memory_used" not in repo.get_report(nb, rid)

    plain = _new_report(world, world.alice)
    repo.update_report(nb, plain, understanding={"a": 1, "note": None})
    repo.update_report(nb, plain, status="outline_ready")
    assert repo.claim_report_generation(nb, plain, {"a": 2, "note": None})
    stored = repo.get_report(nb, plain)
    assert "memory_used" not in stored
    assert stored["understanding"] == {"a": 2, "note": None}


CASES: dict[str, Callable[[World], None]] = {
    name.removeprefix("case_"): value
    for name, value in dict(globals()).items()
    if name.startswith("case_") and callable(value)
}
