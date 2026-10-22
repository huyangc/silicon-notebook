"""The one-day deletion window (``app.domain.notebook_delete_grace``), shared by
both backends.

``test_notebook_delete_grace.py`` runs every case on SQLite and
``postgres/test_notebook_delete_grace_pg.py`` runs the same cases on
PostgreSQL (plus the two-connection races only PostgreSQL can stage).

The world: ``owner`` owns ``doomed`` (the notebook that gets deleted) and
``owner_target``; ``member`` reads ``doomed`` through a membership row and owns
``member_target``; ``bystander`` is a member of ``doomed`` holding no Memory;
``outsider`` reads nothing. ``member`` holds four Memory items in ``doomed``
-- two confirmed, one candidate, one confirmed but proposed for promotion --
and ``owner`` one confirmed item.

Every step goes through the production component that runs it
(``repo._runtime.<component>.<method>``) with an explicit instant: the
window's clock is injected, never read off the wall (a literal date compared
with the real clock is a time bomb). The repository clock seam the services
read is pinned to ``world.clock`` for the same reason.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from app.domain.mount_edges import (
    MASKED_MOUNT_NAME,
    MOUNT_BASE_DELETED_REASON,
    MOUNT_BASE_UNAVAILABLE_REASON,
    MOUNT_OWN_PENDING_DELETE_REASON,
)
from app.domain.notebook_delete_grace import (
    DELETE_WINDOW_CLOSED_CODE,
    MEMORY_NOT_READABLE_CODE,
    NOTEBOOK_DELETE_GRACE_SECONDS,
)
from app.models.schemas import NotebookCreate
from app.repositories.ports import (
    DeleteWindowClosedError,
    DeleteWindowTransferInProgressError,
    NotebookAlreadyDeletingError,
    NotebookDeleteDueError,
    NotebookPendingDeleteError,
)
from app.services.embedding import FakeEmbedder
from tests.memory_purge_cases import Sql, _as_user
from tests.model_testkit import bind_all_embedding_clients

EMBED_DIM = 16
# Any instant works: every clock these cases reach is injected. Driver A's
# stale-row cutoff (list_stale, mark_running) used to read the host clock while
# updated_at is written by the store clock, so a T0 of 2026-10-22 08:00 turned
# case_one_failing_promotion red the moment real time passed it; the cutoff
# now reads the store clock (test_notebook_delete_jobization pins it), and the
# cases pass with T0 anywhere from 2025 to 2099.
T0 = datetime(2099, 1, 1, 8, 0, tzinfo=timezone.utc)
GRACE = timedelta(seconds=NOTEBOOK_DELETE_GRACE_SECONDS)
DEADLINE = T0 + GRACE


def iso(instant: datetime) -> str:
    return instant.isoformat()


@dataclass
class World:
    repo: Any
    sql: Sql
    postgres: bool
    owner: Any
    member: Any
    bystander: Any
    outsider: Any
    doomed: str
    owner_target: str
    member_target: str
    # member's Memory in doomed
    confirmed: tuple[str, str]
    candidate: str
    proposed: str
    owner_memory: str
    clock: list[datetime] = field(default_factory=lambda: [T0])

    @property
    def runtime(self) -> Any:
        return self.repo._runtime

    def status(self, notebook_id: str | None = None) -> dict | None:
        rows = self.sql.rows(
            "SELECT status, status_before_delete, delete_after, delete_requested_at, "
            "delete_requested_by FROM notebooks WHERE id=?",
            (notebook_id or self.doomed,),
        )
        return rows[0] if rows else None

    def jobs(self, notebook_id: str | None = None) -> list[dict]:
        return self.sql.rows(
            "SELECT status, phase FROM notebook_delete_jobs WHERE notebook_id=?",
            (notebook_id or self.doomed,),
        )

    def memory_notebook(self, memory_id: str) -> str | None:
        rows = self.sql.rows(
            "SELECT notebook_id FROM memory_items WHERE id=?", (memory_id,)
        )
        return rows[0]["notebook_id"] if rows else None

    def request(self, at: datetime = T0) -> dict:
        return self.runtime.notebook_delete_jobs.request_pending(
            self.doomed, self.owner.id, now=at
        )


def _pin_clock(repo: Any, monkeypatch: Any, world_clock: list[datetime]) -> None:
    """Point the repository clock seam (``RepositoryFacade.__init__`` resolves
    ``_now`` on the concrete module first) at ``world_clock[0]``."""
    import sys

    from app.services import repository_facade

    modules = [repository_facade]
    concrete = sys.modules.get(type(repo).__module__)
    if concrete is not None and concrete is not repository_facade:
        modules.append(concrete)
    for module in modules:
        monkeypatch.setattr(
            module, "_now", lambda: iso(world_clock[0]), raising=False
        )


def build_world(repo: Any, *, postgres: bool, monkeypatch: Any) -> World:
    bind_all_embedding_clients(repo, FakeEmbedder(dim=EMBED_DIM))
    service = repo._runtime.memory_service
    service.kg_ingest_scheduler = lambda fn, item: None
    service.embedding_scheduler = lambda fn, job: fn(job)
    clock = [T0]
    _pin_clock(repo, monkeypatch, clock)
    identity = repo._runtime.identity
    owner = identity.create_user("o00400001", "pw123456")
    member = identity.create_user("m00400002", "pw123456")
    bystander = identity.create_user("b00400003", "pw123456")
    outsider = identity.create_user("x00400004", "pw123456")

    def notebook(by: Any, name: str) -> str:
        return _as_user(by, lambda: repo.create_notebook(NotebookCreate(name=name))).id

    doomed = notebook(owner, "将被删除")
    owner_target = notebook(owner, "主人的另一本")
    member_target = notebook(member, "成员自己的")
    repo._runtime.sharing.add_member(doomed, member.id)
    repo._runtime.sharing.add_member(doomed, bystander.id)

    def memory(user: Any, tag: str, *, confirm: bool) -> str:
        # One second apart, so "oldest first" is the creation order here.
        clock[0] += timedelta(seconds=1)
        item = service.create_candidate(
            doomed, user.id, None, f"req-{tag}", f"Title {tag}", f"Body {tag}.",
            [], "reason", {}, [],
        )
        if confirm:
            service.confirm(item.id, user.id, {"extract_kg": False})
        return item.id

    clock[0] = T0 - timedelta(minutes=1)
    confirmed = (memory(member, "c1", confirm=True), memory(member, "c2", confirm=True))
    candidate = memory(member, "cand", confirm=False)
    proposed = memory(member, "prop", confirm=True)
    owner_memory = memory(owner, "own", confirm=True)
    clock[0] = T0
    sql = Sql(repo, postgres=postgres)
    # A promotion proposal in flight: the move rule refuses this item.
    sql.write(
        "UPDATE memory_items SET promotion_state='proposed' WHERE id=?", (proposed,)
    )
    return World(
        repo, sql, postgres, owner, member, bystander, outsider, doomed,
        owner_target, member_target, confirmed, candidate, proposed, owner_memory,
        clock,
    )


# ------------------------------------------------------------ T1 (request)


def case_request_opens_the_window_and_hides_the_notebook(world: World) -> None:
    window = world.request()
    assert window["notebook_id"] == world.doomed
    assert window["delete_after"] == iso(DEADLINE)
    row = world.status()
    assert row["status"] == "pending_delete"
    assert row["status_before_delete"] == "draft"
    assert row["delete_requested_by"] == world.owner.id
    assert world.jobs() == []  # no delete job until the window closes
    sharing = world.runtime.sharing
    for user in (world.owner, world.member, world.bystander):
        assert sharing.user_can_read_notebook(world.doomed, user.id) is False
    assert sharing.user_can_access_notebook(world.doomed, world.owner.id) is False
    owned = {
        row["id"] for row in world.runtime.queries.list_user_notebooks(world.owner.id)
    }
    assert world.doomed not in owned and world.owner_target in owned


def case_a_repeated_request_reports_the_open_window(world: World) -> None:
    world.request()
    with pytest.raises(NotebookPendingDeleteError) as caught:
        world.request(at=T0 + timedelta(hours=3))
    assert caught.value.delete_after == iso(DEADLINE)
    assert world.status()["delete_after"] is not None
    stores = world.runtime.notebook_delete_jobs
    with pytest.raises(KeyError):
        stores.request_pending("nb-missing", world.owner.id, now=T0)
    world.sql.write("UPDATE notebooks SET status='deleting' WHERE id=?", (world.owner_target,))
    with pytest.raises(NotebookAlreadyDeletingError) as other:
        stores.request_pending(world.owner_target, world.owner.id, now=T0)
    assert not isinstance(other.value, NotebookPendingDeleteError)


# ------------------------------------------------------------- T2 (cancel)


def _share_and_mount(world: World) -> Callable[[], tuple[Any, set]]:
    """Give ``doomed`` a share link and mount it into ``owner_target``; return
    a probe of both (the token's notebook, the mount's participants)."""
    world.sql.write(
        "UPDATE notebooks SET is_shared=1,share_token=? WHERE id=?",
        ("tok-grace", world.doomed),
    )
    world.sql.write(
        "INSERT INTO notebook_bases (notebook_id,base_notebook_id,created_at,created_by) "
        "VALUES (?,?,?,?)",
        (world.owner_target, world.doomed, iso(T0), world.owner.id),
    )

    def probe() -> tuple[Any, set]:
        token = world.runtime.sharing_store.find_by_token("tok-grace")
        with world.runtime.database.connect() as db:
            participants = set(world.runtime.notebook_store.participant_ids(
                db, world.owner_target, viewer_id=world.owner.id,
            ))
        return token, participants

    return probe


def case_cancel_restores_everything(world: World) -> None:
    world.sql.write("UPDATE notebooks SET status='ready' WHERE id=?", (world.doomed,))
    probe = _share_and_mount(world)
    assert probe() == (world.doomed, {world.owner_target, world.doomed})
    world.request()
    # The share link resolves to nothing and the mount loses its base.
    assert probe() == (None, {world.owner_target})
    store = world.runtime.notebook_delete_jobs
    assert store.cancel_pending(world.doomed, now=DEADLINE - timedelta(seconds=1)) == "cancelled"
    row = world.status()
    assert row == {
        "status": "ready", "status_before_delete": "", "delete_after": None,
        "delete_requested_at": None, "delete_requested_by": None,
    }
    sharing = world.runtime.sharing
    for user in (world.owner, world.member, world.bystander):
        assert sharing.user_can_read_notebook(world.doomed, user.id) is True
    assert probe() == (world.doomed, {world.owner_target, world.doomed})
    # Idempotent: a second cancel reports the live notebook.
    assert store.cancel_pending(world.doomed, now=T0) == "already_live"


def _mount(world: World, notebook_id: str, base_id: str, by: Any) -> Callable[..., list]:
    world.sql.write(
        "INSERT INTO notebook_bases (notebook_id,base_notebook_id,created_at,created_by) "
        "VALUES (?,?,?,?)",
        (notebook_id, base_id, iso(T0), by.id),
    )
    store = world.runtime.notebook_store
    return lambda viewer: [
        (edge["name"], edge["active"], edge["inactive_reason"])
        for edge in store.list_mount_edges_for_notebook(notebook_id, viewer.id)
    ]


def _group_admin_of(world: World, notebook_id: str, admin: Any) -> None:
    """``admin`` becomes a group admin holding an admin grant on ``notebook_id``."""
    groups = world.runtime.groups
    group = groups.create_group(
        name="管理组", kind="project", description="", created_by=world.owner.id
    )["id"]
    groups.upsert_member(group, admin.id, role="admin", added_by=world.owner.id)
    groups.create_grant(
        notebook_id, principal_type="group", principal_id=group, role="admin",
        created_by=world.owner.id, admin_user_id=world.owner.id,
    )


def case_my_mount_of_my_deleted_notebook_says_cancel_brings_it_back(world: World) -> None:
    """The mounted notebook's own owner looks at a mount of it in its window:
    the edge says so and names the way back, under the real name. A group
    admin of the mounting notebook looking at the same edge reads the neutral
    copy -- they did not delete it and cannot cancel it."""
    _group_admin_of(world, world.owner_target, world.bystander)
    edges = _mount(world, world.owner_target, world.doomed, world.owner)
    assert edges(world.owner) == [("将被删除", True, "")]
    world.request()
    assert edges(world.owner) == [("将被删除", False, MOUNT_OWN_PENDING_DELETE_REASON)]
    assert edges(world.bystander) == [("将被删除", False, MOUNT_BASE_UNAVAILABLE_REASON)]
    world.runtime.notebook_delete_jobs.cancel_pending(world.doomed, now=T0)
    assert edges(world.owner) == [("将被删除", True, "")]


def case_a_public_library_deleted_by_its_owner_is_only_unavailable(world: World) -> None:
    """Someone else's public library in its window: the mounter is told it is
    unavailable -- not that it was demoted or is not theirs -- and its name
    stays masked; a cancel brings the edge back whole."""
    world.sql.write("UPDATE notebooks SET tier='base' WHERE id=?", (world.doomed,))
    edges = _mount(world, world.member_target, world.doomed, world.member)
    assert edges(world.member) == [("将被删除", True, "")]
    world.request()
    assert edges(world.member) == [(MASKED_MOUNT_NAME, False, MOUNT_BASE_UNAVAILABLE_REASON)]
    world.runtime.notebook_delete_jobs.cancel_pending(world.doomed, now=T0)
    assert edges(world.member) == [("将被删除", True, "")]


def case_a_mounted_library_being_deleted_says_it_was_deleted(world: World) -> None:
    """Past the window (``'deleting'``) the edge states the fact -- deleted --
    for everyone, its owner included; not "temporarily unavailable"."""
    world.sql.write("UPDATE notebooks SET tier='base' WHERE id=?", (world.doomed,))
    mine = _mount(world, world.owner_target, world.doomed, world.owner)
    theirs = _mount(world, world.member_target, world.doomed, world.member)
    world.request()
    world.runtime.notebook_delete_jobs.promote_due_pending(world.doomed, now=DEADLINE)
    assert mine(world.owner) == [("将被删除", False, MOUNT_BASE_DELETED_REASON)]
    assert theirs(world.member) == [(MASKED_MOUNT_NAME, False, MOUNT_BASE_DELETED_REASON)]


def case_cancel_restores_draft_when_nothing_was_recorded(world: World) -> None:
    world.request()
    world.sql.write(
        "UPDATE notebooks SET status_before_delete='' WHERE id=?", (world.doomed,)
    )
    assert world.runtime.notebook_delete_jobs.cancel_pending(world.doomed, now=T0) == "cancelled"
    assert world.status()["status"] == "draft"


def case_cancel_after_the_deadline_is_refused(world: World) -> None:
    world.request()
    store = world.runtime.notebook_delete_jobs
    with pytest.raises(NotebookDeleteDueError):
        store.cancel_pending(world.doomed, now=DEADLINE)  # due, not promoted
    assert world.status()["status"] == "pending_delete"
    assert store.promote_due_pending(world.doomed, now=DEADLINE) is not None
    with pytest.raises(NotebookDeleteDueError):
        store.cancel_pending(world.doomed, now=T0)  # promoted: too late whatever the clock
    with pytest.raises(KeyError):
        store.cancel_pending("nb-missing", now=T0)


# ------------------------------------------------------------ T3 (promote)


def case_promotion_waits_for_the_deadline_then_creates_one_job(world: World) -> None:
    world.request()
    store = world.runtime.notebook_delete_jobs
    assert store.list_due_pending(now=DEADLINE - timedelta(seconds=1)) == []
    assert store.promote_due_pending(world.doomed, now=DEADLINE - timedelta(seconds=1)) is None
    assert world.status()["status"] == "pending_delete"
    assert store.list_due_pending(now=DEADLINE) == [world.doomed]
    job = store.promote_due_pending(world.doomed, now=DEADLINE)
    assert job["status"] == "queued" and job["phase"] == "mark"
    assert world.status()["status"] == "deleting"
    assert store.promote_due_pending(world.doomed, now=DEADLINE) is None
    assert store.list_due_pending(now=DEADLINE + GRACE) == []
    assert [row["status"] for row in world.jobs()] == ["queued"]


def case_the_sweep_promotes_and_the_delete_runs_to_the_end(world: World) -> None:
    """Driver C through ``sweep_once`` (what the periodic sweeper and the
    startup sweep call -- a window that closed while the server was down is
    promoted on the next boot's sweep), then the six phases: the notebook and
    every member's Memory are gone."""
    world.request()
    runner = world.runtime.notebook_delete
    submitted: list[str] = []
    runner._submit = lambda job_id, notebook_id: submitted.append(job_id) or True
    assert runner.sweep_once(now=DEADLINE - timedelta(minutes=1)) == 0
    assert world.status()["status"] == "pending_delete"
    assert runner.sweep_once(now=DEADLINE + timedelta(minutes=5)) == 1
    assert world.status()["status"] == "deleting"
    [job_id] = submitted
    runner.run(job_id)
    assert world.status() is None
    assert world.sql.count(
        "SELECT COUNT(*) AS c FROM memory_items WHERE notebook_id=?", (world.doomed,)
    ) == 0


def case_one_failing_promotion_does_not_hold_back_the_others(world: World) -> None:
    """Driver C promotes each due window on its own: a failure on one leaves
    it in the window (the next tick retries it) and the rest of the tick --
    the other windows and driver B -- still runs."""
    world.request()
    world.clock[0] = T0 + timedelta(seconds=1)
    world.runtime.notebook_delete_jobs.request_pending(
        world.owner_target, world.owner.id, now=T0 + timedelta(seconds=1)
    )
    runner = world.runtime.notebook_delete
    store = runner.delete_jobs
    submitted: list[str] = []
    runner._submit = lambda job_id, notebook_id: submitted.append(notebook_id) or True
    real_promote = store.promote_due_pending

    def flaky(notebook_id: str, *, now: Any) -> Any:
        if notebook_id == world.doomed:
            raise RuntimeError("injected promotion failure")
        return real_promote(notebook_id, now=now)

    store.promote_due_pending = flaky
    try:
        assert runner.sweep_once(now=DEADLINE + timedelta(minutes=5)) == 1
    finally:
        del store.promote_due_pending
    assert submitted == [world.owner_target]
    assert world.status()["status"] == "pending_delete"
    assert world.status(world.owner_target)["status"] == "deleting"
    assert runner.sweep_once(now=DEADLINE + timedelta(minutes=6)) == 1
    assert submitted == [world.owner_target, world.doomed]
    assert world.status()["status"] == "deleting"


def _seed_activity(world: World, notebook_id: str, tag: str) -> str:
    """One notebook Ask job and one global Ask turn over ``notebook_id``, the
    rows the delete's retention (``retained_user_activity`` and #931's
    ``global_ask_jobs.retained_until``) projects. Returns the global job id."""
    stamp = iso(T0 - timedelta(days=1))
    world.sql.write(
        "INSERT INTO ask_jobs (id,notebook_id,created_by,mode,question,status,"
        "asked_at,answer_id,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"ask-{tag}", notebook_id, world.owner.id, "chunk", "同一个问题", "completed",
         "", "", "", stamp, stamp),
    )
    world.sql.write(
        "INSERT INTO global_ask_conversations(id,user_id,title,scope_json,submitted_via,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (f"gconv-{tag}", world.owner.id, "t", '{"mode":"all","notebook_ids":[]}', "web",
         stamp, stamp),
    )
    world.sql.write(
        "INSERT INTO global_ask_jobs(id,conversation_id,user_id,request_json,status,"
        "payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
        (f"gjob-{tag}", f"gconv-{tag}", world.owner.id, "{}", "done",
         '{"resolved_notebook_ids":["%s"]}' % notebook_id, stamp),
    )
    return f"gjob-{tag}"


