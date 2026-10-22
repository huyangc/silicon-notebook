"""The one-day deletion window on SQLite.

The shared scenarios live in ``tests/notebook_delete_grace_cases.py`` and run
unchanged on PostgreSQL through
``tests/postgres/test_notebook_delete_grace_pg.py``. This file adds the HTTP
contract (DELETE / cancel-delete / GET's explanation code / the notices / the
two Memory exits / the bell / group approval), the notification pushes and
SQLite's own race between two sweepers.
"""
from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.notebook_delete_grace import (
    DELETE_WINDOW_CLOSED_CODE,
    DELETE_WINDOW_TRANSFER_PAGE_SIZE,
    NOT_IN_DELETE_WINDOW_CODE,
    NOTEBOOK_DELETE_DUE_CODE,
    NOTEBOOK_PENDING_DELETE_CODE,
    TRANSFER_IN_PROGRESS_CODE,
)
from app.services import notebook_delete as notebook_delete_module
from app.services.sqlite_repository import SQLiteRepository
from tests.notebook_delete_grace_cases import (
    CASES,
    DEADLINE,
    EMBED_DIM,
    T0,
    build_world,
    iso,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'grace.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    repository = SQLiteRepository(Settings(_env_file=None))
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_notebook_delete_grace(repo, monkeypatch, case):
    CASES[case](build_world(repo, postgres=False, monkeypatch=monkeypatch))


def test_more_than_one_real_page_moves_everything(repo, monkeypatch):
    """The real page size: 203 transferable items take two pages (200 + 3)
    and every one lands in the target exactly once."""
    from tests.notebook_delete_grace_cases import _confirmed_memory, _titles_in, transfer_page

    world = build_world(repo, postgres=False, monkeypatch=monkeypatch)
    extra = [_confirmed_memory(world, world.member, f"p{i:03d}") for i in range(201)]
    world.request()
    first = transfer_page(world, world.member, world.member_target)
    assert (len(first["results"]), first["remaining"]) == (DELETE_WINDOW_TRANSFER_PAGE_SIZE, 3)
    second = transfer_page(world, world.member, world.member_target, first["next_after"])
    assert (len(second["results"]), second["remaining"]) == (3, 0)
    moved = [row for page in (first, second) for row in page["results"]]
    assert all(row["ok"] and row["status"] == "moved" for row in moved)
    assert sorted(row["source_id"] for row in moved) == sorted([*world.confirmed, *extra])
    assert len(_titles_in(world, world.member_target)) == 203


# --------------------------------------------------------------- pushes


def test_every_transition_pushes_the_owner_and_the_memory_holders(repo, monkeypatch):
    """T1, T2 and T3 each refresh the bell of the owner and of every user
    with Memory there; a failing audience read is swallowed (the transition
    has committed)."""
    world = build_world(repo, postgres=False, monkeypatch=monkeypatch)
    pushed: list[str] = []
    monkeypatch.setattr(notebook_delete_module, "publish_snapshot", pushed.append)
    runner = repo._runtime.notebook_delete
    runner._submit = lambda job_id, notebook_id: True
    runner._notify_async = lambda fn: fn()
    audience = sorted([world.owner.id, world.member.id])

    runner.request_pending(world.doomed, world.owner.id)
    assert sorted(pushed) == audience
    pushed.clear()
    assert runner.cancel_pending(world.doomed, world.owner.id) == "cancelled"
    assert sorted(pushed) == audience
    pushed.clear()
    assert runner.cancel_pending(world.doomed, world.owner.id) == "already_live"
    assert pushed == []  # nothing changed, nothing pushed
    runner.request_pending(world.doomed, world.owner.id)
    pushed.clear()
    assert runner.sweep_once(now=DEADLINE) == 1
    assert sorted(pushed) == audience

    def broken(notebook_id):
        raise RuntimeError("audience read failed")

    pushed.clear()
    monkeypatch.setattr(repo._runtime.notebook_delete_jobs, "delete_notice_audience", broken)
    runner.request_pending(world.owner_target, world.owner.id)  # does not raise
    assert pushed == []


def test_request_and_cancel_do_not_wait_for_the_bell(repo, monkeypatch):
    """T1 and T2 answer before their bell refresh runs: the push recomputes
    each subscribed user's snapshot, which must not hold the request."""
    world = build_world(repo, postgres=False, monkeypatch=monkeypatch)
    release, pushed = threading.Event(), []

    def slow_push(user_id):
        assert release.wait(10)
        pushed.append(user_id)

    monkeypatch.setattr(notebook_delete_module, "publish_snapshot", slow_push)
    runner = repo._runtime.notebook_delete
    started = time.monotonic()
    assert runner.request_pending(world.doomed, world.owner.id)["status"] == "pending_delete"
    assert runner.cancel_pending(world.doomed, world.owner.id) == "cancelled"
    assert time.monotonic() - started < 5
    assert pushed == []
    release.set()
    deadline = time.monotonic() + 10
    while len(pushed) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sorted(pushed) == sorted([world.owner.id, world.member.id] * 2)


def test_a_failing_notice_read_drops_only_those_bell_items(repo, monkeypatch, caplog):
    """The bell keeps its other items when the deletion notices cannot be
    read, and the failure is logged."""
    world = build_world(repo, postgres=False, monkeypatch=monkeypatch)
    world.request()
    pending = repo._runtime.pending_actions_service
    real_rows = pending.projections.pending_actions_projection_rows
    other = {"type": "share_request", "request_id": "sr-1", "group_id": "g-1"}

    def rows_with_another_item(user_id):
        rows = real_rows(user_id)
        return {**rows, "items": [*rows["items"], other]}

    monkeypatch.setattr(
        pending.projections, "pending_actions_projection_rows", rows_with_another_item
    )
    before = pending.list_for_user(world.owner.id)
    assert [item["type"] for item in before["items"]][:1] == ["notebook_pending_delete"]
    projection_items = [i for i in before["items"] if i["type"] != "notebook_pending_delete"]

    def broken(user_id, **kwargs):
        raise RuntimeError("notice read failed")

    monkeypatch.setattr(pending.projections, "pending_delete_notices", broken)
    with caplog.at_level("WARNING", logger="silicon_notebook.pending_actions"):
        after = pending.list_for_user(world.owner.id)
    assert after["items"] == projection_items and other in after["items"]
    assert "deletion-window notices failed" in caplog.text
    with pytest.raises(RuntimeError):
        pending.deletion_notices(world.owner.id)  # the block's own endpoint fails closed


def test_two_sweepers_promote_a_window_once(repo, monkeypatch):
    """Two sweepers (two processes' drivers C) race the same due window:
    exactly one promotion and one job row."""
    world = build_world(repo, postgres=False, monkeypatch=monkeypatch)
    world.request()
    store = repo._runtime.notebook_delete_jobs
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def sweep():
        barrier.wait(5)
        outcomes.append(store.promote_due_pending(world.doomed, now=DEADLINE))

    threads = [threading.Thread(target=sweep) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(outcome is None for outcome in outcomes) == [False, True]
    assert [row["status"] for row in world.jobs()] == ["queued"]


# ------------------------------------------------------------------ HTTP


@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'grace-http.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    from app.api.deps import repository
    from app.main import create_app

    client = TestClient(create_app())

    def register(username: str) -> tuple[dict, str]:
        body = client.post(
            "/api/auth/register", json={"username": username, "password": "pw"}
        ).json()
        return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]

    owner, owner_id = register("o00410001")
    member, member_id = register("m00410002")
    reader, reader_id = register("r00410003")
    stranger, _ = register("s00410004")
    repo = repository()
    world_clock = [T0]
    from tests.notebook_delete_grace_cases import _pin_clock

    _pin_clock(repo, monkeypatch, world_clock)
    doomed = client.post("/api/notebooks", json={"name": "将被删除"}, headers=owner).json()["id"]
    mine = client.post("/api/notebooks", json={"name": "我的"}, headers=member).json()["id"]
    for user_id in (member_id, reader_id):
        repo._runtime.sharing.add_member(doomed, user_id)
    service = repo._runtime.memory_service
    service.kg_ingest_scheduler = lambda fn, item: None
    service.embedding_scheduler = lambda fn, job: None
    memory_ids = []
    for index in range(2):
        item = service.create_candidate(
            doomed, member_id, None, f"req-http-{index}", f"T{index}", f"B{index}",
            [], "reason", {}, [],
        )
        if index == 0:
            service.confirm(item.id, member_id, {"extract_kg": False})
        memory_ids.append(item.id)
    return {
        "client": client, "repo": repo, "clock": world_clock, "owner": owner,
        "owner_id": owner_id, "member": member, "member_id": member_id,
        "reader": reader, "stranger": stranger, "doomed": doomed, "mine": mine,
        "memory_ids": memory_ids,
    }


def _delete(http):
    return http["client"].delete(f"/api/notebooks/{http['doomed']}", headers=http["owner"])


def test_delete_answers_the_window_and_hides_the_notebook(http):
    client, doomed = http["client"], http["doomed"]
    response = _delete(http)
    assert response.status_code == 202
    assert response.json() == {"status": "pending_delete", "delete_after": iso(DEADLINE)}
    listed = client.get("/api/notebooks", headers=http["owner"]).json()
    assert doomed not in {row["id"] for row in listed}
    # Readers get the explanation code; a stranger the plain 404.
    for who, is_owner, count in (("owner", True, 0), ("member", False, 2), ("reader", False, 0)):
        got = client.get(f"/api/notebooks/{doomed}", headers=http[who])
        assert got.status_code == 404
        assert got.json()["detail"] == {
            "code": NOTEBOOK_PENDING_DELETE_CODE, "notebook_id": doomed, "name": "将被删除",
            "delete_after": iso(DEADLINE), "is_owner": is_owner, "memory_count": count,
            "transferable_count": 1 if who == "member" else 0,
        }
    plain = client.get(f"/api/notebooks/{doomed}", headers=http["stranger"])
    assert plain.status_code == 404 and plain.json()["detail"] == "Notebook not found"
    # Sub-resources and writes: plain 404 for everyone, the owner included.
    assert client.get(f"/api/notebooks/{doomed}/memories", headers=http["member"]).status_code == 404
    assert client.patch(
        f"/api/notebooks/{doomed}", json={"name": "x"}, headers=http["owner"]
    ).status_code == 404


def test_a_second_delete_is_a_409_with_the_deadline_and_a_member_gets_404(http):
    _delete(http)
    again = _delete(http)
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["code"] == NOTEBOOK_PENDING_DELETE_CODE
    assert detail["delete_after"] == iso(DEADLINE)
    assert http["client"].delete(
        f"/api/notebooks/{http['doomed']}", headers=http["member"]
    ).status_code == 404


def test_a_mirror_cannot_enter_the_window(http):
    http["repo"]._runtime.sharing_store.set_notebook_sync_origin(http["doomed"], "env-src")
    response = _delete(http)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "notebook_mirrored"
    assert http["client"].post(
        f"/api/notebooks/{http['doomed']}/cancel-delete", headers=http["owner"]
    ).status_code == 409


def test_cancel_is_the_owners_and_restores_the_notebook(http):
    client, doomed = http["client"], http["doomed"]
    _delete(http)
    url = f"/api/notebooks/{doomed}/cancel-delete"
    assert client.post(url, headers=http["member"]).status_code == 404
    assert client.post(url, headers=http["stranger"]).status_code == 404
    response = client.post(url, headers=http["owner"])
    assert response.status_code == 200 and response.json()["id"] == doomed
    assert client.get(f"/api/notebooks/{doomed}", headers=http["member"]).status_code == 200
    again = client.post(url, headers=http["owner"])
    assert again.status_code == 200 and again.json()["id"] == doomed
    _delete(http)
    http["clock"][0] = DEADLINE
    late = client.post(url, headers=http["owner"])
    assert late.status_code == 409
    assert late.json()["detail"]["code"] == NOTEBOOK_DELETE_DUE_CODE
    assert client.post("/api/notebooks/nb-missing/cancel-delete", headers=http["owner"]).status_code == 404


def test_notices_endpoint_and_bell(http):
    client = http["client"]
    assert client.get("/api/me/notebook-deletions", headers=http["member"]).json() == {"items": []}
    _delete(http)
    member_items = client.get("/api/me/notebook-deletions", headers=http["member"]).json()["items"]
    assert [item["notebook_id"] for item in member_items] == [http["doomed"]]
    assert member_items[0]["memory_count"] == 2
    # B3: a reader with no Memory is not notified.
    assert client.get("/api/me/notebook-deletions", headers=http["reader"]).json() == {"items": []}
    bell = client.get("/api/me/pending-actions", headers=http["owner"]).json()
    assert [item["type"] for item in bell["items"]][:1] == ["notebook_pending_delete"]
    assert bell["count"] >= 1


def test_export_inside_and_after_the_window(http):
    client, doomed = http["client"], http["doomed"]
    _delete(http)
    url = f"/api/notebooks/{doomed}/memories/export"
    response = client.get(url, headers=http["member"])
    assert response.status_code == 200, response.text
    assert "这本笔记本已被所有者删除" in response.text
    assert "将被删除" in response.headers["content-disposition"] or response.headers[
        "content-disposition"
    ]
    assert client.get(url, headers=http["stranger"]).status_code == 404
    nothing = client.get(url, headers=http["reader"])
    assert nothing.status_code == 404 and nothing.json()["detail"]["code"] == "nothing_to_export"
    http["clock"][0] = DEADLINE
    closed = client.get(url, headers=http["member"])
    assert closed.status_code == 404
    assert closed.json()["detail"] == {"code": DELETE_WINDOW_CLOSED_CODE}


def test_transfer_route_moves_and_reports_per_item(http):
    client, doomed = http["client"], http["doomed"]
    url = f"/api/notebooks/{doomed}/memories/transfer"
    _delete(http)
    assert client.post(
        url, json={"target_notebook_id": http["mine"], "mode": "copy"}, headers=http["member"]
    ).status_code == 422  # the mode is not the caller's choice
    response = client.post(url, json={"target_notebook_id": http["mine"]}, headers=http["member"])
    assert response.status_code == 200, response.text
    page = response.json()
    [result] = page["results"]
    assert result["source_id"] == http["memory_ids"][0]
    assert result["status"] == "moved" and result["ok"] is True
    assert (page["remaining"], page["next_after"]) == (0, http["memory_ids"][0])
    # Only inside a window: a live notebook keeps the Memory page's transfer.
    live = client.post(
        f"/api/notebooks/{http['mine']}/memories/transfer",
        json={"target_notebook_id": http["mine"]}, headers=http["member"],
    )
    assert live.status_code == 409
    assert live.json()["detail"]["code"] == NOT_IN_DELETE_WINDOW_CODE
    # One transfer at a time per caller and notebook.
    claim = http["repo"]._runtime.memory_store.delete_window_transfer_claim
    with claim(doomed, http["member_id"]) as held:
        assert held
        busy = client.post(url, json={"target_notebook_id": http["mine"]}, headers=http["member"])
    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == TRANSFER_IN_PROGRESS_CODE
    assert client.post(
        url, json={"target_notebook_id": doomed}, headers=http["member"]
    ).status_code == 404  # not a notebook the member can write
    assert client.post(
        url, json={"target_notebook_id": http["mine"]}, headers=http["stranger"]
    ).status_code == 404
    http["clock"][0] = DEADLINE + timedelta(seconds=1)
    closed = client.post(url, json={"target_notebook_id": http["mine"]}, headers=http["member"])
    assert closed.status_code == 404
    assert closed.json()["detail"] == {"code": DELETE_WINDOW_CLOSED_CODE}


def test_group_approval_refuses_a_notebook_inside_its_window(http, monkeypatch):
    """The share-request approval on a notebook inside its window answers the
    window's own 409 copy (the other states keep theirs)."""
    from app.api import group_routes
    from app.repositories.ports import ShareRequestNotebookNotLiveError

    class Groups:
        def approve_share_request(self, *args, **kwargs):
            raise ShareRequestNotebookNotLiveError(http["doomed"], "pending_delete")

    monkeypatch.setattr(group_routes, "group_repository", lambda: Groups())
    monkeypatch.setattr(group_routes, "_require_group_admin", lambda group_id, user: None)
    response = http["client"].post(
        "/api/groups/grp-x/share-requests/sr-x/approve", headers=http["owner"]
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "这本笔记本已安排删除，不能再共享。"
