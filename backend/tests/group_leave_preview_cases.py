"""Leaving a group: the preview of what the leaver stops seeing, shared by both backends.

``test_group_leave_preview.py`` runs every case on SQLite and
``postgres/test_group_leave_preview_pg.py`` runs the same cases on PostgreSQL.

The preview (``MemoryService.group_exit_preview``, behind
``GET /groups/{id}/membership/leave-preview``) lists the notebooks holding the
leaver's own Memory that the leaver reads now and would no longer read once
the group membership ends, with the Memory count of each. The world puts one
notebook in every read-path combination the read predicate distinguishes:

* readable ONLY through the group (``group`` grant, and the ``group_admins``
  grant the leaver holds as a group admin)  -> listed;
* also readable through another group, a notebook membership row, a personal
  grant, an everyone grant, or ownership                             -> not listed;
* readable only through the group but holding no Memory of the leaver (only
  someone else's)                                                    -> not listed;
* someone else's private notebook still holding the leaver's Memory from an
  old membership (unreadable already)                                -> not listed;
* a notebook being deleted (not live)                                -> not listed.

Every case drives the production entry points: the preview through the
service, the leave through ``group_store.remove_member`` (the route's call),
the read check through ``sharing.user_can_read_notebook``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.schemas import NotebookCreate
from tests.memory_purge_cases import Sql, _as_user, wire_world

_STAMP = "2026-10-21T00:00:00+00:00"


@dataclass
class World:
    repo: Any
    sql: Sql
    owner: Any
    leaver: Any
    other: Any
    group: str
    other_group: str
    notebooks: dict[str, str] = field(default_factory=dict)
    # notebook key -> how many of the leaver's own Memory items it holds
    leaver_memory: dict[str, int] = field(default_factory=dict)


def _grant(world: World, key: str, principal_type: str, principal_id: str) -> None:
    notebook_id = world.notebooks[key]
    world.sql.write(
        "INSERT INTO notebook_grants "
        "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
        "VALUES (?,?,?,?,'viewer',?,?)",
        (
            f"gnt-{key}-{principal_type}-{principal_id}", notebook_id, principal_type,
            principal_id, world.owner.id, _STAMP,
        ),
    )


def _memory(world: World, key: str, user: Any, n: int, *, status: str = "confirmed") -> None:
    svc = world.repo._runtime.memory_service
    for index in range(n):
        item = svc.create_candidate(
            world.notebooks[key], user.id, None, f"req-{key}-{user.id}-{index}",
            f"{key} {index}", f"Body of {key} {index}.", [], "reason", {}, [],
        )
        if status == "confirmed" and index % 2 == 0:
            # Every status counts: leave odd ones as candidates.
            svc.confirm(item.id, user.id, {"extract_kg": False})
    if user.id == world.leaver.id:
        world.leaver_memory[key] = world.leaver_memory.get(key, 0) + n


# Notebooks the leaver stops reading by leaving ``group`` (and holds Memory in).
EXPECTED_HIDDEN = ("only_group", "only_group_admins")
# Every other notebook of the world holding the leaver's Memory.
EXPECTED_KEPT = (
    "also_other_group", "also_member", "also_user_grant", "also_everyone", "own",
)


def build_world(repo: Any, *, postgres: bool) -> World:
    wire_world(repo)
    sql = Sql(repo, postgres=postgres)
    identity = repo._runtime.identity
    owner = identity.create_user("o00210001", "pw123456")
    leaver = identity.create_user("l00210002", "pw123456")
    other = identity.create_user("m00210003", "pw123456")
    groups = repo._runtime.groups
    group = groups.create_group(
        name="Leave me", kind="project", description="", created_by=owner.id
    )["id"]
    other_group = groups.create_group(
        name="Stay here", kind="project", description="", created_by=owner.id
    )["id"]
    # The leaver is a group ADMIN of ``group`` (so ``group_admins`` grants
    # reach them) next to the owner — not the last admin, so the leave goes.
    groups.upsert_member(group, leaver.id, role="admin", added_by=owner.id)
    groups.upsert_member(group, other.id, role="member", added_by=owner.id)
    groups.upsert_member(other_group, leaver.id, role="member", added_by=owner.id)
    world = World(repo, sql, owner, leaver, other, group, other_group)

    def notebook(key: str, by: Any, name: str) -> None:
        world.notebooks[key] = _as_user(
            by, lambda: repo.create_notebook(NotebookCreate(name=name))
        ).id

    # Names sort differently from creation order: the preview orders by name.
    notebook("only_group", owner, "B 只经本组")
    notebook("only_group_admins", owner, "A 只经本组管理员")
    notebook("also_other_group", owner, "C 也经别的组")
    notebook("also_member", owner, "D 也是成员")
    notebook("also_user_grant", owner, "E 也有个人授权")
    notebook("also_everyone", owner, "F 全员可读")
    notebook("own", leaver, "G 自己的")
    notebook("no_leaver_memory", owner, "H 只有别人的记忆")
    notebook("old_private", owner, "I 早已读不到")
    notebook("deleting", owner, "J 删除中")

    for key in (
        "only_group", "also_other_group", "also_member", "also_user_grant",
        "also_everyone", "own", "no_leaver_memory", "deleting",
    ):
        _grant(world, key, "group", group)
    _grant(world, "only_group_admins", "group_admins", group)
    _grant(world, "also_other_group", "group", other_group)
    repo._runtime.sharing.add_member(world.notebooks["also_member"], leaver.id)
    _grant(world, "also_user_grant", "user", leaver.id)
    _grant(world, "also_everyone", "everyone", "")

    _memory(world, "only_group", leaver, 3)
    _memory(world, "only_group_admins", leaver, 2)
    for key in EXPECTED_KEPT:
        _memory(world, key, leaver, 1)
    _memory(world, "no_leaver_memory", other, 2)
    _memory(world, "only_group", other, 1)  # someone else's never counts
    # Saved while a plain member; the owner removed the member since (that
    # never deletes Memory), so the leaver reads this notebook no more.
    repo._runtime.sharing.add_member(world.notebooks["old_private"], leaver.id)
    _memory(world, "old_private", leaver, 2)
    repo._runtime.sharing.remove_member(world.notebooks["old_private"], leaver.id)
    _memory(world, "deleting", leaver, 1)
    sql.write(
        "UPDATE notebooks SET status='deleting' WHERE id=?",
        (world.notebooks["deleting"],),
    )
    return world


# ---------------------------------------------------------------------- reads


def preview(world: World, user: Any | None = None, group: str | None = None) -> list[dict]:
    return world.repo._runtime.memory_service.group_exit_preview(
        group or world.group, (user or world.leaver).id
    )


def readable(world: World, key: str, user: Any | None = None) -> bool:
    return world.repo._runtime.sharing.user_can_read_notebook(
        world.notebooks[key], (user or world.leaver).id
    )


def memory_rows(world: World, key: str, user: Any | None = None) -> int:
    return world.sql.count(
        "SELECT COUNT(*) AS c FROM memory_items WHERE notebook_id=? AND created_by=?",
        (world.notebooks[key], (user or world.leaver).id),
    )


def visible_memory_by_notebook(world: World) -> dict[str, int]:
    """The leaver's own Memory as the Memory page lists it (every status,
    every notebook the leaver reads), counted per notebook."""
    svc = world.repo._runtime.memory_service
    counts: dict[str, int] = {}
    offset = 0
    while True:
        page = svc.list_memories(world.leaver.id, offset=offset, limit=100)
        for item in page.items:
            counts[item.notebook_id] = counts.get(item.notebook_id, 0) + 1
        offset += len(page.items)
        if not page.items or offset >= page.total_count:
            return counts


def leave(world: World) -> bool:
    return world.repo._runtime.groups.remove_member(world.group, world.leaver.id)


# ---------------------------------------------------------------------- cases


def case_preview_lists_only_what_the_group_alone_lets_the_leaver_read(world: World) -> None:
    rows = preview(world)
    assert rows == [
        {
            "notebook_id": world.notebooks[key],
            "name": name,
            "memory_count": world.leaver_memory[key],
        }
        for key, name in (
            ("only_group_admins", "A 只经本组管理员"),
            ("only_group", "B 只经本组"),
        )
    ]
    assert sum(row["memory_count"] for row in rows) == 5


def case_preview_matches_what_the_leave_actually_hides(world: World) -> None:
    """Same measure as the leave itself: the preview equals the notebooks
    whose read check flips from true to false when the leave runs, and the
    Memory those notebooks drop from the leaver's Memory list."""
    rows = preview(world)
    holding = [key for key, count in world.leaver_memory.items() if count]
    before = {key: readable(world, key) for key in holding}
    visible_before = visible_memory_by_notebook(world)
    rows_before = {key: memory_rows(world, key) for key in holding}

    assert leave(world) is True

    after = {key: readable(world, key) for key in holding}
    flipped = {key for key in holding if before[key] and not after[key]}
    assert {row["notebook_id"] for row in rows} == {
        world.notebooks[key] for key in flipped
    }
    assert flipped == set(EXPECTED_HIDDEN)
    visible_after = visible_memory_by_notebook(world)
    lost = {
        notebook_id: count - visible_after.get(notebook_id, 0)
        for notebook_id, count in visible_before.items()
        if count != visible_after.get(notebook_id, 0)
    }
    assert lost == {row["notebook_id"]: row["memory_count"] for row in rows}
    # Leaving deletes nothing: every row is still there.
    assert {key: memory_rows(world, key) for key in holding} == rows_before
    # After the leave there is nothing left to preview for this group.
    assert preview(world) == []