def _retained(world: World, notebook_id: str, global_job: str) -> tuple[list, Any]:
    rows = world.sql.rows(
        "SELECT * FROM retained_user_activity WHERE notebook_id=? ORDER BY activity_type",
        (notebook_id,),
    )
    for row in rows:
        for column in ("notebook_id", "record_id", "notebook_name"):
            row.pop(column, None)
    [job] = world.sql.rows(
        "SELECT retained_until FROM global_ask_jobs WHERE id=?", (global_job,)
    )
    return rows, job["retained_until"]


def case_a_promoted_delete_leaves_what_a_direct_delete_leaves(world: World) -> None:
    """The window changes when the six phases start, nothing about what they
    leave: the retained activity projection and #931's stamp on the global
    turns are field for field those of an immediate delete at the same
    instant."""
    twin = world.owner_target
    doomed_job = _seed_activity(world, world.doomed, "doomed")
    twin_job = _seed_activity(world, twin, "twin")
    runner, store = world.runtime.notebook_delete, world.runtime.notebook_delete_jobs
    world.clock[0] = DEADLINE + timedelta(minutes=1)
    direct = store.request(twin, world.owner.id)
    runner.run(direct["id"])
    store.request_pending(world.doomed, world.owner.id, now=T0)
    promoted = store.promote_due_pending(world.doomed, now=DEADLINE)
    runner.run(promoted["id"])
    assert world.status() is None and world.status(twin) is None
    doomed_rows, doomed_stamp = _retained(world, world.doomed, doomed_job)
    twin_rows, twin_stamp = _retained(world, twin, twin_job)
    assert doomed_rows and doomed_rows == twin_rows
    assert doomed_stamp is not None and doomed_stamp == twin_stamp


