"""Named sharing API (share-edit P1-T6), shared by both backends.

``test_notebook_collaborators_api.py`` runs every case on SQLite and
``postgres/test_notebook_collaborators_api_pg.py`` runs the same cases on
PostgreSQL, through the real routes of a fresh app. One world: the owner's
notebook, a group admin (``group_admins`` admin edge), a link member, a
stranger, and users to name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi.testclient import TestClient

from app.models.memory import MemberExitCompletion

_STAMP = "2026-10-28T00:00:00+00:00"


@dataclass
class World:
    client: TestClient
    postgres: bool
    notebook: str
    group: str
    headers: dict[str, dict] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)
    usernames: dict[str, str] = field(default_factory=dict)

    def url(self, user: str = "", suffix: str = "") -> str:
        base = f"/api/notebooks/{self.notebook}/collaborators"
        return base + (f"/{self.users[user]}" if user else "") + suffix

    def write(self, text: str, params: tuple) -> None:
        with _repository()._runtime.database.write() as db:
            db.execute(text.replace("?", "%s") if self.postgres else text, params)

    def count(self, text: str, params: tuple = ()) -> int:
        with _repository()._runtime.database.connect() as db:
            row = db.execute(
                text.replace("?", "%s") if self.postgres else text, params
            ).fetchone()
        return int(row["c"])


def _repository():
    from app.api.deps import repository

    return repository()


def _login(client: TestClient, username: str) -> dict:
    client.post("/api/auth/register", json={"username": username, "password": "pw123456"})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw123456"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


_NAMES = ("owner", "editor", "second", "deputy", "member", "stranger", "carol")


def build_world(client: TestClient, *, postgres: bool) -> World:
    usernames = {name: f"k{20261400 + index:08d}" for index, name in enumerate(_NAMES)}
    headers = {name: _login(client, usernames[name]) for name in _NAMES}
    users = {name: client.get("/api/me", headers=h).json()["id"] for name, h in headers.items()}
    owner = headers["owner"]
    notebook = client.post("/api/notebooks", json={"name": "协作"}, headers=owner).json()["id"]
    group = client.post("/api/groups", json={"name": "组"}, headers=owner).json()["id"]
    assert client.put(
        f"/api/groups/{group}/members/{users['deputy']}", json={"role": "admin"},
        headers=owner,
    ).status_code == 200
    assert client.post(
        f"/api/notebooks/{notebook}/grants",
        json={"principal_type": "group_admins", "principal_id": group, "role": "admin"},
        headers=owner,
    ).status_code == 200
    _repository()._runtime.sharing.add_member(notebook, users["member"])
    return World(client=client, postgres=postgres, notebook=notebook, group=group,
                 headers=headers, users=users, usernames=usernames)


def _put(world: World, user: str, who: str = "owner"):
    return world.client.put(world.url(user), headers=world.headers[who])


def _memory(world: World, user: str, n: int) -> None:
    from tests.memory_purge_cases import wire_world

    wire_world(_repository())
    service = _repository()._runtime.memory_service
    for index in range(n):
        service.create_candidate(
            world.notebook, world.users[user], None, f"req-{user}-{index}",
            f"t{index}", f"Body {index}.", [], "reason", {}, [],
        )


def _memory_count(world: World, user: str) -> int:
    return world.count(
        "SELECT COUNT(*) AS c FROM memory_items WHERE notebook_id=? AND created_by=?",
        (world.notebook, world.users[user]),
    )


# ---------------------------------------------------------------- cases


def collaborator_endpoints_owner_only_404_for_others(world: World) -> None:
    client = world.client
    assert _put(world, "editor").status_code == 200
    for who in ("deputy", "member", "stranger"):
        assert client.get(world.url(), headers=world.headers[who]).status_code == 404, who
        assert _put(world, "second", who).status_code == 404, who
        assert client.delete(world.url("editor"), headers=world.headers[who]).status_code == 404, who
    assert world.count(
        "SELECT COUNT(*) AS c FROM notebook_grants WHERE notebook_id=? AND principal_type='user'",
        (world.notebook,),
    ) == 1
    listed = client.get(world.url(), headers=world.headers["owner"]).json()
    assert listed["view"] == "owner"
    assert [(item["username"], item["role"]) for item in listed["items"]] == [
        (world.usernames["editor"], "editor"),
    ]
    assert listed["items"][0]["granted_by_username"] == world.usernames["owner"]
    # validation answers: unknown user / the owner themself
    assert client.put(
        f"/api/notebooks/{world.notebook}/collaborators/user-missing",
        headers=world.headers["owner"],
    ).status_code == 400
    assert _put(world, "owner").status_code == 400


def d17_editor_sees_editor_names_only(world: World) -> None:
    client = world.client
    for name in ("editor", "second", "deputy"):
        assert _put(world, name).status_code == 200
    for who in ("editor", "deputy"):
        seen = client.get(world.url(), headers=world.headers[who]).json()
        assert seen["view"] == "editor" and seen["items"] == []
        assert {item["username"] for item in seen["editors"]} == {
            world.usernames[name] for name in ("editor", "second", "deputy")
        }
        assert set(seen["editors"][0]) == {"username", "display_name"}


def put_emits_bell_event_once(world: World, monkeypatch) -> None:
    from app.services.pending_bus import pending_bus

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(pending_bus, "emit", lambda user, event: events.append((user, event)))
    first = _put(world, "editor")
    second = _put(world, "editor")
    assert (first.json()["changed"], second.json()["changed"]) == (True, False)
    assert events == [(world.users["editor"], {
        "event": "notebook_collaborator_granted", "notebook_id": world.notebook,
    })]


def revoke_clears_overlay_only_when_access_lost(world: World, monkeypatch) -> None:
    sharing = _repository()._runtime.sharing
    cleared: list[str] = []
    original = sharing.forget_member_state

    def record(notebook_id, user_id):
        cleared.append(user_id)
        return original(notebook_id, user_id)

    monkeypatch.setattr(sharing, "forget_member_state", record)
    for name in ("editor", "member"):
        assert _put(world, name).status_code == 200
    owner = world.headers["owner"]
    assert world.client.delete(world.url("member"), headers=owner).status_code == 204
    assert cleared == [], "a link member still reads: their overlay stays"
    assert world.client.delete(world.url("editor"), headers=owner).status_code == 204
    assert cleared == [world.users["editor"]]
    assert world.client.delete(world.url("editor"), headers=owner).status_code == 404
    assert world.client.get(
        f"/api/notebooks/{world.notebook}", headers=world.headers["editor"]
    ).status_code == 404


def self_exit_collaboration_disclosure_and_ack(world: World, monkeypatch) -> None:
    client = world.client
    for name in ("editor", "second"):
        assert _put(world, name).status_code == 200
    _memory(world, "editor", 2)
    editor = world.headers["editor"]
    disclosure = client.get(world.url("editor", "/exit-disclosure"), headers=editor)
    assert disclosure.status_code == 200 and disclosure.json() == {"memory_count": 2, "suggestion_count": 0}
    # someone else's disclosure: 404; a reader without a named grant: 0, like
    # the membership disclosure for a non-member
    assert client.get(world.url("second", "/exit-disclosure"), headers=editor).status_code == 404
    member_disclosure = client.get(
        world.url("member", "/exit-disclosure"), headers=world.headers["member"]
    )
    assert member_disclosure.status_code == 200
    assert member_disclosure.json() == {"memory_count": 0, "suggestion_count": 0}
    # 409 exit_disclosure_required: nothing touched
    refused = client.delete(world.url("editor"), headers=editor)
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"code": "exit_disclosure_required", "memory_count": 2, "suggestion_count": 0}
    assert _memory_count(world, "editor") == 2
    # 503 exit_incomplete: the purge failed part-way, still a collaborator
    memory = _repository()._runtime.memory_service

    def fail(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(memory, "_purge_page", fail)
    failed = client.delete(world.url("editor") + "?acknowledged_memory_count=2", headers=editor)
    assert failed.status_code == 503 and failed.json()["detail"]["code"] == "exit_incomplete"
    monkeypatch.undo()
    # 409 exit_incomplete: Memory saved while the exit ran stays, so does the grant
    store = memory.store
    original_finish = store.finish_grant_exit
    monkeypatch.setattr(
        store, "finish_grant_exit", lambda nb, user, token, **kwargs: MemberExitCompletion(1, False)
    )
    partial = client.delete(world.url("editor") + "?acknowledged_memory_count=2", headers=editor)
    assert partial.status_code == 409, partial.text
    assert partial.json()["detail"]["code"] == "exit_incomplete"
    monkeypatch.setattr(store, "finish_grant_exit", original_finish)
    # 200: the acknowledged exit deletes the leaver's Memory and the grant
    _memory(world, "editor", 1)
    count = _memory_count(world, "editor")
    done = client.delete(
        world.url("editor") + f"?acknowledged_memory_count={count}", headers=editor
    )
    assert done.status_code == 200 and done.json() == {"deleted_memory_count": count, "deleted_suggestion_count": 0}
    assert _memory_count(world, "editor") == 0
    assert client.get(f"/api/notebooks/{world.notebook}", headers=editor).status_code == 404
    # 204: nothing to delete
    second = world.headers["second"]
    assert client.delete(world.url("second"), headers=second).status_code == 204
    # no named grant (any more): idempotent like DELETE .../membership -- 204,
    # also for the leaver who no longer reads the notebook at all, and the
    # acknowledged retry gets the same 409 a membership retry gets
    assert client.delete(world.url("second"), headers=second).status_code == 204
    assert client.delete(world.url("editor"), headers=editor).status_code == 204
    assert client.delete(
        f"/api/notebooks/{world.notebook}/membership", headers=editor
    ).status_code == 204
    retry = client.delete(world.url("editor") + "?acknowledged_memory_count=1", headers=editor)
    membership_retry = client.delete(
        f"/api/notebooks/{world.notebook}/membership?acknowledged_memory_count=1",
        headers=editor,
    )
    assert (retry.status_code, retry.json()) == (
        membership_retry.status_code, membership_retry.json()
    ) == (409, {"detail": {"code": "exit_disclosure_required", "memory_count": 0, "suggestion_count": 0}})
    # the same answers for a notebook id that does not exist: nothing disclosed
    assert client.delete(
        "/api/notebooks/nb-missing/collaborators/" + world.users["editor"], headers=editor
    ).status_code == 204


def collaboration_suggestions_disclosure_and_late_adoption(world: World, monkeypatch) -> None:
    """Both counts and actual deletions reach the collaborator HTTP contract."""
    client = world.client
    assert _put(world, "editor").status_code == 200
    _memory(world, "editor", 2)
    world.write("UPDATE memory_items SET suggested_signal='clarified' WHERE created_by=?", (world.users["editor"],))
    editor = world.headers["editor"]
    disclosure = client.get(world.url("editor", "/exit-disclosure"), headers=editor)
    assert disclosure.json() == {"memory_count": 0, "suggestion_count": 2}
    refused = client.delete(world.url("editor") + "?acknowledged_memory_count=1", headers=editor)
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"code": "exit_disclosure_required", "memory_count": 0, "suggestion_count": 2}
    assert _memory_count(world, "editor") == 2
    memory = _repository()._runtime.memory_service
    original = memory.store.grant_exit_snapshot

    def adopt_after_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("claim"):
            world.write(
                "UPDATE memory_items SET status='confirmed' WHERE id=(SELECT MIN(id) FROM memory_items WHERE created_by=?)",
                (world.users["editor"],),
            )
        return result

    monkeypatch.setattr(memory.store, "grant_exit_snapshot", adopt_after_claim)
    adopted = client.delete(world.url("editor") + "?acknowledged_memory_count=0", headers=editor)
    assert adopted.status_code == 409
    assert adopted.json()["detail"] == {"code": "exit_disclosure_required", "memory_count": 1, "suggestion_count": 1}
    assert _memory_count(world, "editor") == 2
    assert _repository()._runtime.groups.user_grant_role(world.notebook, world.users["editor"]) == "editor"
    monkeypatch.setattr(memory.store, "grant_exit_snapshot", original)
    done = client.delete(world.url("editor") + "?acknowledged_memory_count=1", headers=editor)
    assert done.status_code == 200
    assert done.json() == {"deleted_memory_count": 1, "deleted_suggestion_count": 1}
    assert _memory_count(world, "editor") == 0
    assert _put(world, "second").status_code == 200
    _memory(world, "second", 1)
    world.write("UPDATE memory_items SET suggested_signal='repeated' WHERE created_by=?", (world.users["second"],))
    only = client.delete(world.url("second"), headers=world.headers["second"])
    assert only.status_code == 200
    assert only.json() == {"deleted_memory_count": 0, "deleted_suggestion_count": 1}


def cross_state_two_exits_independent(world: World) -> None:
    """A link member who is also an editor: leaving the collaboration keeps
    reading (as a member) and keeps every Memory; leaving the share afterwards
    is the member exit, which now deletes it."""
    client = world.client
    assert _put(world, "member").status_code == 200
    _memory(world, "member", 2)
    member = world.headers["member"]
    assert client.get(
        world.url("member", "/exit-disclosure"), headers=member
    ).json() == {"memory_count": 0, "suggestion_count": 0}
    assert client.delete(world.url("member"), headers=member).status_code == 204
    assert _memory_count(world, "member") == 2
    assert client.get(f"/api/notebooks/{world.notebook}", headers=member).status_code == 200
    assert client.get(
        f"/api/notebooks/{world.notebook}/membership/exit-disclosure", headers=member
    ).json() == {"memory_count": 2, "suggestion_count": 0}
    left = client.delete(
        f"/api/notebooks/{world.notebook}/membership?acknowledged_memory_count=2",
        headers=member,
    )
    assert left.status_code == 200 and _memory_count(world, "member") == 0


def viewer_named_edge_exits_via_collaborators_me(world: World) -> None:
    world.write(
        "INSERT INTO notebook_grants "
        "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("gl-viewer", world.notebook, "user", world.users["carol"], "viewer",
         world.users["owner"], _STAMP),
    )
    carol = world.headers["carol"]
    assert world.client.get(world.url(), headers=carol).status_code == 404  # not an editor
    assert world.client.delete(world.url("carol"), headers=carol).status_code == 204
    assert world.count(
        "SELECT COUNT(*) AS c FROM notebook_grants WHERE id='gl-viewer'"
    ) == 0


def grants_and_collaborators_cover_every_grant_row_exactly_once(world: World) -> None:
    client, owner = world.client, world.headers["owner"]
    assert client.post(
        f"/api/notebooks/{world.notebook}/grants",
        json={"principal_type": "group", "principal_id": world.group, "role": "viewer"},
        headers=owner,
    ).status_code == 200
    assert _put(world, "editor").status_code == 200
    for grant_id, principal_type, user, role in (
        ("gc-everyone", "everyone", None, "viewer"),
        ("gc-uviewer", "user", "carol", "viewer"),
        ("gc-uadmin", "user", "second", "admin"),
    ):
        world.write(
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (grant_id, world.notebook, principal_type,
             world.users[user] if user else "", role, world.users["owner"], _STAMP),
        )
    with _repository()._runtime.database.connect() as db:
        every = {
            row["id"] for row in db.execute(
                ("SELECT id FROM notebook_grants WHERE notebook_id=%s" if world.postgres
                 else "SELECT id FROM notebook_grants WHERE notebook_id=?"),
                (world.notebook,),
            ).fetchall()
        }
    grants = client.get(f"/api/notebooks/{world.notebook}/grants", headers=owner).json()
    named = client.get(world.url(), headers=owner).json()["items"]
    grant_ids = {item["id"] for item in grants}
    named_ids = {item["id"] for item in named}
    assert len(every) == 6
    assert not grant_ids & named_ids
    assert grant_ids | named_ids == every
    for item in grants:
        assert client.delete(
            f"/api/notebooks/{world.notebook}/grants/{item['id']}", headers=owner
        ).status_code == 204, item
    for item in named:
        assert client.delete(
            f"/api/notebooks/{world.notebook}/collaborators/{item['user_id']}", headers=owner
        ).status_code == 204, item
    assert world.count(
        "SELECT COUNT(*) AS c FROM notebook_grants WHERE notebook_id=?", (world.notebook,)
    ) == 0


def group_admin_cannot_delete_editor_edge(world: World) -> None:
    client, deputy = world.client, world.headers["deputy"]
    granted = _put(world, "editor").json()["grant"]
    assert client.delete(
        f"/api/notebooks/{world.notebook}/grants/{granted['id']}", headers=deputy
    ).status_code == 404
    assert client.delete(world.url("editor"), headers=deputy).status_code == 404
    assert client.get(world.url(), headers=world.headers["owner"]).json()["items"][0][
        "id"
    ] == granted["id"]


def tier_to_base_with_editors_409(world: World) -> None:
    world.write("UPDATE users SET role='admin' WHERE id=?", (world.users["owner"],))
    assert _put(world, "editor").status_code == 200
    refused = world.client.post(
        f"/api/notebooks/{world.notebook}/tier", json={"tier": "base"},
        headers=world.headers["owner"],
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == "请先移除这本笔记本的编辑者"
    assert world.client.delete(
        world.url("editor"), headers=world.headers["owner"]
    ).status_code == 204
    assert world.client.post(
        f"/api/notebooks/{world.notebook}/tier", json={"tier": "base"},
        headers=world.headers["owner"],
    ).status_code == 200


def granting_first_editor_closes_borrowed_mounts_and_revoke_restores(world: World) -> None:
    """Decision D13: the first named editor shares the notebook, so a library
    the owner only borrows (shared with them by someone else) stops counting --
    exactly the edge ``closes_when_shared`` listed beforehand; removing the
    editor (nothing else shares it) brings it back."""
    client, owner = world.client, world.headers["owner"]
    private = client.post("/api/notebooks", json={"name": "私有"}, headers=owner).json()["id"]
    borrowed = client.post(
        "/api/notebooks", json={"name": "借来的"}, headers=world.headers["carol"]
    ).json()["id"]
    _repository()._runtime.sharing.add_member(borrowed, world.users["owner"])
    assert client.put(
        f"/api/notebooks/{private}/bases", json={"base_notebook_ids": [borrowed]},
        headers=owner,
    ).status_code == 200

    def bases():
        return [
            (item["id"], item["active"], item["closes_when_shared"])
            for item in client.get(f"/api/notebooks/{private}/bases", headers=owner).json()
        ]

    assert bases() == [(borrowed, True, True)]
    assert client.put(
        f"/api/notebooks/{private}/collaborators/{world.users['editor']}", headers=owner
    ).status_code == 200
    assert bases() == [(borrowed, False, False)]
    assert client.delete(
        f"/api/notebooks/{private}/collaborators/{world.users['editor']}", headers=owner
    ).status_code == 204
    assert bases() == [(borrowed, True, True)]


CASES: dict[str, Callable[..., Any]] = {
    "collaborator_endpoints_owner_only_404_for_others": (
        collaborator_endpoints_owner_only_404_for_others
    ),
    "d17_editor_sees_editor_names_only": d17_editor_sees_editor_names_only,
    "cross_state_two_exits_independent": cross_state_two_exits_independent,
    "viewer_named_edge_exits_via_collaborators_me": viewer_named_edge_exits_via_collaborators_me,
    "grants_and_collaborators_cover_every_grant_row_exactly_once": (
        grants_and_collaborators_cover_every_grant_row_exactly_once
    ),
    "group_admin_cannot_delete_editor_edge": group_admin_cannot_delete_editor_edge,
    "tier_to_base_with_editors_409": tier_to_base_with_editors_409,
    "granting_first_editor_closes_borrowed_mounts_and_revoke_restores": (
        granting_first_editor_closes_borrowed_mounts_and_revoke_restores
    ),
}
MONKEYPATCH_CASES: dict[str, Callable[..., Any]] = {
    "collaboration_suggestions_disclosure_and_late_adoption": collaboration_suggestions_disclosure_and_late_adoption,
    "put_emits_bell_event_once": put_emits_bell_event_once,
    "revoke_clears_overlay_only_when_access_lost": revoke_clears_overlay_only_when_access_lost,
    "self_exit_collaboration_disclosure_and_ack": self_exit_collaboration_disclosure_and_ack,
}
