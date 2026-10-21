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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from app.domain.notebook_delete_grace import (
    DELETE_WINDOW_CLOSED_CODE,
    NOTEBOOK_DELETE_GRACE_SECONDS,
)
from app.models.schemas import NotebookCreate
from app.repositories.ports import (
    DeleteWindowClosedError,
    NotebookAlreadyDeletingError,
    NotebookDeleteDueError,
    NotebookPendingDeleteError,
)
from app.services.embedding import FakeEmbedder
from tests.memory_purge_cases import Sql, _as_user
from tests.model_testkit import bind_all_embedding_clients

EMBED_DIM = 16
T0 = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
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
    assert "这本笔记本已被所有者删除，2026-10-23 08:00起其中的全部内容（包括这些记忆）将被永久删除。" in text
    # A live notebook's export carries no such line.
    world.runtime.notebook_delete_jobs.cancel_pending(world.doomed, now=T0)
    live = "".join(service.export_markdown(world.doomed, world.member.id).pieces)
    assert "已被所有者删除" not in live


# ------------------------------------------------ transfer (B9, 2026-10-22)


def transfer(world: World, user: Any, target: str) -> list[dict]:
    return world.runtime.memory_service.transfer_out_of_delete_window(
        user.id, world.doomed, target
    )


def case_the_window_moves_confirmed_memory_to_my_notebook(world: World) -> None:
    world.request()
    results = transfer(world, world.member, world.member_target)
    assert [row["source_id"] for row in results] == list(world.confirmed)
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
        )
    finally:
        store.memory_for_user = real
    first, second = results
    assert first["ok"] and first["status"] == "moved"
    assert world.memory_notebook(first["new_id"]) == world.member_target
    assert second["ok"] is False and second["status"] == "failed"
    assert second["error_code"] == DELETE_WINDOW_CLOSED_CODE
    assert world.memory_notebook(world.confirmed[1]) == world.doomed


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