def case_global_ask_loses_the_library_for_the_window_only(world: World) -> None:
    """Global Ask judges its libraries with the batch read check: the window
    takes the library out of every scope (B5) and a cancel brings it back."""
    authority = world.runtime.sharing_store
    assert authority.readable_notebook_ids([world.doomed], world.member.id) == {world.doomed}
    world.request()
    assert authority.readable_notebook_ids([world.doomed], world.member.id) == set()
    assert world.doomed not in authority.readable_notebook_names(world.member.id)
    world.runtime.notebook_delete_jobs.cancel_pending(world.doomed, now=T0)
    assert authority.readable_notebook_ids([world.doomed], world.member.id) == {world.doomed}


# ------------------------------------------------------- notices (B3, §5.3)


def notices(world: World, user: Any, at: datetime = T0) -> list[dict]:
    return world.runtime.queries.pending_delete_notices(user.id, now=at)


def case_notices_reach_the_owner_and_memory_holders_only(world: World) -> None:
    assert notices(world, world.owner) == []
    world.request()
    [owner_notice] = notices(world, world.owner)
    assert owner_notice == {
        "notebook_id": world.doomed, "name": "将被删除", "delete_after": iso(DEADLINE),
        "is_owner": True, "memory_count": 1, "transferable_count": 1,
    }
    [member_notice] = notices(world, world.member)
    assert member_notice["is_owner"] is False
    assert member_notice["memory_count"] == 4
    assert member_notice["transferable_count"] == 2  # candidate and proposed stay
    # A reader without Memory is not notified, but a direct open explains.
    assert notices(world, world.bystander) == []
    queries = world.runtime.queries
    [direct] = queries.pending_delete_notices(
        world.bystander.id, now=T0, notebook_id=world.doomed
    )
    assert direct["memory_count"] == 0 and direct["is_owner"] is False
    # Someone who cannot read it learns nothing.
    assert notices(world, world.outsider) == []
    assert queries.pending_delete_notices(
        world.outsider.id, now=T0, notebook_id=world.doomed
    ) == []
    # Closed window (due, not promoted): no notice any more.
    assert notices(world, world.member, at=DEADLINE) == []