def case_rejoining_shows_the_memory_again(world: World) -> None:
    rows = preview(world)
    visible_before = visible_memory_by_notebook(world)
    assert leave(world) is True
    world.repo._runtime.groups.upsert_member(
        world.group, world.leaver.id, role="admin", added_by=world.owner.id
    )
    assert visible_memory_by_notebook(world) == visible_before
    assert preview(world) == rows


def case_a_plain_member_does_not_count_group_admins_grants(world: World) -> None:
    """Demoted to a plain member, the leaver no longer reads the notebook
    shared with the group's admins only: it is no longer theirs to lose."""
    world.repo._runtime.groups.upsert_member(
        world.group, world.leaver.id, role="member", added_by=world.owner.id
    )
    assert not readable(world, "only_group_admins")
    assert [row["notebook_id"] for row in preview(world)] == [
        world.notebooks["only_group"]
    ]


def case_non_members_and_other_groups_preview_nothing(world: World) -> None:
    # Not a member of the group: nothing (the route answers 404 first).
    assert preview(world, user=world.owner, group=world.other_group) == []
    stranger = world.repo._runtime.identity.create_user("s00210004", "pw123456")
    assert preview(world, user=stranger) == []
    # The other group: every notebook it shares with the leaver is also
    # reachable another way (``also_other_group`` through ``group``).
    assert preview(world, group=world.other_group) == []


def case_preview_counts_only_the_callers_own_memory(world: World) -> None:
    """Another member's preview of the same group counts their own Memory
    only — never the leaver's in the same notebooks."""
    rows = preview(world, user=world.other)
    assert rows == [
        {
            "notebook_id": world.notebooks["only_group"],
            "name": "B 只经本组",
            "memory_count": 1,
        },
        {
            "notebook_id": world.notebooks["no_leaver_memory"],
            "name": "H 只有别人的记忆",
            "memory_count": 2,
        },
    ]


CASES = {
    name: fn for name, fn in globals().items()
    if name.startswith("case_") and callable(fn)
}