def case_notices_follow_cancel_and_promotion(world: World) -> None:
    world.request()
    store = world.runtime.notebook_delete_jobs
    store.cancel_pending(world.doomed, now=T0)
    assert notices(world, world.owner) == notices(world, world.member) == []
    world.request()
    store.promote_due_pending(world.doomed, now=DEADLINE)
    assert notices(world, world.owner) == notices(world, world.member) == []


def case_notices_are_ordered_by_deadline(world: World) -> None:
    store = world.runtime.notebook_delete_jobs
    store.request_pending(world.owner_target, world.owner.id, now=T0 + timedelta(hours=2))
    world.request(at=T0)
    assert [row["notebook_id"] for row in notices(world, world.owner, T0 + timedelta(hours=3))] == [
        world.doomed, world.owner_target,
    ]


def case_the_bell_counts_the_notice(world: World) -> None:
    service = world.runtime.pending_actions_service
    before = service.list_for_user(world.member.id)
    world.request()
    after = service.list_for_user(world.member.id)
    [item] = [i for i in after["items"] if i["type"] == "notebook_pending_delete"]
    assert item == {
        "type": "notebook_pending_delete", "notebook_id": world.doomed,
        "notebook_name": "将被删除", "delete_after": iso(DEADLINE), "is_owner": False,
        "memory_count": 4, "transferable_count": 2,
    }
    assert after["count"] == before["count"] + 1
    assert after["items"][0] == item  # deadlines first


# --------------------------------------------------- export (§5.1, B4)


def export(world: World, user: Any, at: datetime = T0) -> Any:
    return world.runtime.memory_store.memory_export_snapshot(
        world.doomed, user.id, page_size=2, max_bytes=10**6, now=at
    )


def case_the_window_exports_own_memory(world: World) -> None:
    live = export(world, world.member)
    assert live.delete_after is None and len(live.items) == 4
    world.request()
    snapshot = export(world, world.member)
    assert snapshot.title == "将被删除"
    assert snapshot.delete_after == iso(DEADLINE)
    assert {item.id for item in snapshot.items} == {*world.confirmed, world.candidate, world.proposed}
    owner_snapshot = export(world, world.owner)  # B4: the owner exports too
    assert [item.id for item in owner_snapshot.items] == [world.owner_memory]
    assert export(world, world.bystander).items == ()
    with pytest.raises(KeyError) as outsider:
        export(world, world.outsider)
    assert not isinstance(outsider.value, DeleteWindowClosedError)


def case_the_export_closes_with_the_window(world: World) -> None:
    world.request()
    with pytest.raises(DeleteWindowClosedError):
        export(world, world.member, at=DEADLINE)  # due, not promoted
    world.runtime.notebook_delete_jobs.promote_due_pending(world.doomed, now=DEADLINE)
    with pytest.raises(DeleteWindowClosedError):
        export(world, world.member, at=T0)  # deleting: closed whatever the clock
    # Phase 3 removes the membership: the answer degrades to a plain 404.
    world.sql.write(
        "DELETE FROM notebook_members WHERE notebook_id=? AND user_id=?",
        (world.doomed, world.member.id),
    )
    with pytest.raises(KeyError) as plain:
        export(world, world.member, at=T0)
    assert not isinstance(plain.value, DeleteWindowClosedError)


def case_losing_read_access_inside_the_window_ends_the_export(world: World) -> None:
    world.request()
    world.sql.write(
        "DELETE FROM notebook_members WHERE notebook_id=? AND user_id=?",
        (world.doomed, world.member.id),
    )
    with pytest.raises(KeyError):
        export(world, world.member)
    assert notices(world, world.member) == []


def case_the_export_file_names_the_deadline(world: World) -> None:
    world.request()
    service = world.runtime.memory_service
    document = service.export_markdown(world.doomed, world.member.id)
    text = "".join(document.pieces)
    assert document.title == "将被删除"
    assert (
        f"这本笔记本已被所有者删除，{DEADLINE:%Y-%m-%d %H:%M}起其中的全部内容"
        "（包括这些记忆）将被永久删除。"
    ) in text
    # A live notebook's export carries no such line.
    world.runtime.notebook_delete_jobs.cancel_pending(world.doomed, now=T0)
    live = "".join(service.export_markdown(world.doomed, world.member.id).pieces)
    assert "已被所有者删除" not in live


# ------------------------------------------------ transfer (B9, 2026-10-22)


def transfer_page(
    world: World, user: Any, target: str, after: str | None = None
) -> dict:
    return world.runtime.memory_service.transfer_out_of_delete_window(
        user.id, world.doomed, target, after=after
    )


def transfer(world: World, user: Any, target: str) -> list[dict]:
    page = transfer_page(world, user, target)
    assert page["remaining"] == 0
    return page["results"]


def case_the_window_moves_confirmed_memory_to_my_notebook(world: World) -> None:
    world.request()
    results = transfer(world, world.member, world.member_target)
    assert sorted(row["source_id"] for row in results) == sorted(world.confirmed)
    assert all(row["ok"] and row["status"] == "moved" for row in results)
    for row in results:
        assert world.memory_notebook(row["new_id"]) == world.member_target
        assert world.memory_notebook(row["source_id"]) is None
    # Candidates and proposed items stay behind (the notice says how many).
    assert world.memory_notebook(world.candidate) == world.doomed
    assert world.memory_notebook(world.proposed) == world.doomed
    [notice] = notices(world, world.member)
    assert notice["memory_count"] == 2 and notice["transferable_count"] == 0
    # Nothing left to move: an empty, honest answer.
    assert transfer(world, world.member, world.member_target) == []


def _confirmed_memory(world: World, user: Any, tag: str) -> str:
    """One more confirmed item of ``user`` in ``doomed`` (before the window:
    a notebook inside it takes no writes)."""
    service = world.runtime.memory_service
    world.clock[0] += timedelta(seconds=1)
    item = service.create_candidate(
        world.doomed, user.id, None, f"req-{tag}", f"Title {tag}", f"Body {tag}.",
        [], "reason", {}, [],
    )
    service.confirm(item.id, user.id, {"extract_kg": False})
    world.clock[0] = T0
    return item.id


def _titles_in(world: World, notebook_id: str) -> list[str]:
    return sorted(row["title"] for row in world.sql.rows(
        "SELECT title FROM memory_items WHERE notebook_id=?", (notebook_id,)
    ))


@contextmanager
def _page_size(size: int):
    from app.services import memory_service as module

    previous = module.DELETE_WINDOW_TRANSFER_PAGE_SIZE
    module.DELETE_WINDOW_TRANSFER_PAGE_SIZE = size
    try:
        yield
    finally:
        module.DELETE_WINDOW_TRANSFER_PAGE_SIZE = previous


def case_the_transfer_goes_page_by_page_until_nothing_is_left(world: World) -> None:
    """More items than one page: each page moves at most the page size in id
    order, says how many are left and where the next starts; repeating until
    ``remaining`` is 0 moves every item exactly once."""
    extra = [_confirmed_memory(world, world.member, f"x{i}") for i in range(3)]
    world.request()
    expected = sorted([*world.confirmed, *extra])
    seen, remaining, after = [], [], None
    with _page_size(2):
        while True:
            page = transfer_page(world, world.member, world.member_target, after)
            assert len(page["results"]) <= 2
            assert all(row["ok"] and row["status"] == "moved" for row in page["results"])
            seen += [row["source_id"] for row in page["results"]]
            remaining.append(page["remaining"])
            after = page["next_after"]
            if page["remaining"] == 0:
                break
    assert remaining == [3, 1, 0]
    assert seen == expected
    assert _titles_in(world, world.member_target) == sorted(
        ["Title c1", "Title c2", "Title x0", "Title x1", "Title x2"]
    )


def case_the_next_page_starts_after_the_previous_one(world: World) -> None:
    """An item that fails stays in the notebook and stays transferable; the
    cursor keeps the next page from picking it up again, so every item is
    tried once and the run ends."""
    extra = _confirmed_memory(world, world.member, "x0")
    world.request()
    ordered = sorted([*world.confirmed, extra])
    store = world.runtime.memory_store
    real = store.memory_for_user

    def first_one_fails(memory_id, user_id, **kwargs):
        if memory_id == ordered[0]:
            raise RuntimeError("this one fails")
        return real(memory_id, user_id, **kwargs)

    store.memory_for_user = first_one_fails
    try:
        with _page_size(2):
            first = transfer_page(world, world.member, world.member_target)
            second = transfer_page(
                world, world.member, world.member_target, first["next_after"]
            )
    finally:
        store.memory_for_user = real
    assert [(r["source_id"], r["ok"]) for r in first["results"]] == [
        (ordered[0], False), (ordered[1], True),
    ]
    assert first["remaining"] == 1
    assert [(r["source_id"], r["ok"]) for r in second["results"]] == [(ordered[2], True)]
    assert second["remaining"] == 0
    assert world.memory_notebook(ordered[0]) == world.doomed


def case_a_page_after_the_deadline_closes_the_transfer(world: World) -> None:
    """The deadline passes between two pages: the next page is refused
    before it reads anything, the moved ones stay moved."""
    extra = [_confirmed_memory(world, world.member, f"x{i}") for i in range(3)]
    world.request()
    ordered = sorted([*world.confirmed, *extra])
    with _page_size(2):
        first = transfer_page(world, world.member, world.member_target)
        assert [row["source_id"] for row in first["results"]] == ordered[:2]
        assert first["remaining"] == 3
        world.runtime.notebook_delete_jobs.promote_due_pending(world.doomed, now=DEADLINE)
        world.clock[0] = DEADLINE
        with pytest.raises(DeleteWindowClosedError):
            transfer_page(world, world.member, world.member_target, first["next_after"])
    for source in ordered[:2]:
        assert world.memory_notebook(source) is None
    for source in ordered[2:]:
        assert world.memory_notebook(source) == world.doomed


def case_two_window_transfers_at_once_run_one(world: World) -> None:
    """Two tabs press「确认转移」together: one runs, the other is refused at
    once (``transfer_in_progress``); every item lands in the target exactly
    once and the one result list says so."""
    world.request()
    store = world.runtime.memory_store
    real = store.memory_for_user
    entered, release = threading.Event(), threading.Event()

    def paused(memory_id, user_id, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return real(memory_id, user_id, **kwargs)

    store.memory_for_user = paused
    outcome: dict = {}

    def first() -> None:
        outcome["results"] = transfer(world, world.member, world.member_target)

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert entered.wait(10)
        with pytest.raises(DeleteWindowTransferInProgressError):
            transfer(world, world.member, world.member_target)
    finally:
        release.set()
        thread.join(20)
        store.memory_for_user = real
    assert sorted(row["source_id"] for row in outcome["results"]) == sorted(world.confirmed)
    assert all(row["ok"] and row["status"] == "moved" for row in outcome["results"])
    assert _titles_in(world, world.member_target) == ["Title c1", "Title c2"]
    # The claim is released: a later transfer runs (nothing is left to move).
    assert transfer(world, world.member, world.member_target) == []


def case_a_source_removed_by_the_delete_after_its_copy_counts_as_moved(world: World) -> None:
    """The window closes and the notebook's delete runs between an item's
    copy and its source removal: the source is gone, so the item did leave
    -- ``moved``, not "copied, source kept"; the next item fails
    ``delete_window_closed``."""
    world.request()
    store = world.runtime.memory_store
    real = store.delete_memory_if_unchanged
    runner = world.runtime.notebook_delete
    calls = {"n": 0}

    def delete_runs_first(memory_id, user_id, revision):
        calls["n"] += 1
        if calls["n"] == 1:
            job = world.runtime.notebook_delete_jobs.promote_due_pending(
                world.doomed, now=DEADLINE
            )
            runner.run(job["id"])
            assert world.status() is None
        return real(memory_id, user_id, revision)

    store.delete_memory_if_unchanged = delete_runs_first
    try:
        results = world.runtime.memory_service.transfer_out_of_delete_window(
            world.member.id, world.doomed, world.member_target
        )["results"]
    finally:
        store.delete_memory_if_unchanged = real
    first, second = results
    assert (first["ok"], first["status"], first["error_code"]) == (True, "moved", None)
    assert world.memory_notebook(first["new_id"]) == world.member_target
    assert (second["ok"], second["error_code"]) == (False, DELETE_WINDOW_CLOSED_CODE)


def case_losing_access_part_way_reports_not_readable(world: World) -> None:
    """The caller's membership goes while the window is still open: the
    remaining items fail ``not_readable`` (the true reason), not
    ``delete_window_closed``."""
    world.request()
    store = world.runtime.memory_store
    real = store.memory_for_user
    calls = {"n": 0}

    def lose_access_before_the_second(memory_id, user_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            world.sql.write(
                "DELETE FROM notebook_members WHERE notebook_id=? AND user_id=?",
                (world.doomed, world.member.id),
            )
        return real(memory_id, user_id, **kwargs)

    store.memory_for_user = lose_access_before_the_second
    try:
        results = world.runtime.memory_service.transfer_out_of_delete_window(
            world.member.id, world.doomed, world.member_target
        )["results"]
    finally:
        store.memory_for_user = real
    first, second = results
    assert first["ok"] and first["status"] == "moved"
    assert (second["ok"], second["error_code"]) == (False, MEMORY_NOT_READABLE_CODE)
    assert world.memory_notebook(second["source_id"]) == world.doomed


def case_moved_memory_stays_moved_after_a_cancel(world: World) -> None:
    world.request()
    moved = transfer(world, world.member, world.member_target)
    world.runtime.notebook_delete_jobs.cancel_pending(world.doomed, now=T0)
    for row in moved:
        assert world.memory_notebook(row["new_id"]) == world.member_target
        assert world.memory_notebook(row["source_id"]) is None
    page = world.runtime.memory_service.list_memories(
        world.member.id, notebook_id=world.doomed
    )
    assert {item.id for item in page.items} == {world.candidate, world.proposed}


def case_the_target_must_be_a_live_notebook_i_own(world: World) -> None:
    world.request()
    with pytest.raises(PermissionError):
        transfer(world, world.member, world.owner_target)  # not mine
    with pytest.raises(PermissionError):
        transfer(world, world.owner, world.doomed)  # the deleted one itself
    world.sql.write(
        "UPDATE notebooks SET status='deleting' WHERE id=?", (world.member_target,)
    )
    with pytest.raises(PermissionError):
        transfer(world, world.member, world.member_target)  # not live
    assert {world.memory_notebook(mid) for mid in world.confirmed} == {world.doomed}
    with pytest.raises(KeyError):
        transfer(world, world.outsider, world.member_target)


def case_the_transfer_closes_with_the_window(world: World) -> None:
    world.request()
    world.clock[0] = DEADLINE
    with pytest.raises(DeleteWindowClosedError):
        transfer(world, world.member, world.member_target)
    assert {world.memory_notebook(mid) for mid in world.confirmed} == {world.doomed}


def case_the_window_read_itself_honours_the_deadline(world: World) -> None:
    """The per-item window read (``memory_for_user(delete_window_now=...)``)
    closes at the deadline on its own, even before the promotion: the
    service's up-front classification is not the only thing standing between
    a closed window and the caller's Memory."""
    world.request()
    store = world.runtime.memory_store
    item = world.confirmed[0]
    assert store.memory_for_user(
        item, world.member.id, delete_window_now=DEADLINE - timedelta(seconds=1)
    ).id == item
    assert world.status()["status"] == "pending_delete"
    with pytest.raises(KeyError):
        store.memory_for_user(item, world.member.id, delete_window_now=DEADLINE)
    with pytest.raises(KeyError):
        store.memory_for_user(item, world.member.id)


def case_a_promotion_part_way_fails_the_remaining_items(world: World) -> None:
    """Started inside the window; the promotion commits after the first item
    moved: the first stays moved, the second fails ``delete_window_closed``."""
    world.request()
    service = world.runtime.memory_service
    store = world.runtime.memory_store
    real = store.memory_for_user
    calls = {"n": 0}

    def promote_before_the_second_item(memory_id, user_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            world.runtime.notebook_delete_jobs.promote_due_pending(
                world.doomed, now=DEADLINE
            )
        return real(memory_id, user_id, **kwargs)

    store.memory_for_user = promote_before_the_second_item
    try:
        results = service.transfer_out_of_delete_window(
            world.member.id, world.doomed, world.member_target
        )["results"]
    finally:
        store.memory_for_user = real
    first, second = results
    assert first["ok"] and first["status"] == "moved"
    assert world.memory_notebook(first["new_id"]) == world.member_target
    assert second["ok"] is False and second["status"] == "failed"
    assert second["error_code"] == DELETE_WINDOW_CLOSED_CODE
    assert world.memory_notebook(second["source_id"]) == world.doomed


# ----------------------------------------- in-flight maintenance (§3, §8.12)


def case_in_flight_maintenance_survives_the_window(world: World) -> None:
    """The ``notebook_deleting`` checkpoint and the two "still alive" probes
    keep answering as for a live notebook during the window (in-flight work
    completes, a cancel finds it intact) and flip once promoted."""
    runtime = world.runtime
    checkpoint = runtime.knowledge_lifecycle._notebook_deleting
    alive = runtime.analysis_artifacts._notebook_alive
    world.request()
    assert runtime.notebook_store.status_of(world.doomed) == "pending_delete"
    assert checkpoint(world.doomed) is False
    assert alive(world.doomed) is True
    runtime.notebook_delete_jobs.promote_due_pending(world.doomed, now=DEADLINE)
    assert checkpoint(world.doomed) is True
    assert alive(world.doomed) is False


CASES: dict[str, Callable[[World], None]] = {
    name: fn for name, fn in globals().items()
    if name.startswith("case_") and callable(fn)
}
