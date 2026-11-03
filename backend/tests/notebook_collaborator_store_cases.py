"""Named-share (editor) storage layer, shared by both backends (share-edit P1-T5).

``test_notebook_collaborator_store.py`` runs every case on SQLite and
``postgres/test_notebook_collaborator_store_pg.py`` runs the same cases on
PostgreSQL, plus the PostgreSQL-only races (barriers on real row locks). Every
case drives the production stores: ``groups`` (the grant rows),
``notebook_store.set_tier`` (publishing) and the Memory store's collaboration
exit (``grant_exit_snapshot`` / ``finish_grant_exit``).
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.models.memory import MemberExitCompletion, MemberExitOutcome
from app.models.schemas import NotebookCreate
from app.repositories.ports import (
    CollaboratorTargetInvalid,
    NotebookHasEditors,
    NotebookIsPublicLibrary,
    NotebookNotFoundError,
)
from app.services.memory_service import ExitDisclosureRequired

_STAMP = "2026-10-28T00:00:00+00:00"


@dataclass
class World:
    repo: Any
    postgres: bool
    notebook: str
    users: dict[str, Any] = field(default_factory=dict)

    def sql(self, text: str) -> str:
        return text.replace("?", "%s") if self.postgres else text

    def write(self, text: str, params: tuple = ()) -> None:
        with self.repo._runtime.database.write() as db:
            db.execute(self.sql(text), params)

    def rows(self, text: str, params: tuple = ()) -> list[dict]:
        with self.repo._runtime.database.connect() as db:
            return [dict(row) for row in db.execute(self.sql(text), params).fetchall()]

    @property
    def groups(self):
        return self.repo._runtime.groups

    def uid(self, name: str) -> str:
        return self.users[name].id


def _as_user(user: Any, fn: Callable[[], Any]) -> Any:
    token = set_request_user(user)
    try:
        return fn()
    finally:
        reset_request_user(token)


def build_world(repo: Any, *, postgres: bool) -> World:
    identity = repo._runtime.identity
    users = {
        name: identity.create_user(f"c{20261100 + index:08d}", "pw123456")
        for index, name in enumerate(("owner", "viewer", "admin", "editor", "stranger"))
    }
    notebook = _as_user(
        users["owner"], lambda: repo.create_notebook(NotebookCreate(name="Collab"))
    ).id
    return World(repo=repo, postgres=postgres, notebook=notebook, users=users)


def _user_grant(world: World, user: str, role: str, grant_id: str) -> None:
    world.write(
        "INSERT INTO notebook_grants "
        "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (grant_id, world.notebook, "user", world.uid(user), role, world.uid("owner"),
         _STAMP),
    )


def _set_editor(world: World, user: str) -> dict:
    return world.groups.set_user_editor_grant(
        world.notebook, owner_id=world.uid("owner"), target_user_id=world.uid(user)
    )


# ---------------------------------------------------------------- cases


def set_editor_upgrades_viewer_and_downgrades_admin(world: World) -> None:
    _user_grant(world, "viewer", "viewer", "gc-viewer")
    _user_grant(world, "admin", "admin", "gc-admin")
    for user, grant_id, previous in (("viewer", "gc-viewer", "viewer"),
                                     ("admin", "gc-admin", "admin")):
        result = _set_editor(world, user)
        assert result["changed"] is True, user
        assert result["previous_role"] == previous, user
        assert result["grant"]["id"] == grant_id, "the row is updated in place"
        assert result["grant"]["role"] == "editor"
        assert world.groups.user_grant_role(world.notebook, world.uid(user)) == "editor"
    fresh = _set_editor(world, "editor")
    assert fresh["changed"] is True and fresh["previous_role"] is None
    assert fresh["grant"]["username"] == world.users["editor"].username
    assert fresh["grant"]["granted_by"] == world.uid("owner")
    listed = world.groups.list_user_grants(world.notebook)
    assert {row["user_id"] for row in listed} == {
        world.uid("viewer"), world.uid("admin"), world.uid("editor"),
    }
    assert {row["role"] for row in listed} == {"editor"}
    names = world.groups.list_editor_names(world.notebook)
    assert {row["username"] for row in names} == {
        world.users[name].username for name in ("viewer", "admin", "editor")
    }


def set_editor_idempotent_reports_unchanged(world: World) -> None:
    first = _set_editor(world, "editor")
    second = _set_editor(world, "editor")
    assert (first["changed"], second["changed"]) == (True, False)
    assert second["previous_role"] == "editor"
    assert second["grant"]["id"] == first["grant"]["id"]
    assert len(world.groups.list_user_grants(world.notebook)) == 1


def set_editor_refuses_public_library(world: World) -> None:
    world.write("UPDATE notebooks SET tier='base' WHERE id=?", (world.notebook,))
    with pytest.raises(NotebookIsPublicLibrary):
        _set_editor(world, "editor")
    assert world.groups.list_user_grants(world.notebook) == []


def set_editor_refuses_owner_self_and_unknown_users(world: World) -> None:
    for target in (world.uid("owner"), "user-does-not-exist"):
        with pytest.raises(CollaboratorTargetInvalid):
            world.groups.set_user_editor_grant(
                world.notebook, owner_id=world.uid("owner"), target_user_id=target
            )
    world.write("UPDATE users SET status='disabled' WHERE id=?", (world.uid("editor"),))
    with pytest.raises(CollaboratorTargetInvalid):
        _set_editor(world, "editor")
    assert world.groups.list_user_grants(world.notebook) == []


def set_editor_pending_delete_or_foreign_is_not_found(world: World) -> None:
    with pytest.raises(NotebookNotFoundError):
        world.groups.set_user_editor_grant(
            world.notebook, owner_id=world.uid("stranger"),
            target_user_id=world.uid("editor"),
        )
    world.write(
        "UPDATE notebooks SET status='pending_delete' WHERE id=?", (world.notebook,)
    )
    with pytest.raises(NotebookNotFoundError):
        _set_editor(world, "editor")
    with pytest.raises(NotebookNotFoundError):
        world.groups.set_user_editor_grant(
            "nb-missing", owner_id=world.uid("owner"), target_user_id=world.uid("editor")
        )


def tier_base_refused_while_editors_exist(world: World) -> None:
    store = world.repo._runtime.notebook_store
    _user_grant(world, "viewer", "viewer", "gc-viewer")  # a named viewer does not block
    _set_editor(world, "editor")
    with pytest.raises(NotebookHasEditors):
        store.set_tier(world.notebook, "base")
    assert world.rows("SELECT tier FROM notebooks WHERE id=?", (world.notebook,))[0][
        "tier"
    ] == "personal"
    assert world.groups.delete_user_grant(world.notebook, world.uid("editor")) is True
    store.set_tier(world.notebook, "base")
    assert world.rows("SELECT tier FROM notebooks WHERE id=?", (world.notebook,))[0][
        "tier"
    ] == "base"


def group_delete_grant_cannot_touch_user_rows(world: World) -> None:
    _set_editor(world, "editor")
    grant_id = world.groups.list_user_grants(world.notebook)[0]["id"]
    assert world.groups.delete_grant(world.notebook, grant_id) is False
    assert world.groups.user_grant_role(world.notebook, world.uid("editor")) == "editor"
    assert world.groups.delete_user_grant(world.notebook, world.uid("editor")) is True
    assert world.groups.delete_user_grant(world.notebook, world.uid("editor")) is False


def list_grants_excludes_user_rows(world: World) -> None:
    _set_editor(world, "editor")
    _user_grant(world, "viewer", "viewer", "gc-viewer")
    world.write(
        "INSERT INTO notebook_grants "
        "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("gc-everyone", world.notebook, "everyone", "", "viewer", None, _STAMP),
    )
    listed = world.groups.list_grants(world.notebook)
    assert [row["principal_type"] for row in listed] == ["everyone"]
    assert {row["user_id"] for row in world.groups.list_user_grants(world.notebook)} == {
        world.uid("editor"), world.uid("viewer"),
    }


class _Recording:
    def __init__(self, connection, statements):
        self._connection = connection
        self._statements = statements

    def execute(self, query, params=None, *args, **kwargs):
        self._statements.append(str(query))
        return self._connection.execute(query, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


@contextmanager
def recording(database, monkeypatch, statements: list[str]):
    for name in ("connect", "write"):
        original = getattr(database, name)

        @contextmanager
        def wrapped(*args, _original=original, **kwargs):
            with _original(*args, **kwargs) as connection:
                yield _Recording(connection, statements)

        monkeypatch.setattr(database, name, wrapped)
    try:
        yield
    finally:
        monkeypatch.undo()


def owner_aggregate_collaborators_one_statement(world: World, monkeypatch) -> None:
    second = _as_user(
        world.users["owner"],
        lambda: world.repo.create_notebook(NotebookCreate(name="Second")),
    ).id
    _set_editor(world, "editor")
    world.groups.set_user_editor_grant(
        second, owner_id=world.uid("owner"), target_user_id=world.uid("viewer")
    )
    # someone else's notebook never shows up
    other = _as_user(
        world.users["stranger"],
        lambda: world.repo.create_notebook(NotebookCreate(name="Other")),
    ).id
    world.groups.set_user_editor_grant(
        other, owner_id=world.uid("stranger"), target_user_id=world.uid("editor")
    )
    statements: list[str] = []
    with recording(world.groups.database, monkeypatch, statements):
        rows = world.groups.list_user_grants_for_owner(world.uid("owner"))
    assert len(statements) == 1, statements
    assert {(row["notebook_id"], row["username"], row["role"]) for row in rows} == {
        (world.notebook, world.users["editor"].username, "editor"),
        (second, world.users["viewer"].username, "editor"),
    }


def concurrent_member_and_grant_exits_acknowledge_last_edge(
    world: World, monkeypatch, *, memory_count: int = 1, suggestion_count: int = 0
) -> None:
    """Both exit tabs see the other edge before either completion; the last
    completion must ask for Memory acknowledgement and preserve its edge."""
    from tests.memory_purge_cases import wire_world

    wire_world(world.repo)
    memory = world.repo._runtime.memory_service
    store = memory.store
    editor = world.uid("editor")
    _set_editor(world, "editor")
    world.repo._runtime.sharing.add_member(world.notebook, editor)
    if memory_count:
        memory.create_candidate(
            world.notebook, editor, None, "cross-exit", "Own memory", "Body.",
            [], "reason", {}, [],
        )
    for index in range(suggestion_count):
        item = memory.create_candidate(
            world.notebook, editor, None, f"cross-draft-{index}", "Suggested", "Body.",
            [], "reason", {}, [],
        )
        world.write("UPDATE memory_items SET suggested_signal='clarified' WHERE id=?", (item.id,))
    suggest = world.repo._runtime.memory_suggest_store
    suggest.set_memory_mode(editor, "suggest", now=_STAMP)
    suggest.note_signal(world.notebook, editor, ask_job_id="cross-ask", now=_STAMP)
    jobs_before = world.rows("SELECT * FROM memory_suggest_jobs WHERE notebook_id=? AND owner_id=?", (world.notebook, editor))
    retained = world.rows(
        "SELECT * FROM memory_items WHERE notebook_id=? AND created_by=?",
        (world.notebook, editor),
    )
    cleaned: list[tuple[str, str]] = []
    cleanup = memory.membership.forget_member_state

    def remember_cleanup(notebook_id, user_id):
        cleaned.append((notebook_id, user_id))
        cleanup(notebook_id, user_id)

    monkeypatch.setattr(memory.membership, "forget_member_state", remember_cleanup)
    claimed = threading.Barrier(2)
    member_snapshot = store.member_exit_snapshot
    grant_snapshot = store.grant_exit_snapshot

    def member_claim(*args, **kwargs):
        result = member_snapshot(*args, **kwargs)
        if kwargs.get("claim"):
            claimed.wait(timeout=10)
        return result

    def grant_claim(*args, **kwargs):
        result = grant_snapshot(*args, **kwargs)
        if kwargs.get("claim"):
            claimed.wait(timeout=10)
        return result

    monkeypatch.setattr(store, "member_exit_snapshot", member_claim)
    monkeypatch.setattr(store, "grant_exit_snapshot", grant_claim)
    outcomes: dict[str, Any] = {}

    def leave(name, operation):
        try:
            outcomes[name] = operation()
        except ExitDisclosureRequired as exc:
            outcomes[name] = ("disclosure", exc.memory_count, exc.suggestion_count)
        except Exception as exc:
            outcomes[name] = type(exc).__name__

    operations = {
        "member": lambda: memory.leave_notebook(world.notebook, editor, None),
        "grant": lambda: memory.leave_collaboration(
            world.notebook, editor, None,
        ),
    }
    threads = [threading.Thread(target=leave, args=(name, operation))
               for name, operation in operations.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "exit did not complete"
    if not memory_count and not suggestion_count:
        assert outcomes == {"member": MemberExitOutcome(0, 0, ended=True), "grant": MemberExitOutcome(0, 0, ended=True)}
    else:
        assert list(outcomes.values()).count(MemberExitOutcome(0, 0, ended=True)) == 1, outcomes
        assert list(outcomes.values()).count(("disclosure", memory_count, suggestion_count)) == 1, outcomes
        assert world.repo._runtime.sharing.user_can_read_notebook(world.notebook, editor)
        assert cleaned == []  # still reading after the successful first exit
        assert world.rows(
            "SELECT * FROM memory_items WHERE notebook_id=? AND created_by=?",
            (world.notebook, editor),
        ) == retained
        assert world.rows("SELECT * FROM memory_suggest_jobs WHERE notebook_id=? AND owner_id=?", (world.notebook, editor)) == jobs_before
        assert world.repo._runtime.sharing.is_member(world.notebook, editor) is (
            outcomes["member"] != MemberExitOutcome(0, 0, ended=True)
        )
        assert (world.groups.user_grant_role(world.notebook, editor) == "editor") is (
            outcomes["grant"] != MemberExitOutcome(0, 0, ended=True)
        )
        monkeypatch.setattr(store, "member_exit_snapshot", member_snapshot)
        monkeypatch.setattr(store, "grant_exit_snapshot", grant_snapshot)
        refused = next(name for name, value in outcomes.items() if value != MemberExitOutcome(0, 0, ended=True))
        finish = memory.leave_notebook if refused == "member" else memory.leave_collaboration
        assert finish(world.notebook, editor, memory_count) == MemberExitOutcome(memory_count, suggestion_count, ended=True)
    assert cleaned == [(world.notebook, editor)]
    assert not world.repo._runtime.sharing.user_can_read_notebook(world.notebook, editor)
    assert world.rows(
        "SELECT id FROM memory_items WHERE notebook_id=? AND created_by=?",
        (world.notebook, editor),
    ) == []
    assert world.rows("SELECT * FROM memory_suggest_jobs WHERE notebook_id=? AND owner_id=?", (world.notebook, editor)) == []


def user_grant_revocation_clears_only_lost_read_windows(world: World) -> None:
    """The store transaction clears jobs even without service cleanup; read survivors retain them."""
    suggest = world.repo._runtime.memory_suggest_store
    loss = "2026-10-28T12:00:00+00:00"
    new_window = "2026-10-29T00:00:00+00:00"
    world.groups.now = world.repo._runtime.sharing._store.now = lambda: _STAMP
    for name in ("editor", "viewer"):
        _set_editor(world, name)
        user = world.uid(name)
        suggest.set_memory_mode(user, "suggest", now=_STAMP)
        suggest.note_signal(world.notebook, user, ask_job_id="old-window", now=_STAMP)
        assert suggest.claim(world.notebook, user, claim_token=f"old-{name}", now=_STAMP) is not None
    world.repo._runtime.sharing.add_member(world.notebook, world.uid("viewer"))
    world.groups.now = lambda: loss
    for name in ("editor", "viewer"):
        assert world.groups.delete_user_grant(world.notebook, world.uid(name))
    rows = world.rows("SELECT owner_id FROM memory_suggest_jobs WHERE notebook_id=?", (world.notebook,))
    assert rows == [{"owner_id": world.uid("viewer")}]
    survivor = world.rows("SELECT window_started_at,claim_token FROM memory_suggest_jobs WHERE notebook_id=? AND owner_id=?",
                          (world.notebook, world.uid("viewer")))[0]
    assert survivor["claim_token"] == "old-viewer"
    assert datetime.fromisoformat(str(survivor["window_started_at"])).astimezone(timezone.utc) == datetime.fromisoformat(_STAMP)
    world.groups.now = lambda: new_window
    _set_editor(world, "editor")
    suggest.note_signal(world.notebook, world.uid("editor"), ask_job_id="new-window", now=new_window)
    row = world.rows("SELECT window_started_at FROM memory_suggest_jobs WHERE notebook_id=? AND owner_id=?", (world.notebook, world.uid("editor")))[0]
    assert datetime.fromisoformat(str(row["window_started_at"])).astimezone(timezone.utc) == datetime.fromisoformat(new_window)


def grant_exit_locks_grant_row_and_keeps_memory_when_still_readable(
    world: World, monkeypatch
) -> None:
    from tests.memory_purge_cases import wire_world

    wire_world(world.repo)
    memory = world.repo._runtime.memory_service
    store = memory.store
    _set_editor(world, "editor")
    _set_editor(world, "viewer")
    editor, viewer = world.uid("editor"), world.uid("viewer")
    for user, count in ((editor, 2), (viewer, 1)):
        for index in range(count):
            memory.create_candidate(
                world.notebook, user, None, f"req-{user}-{index}", f"t{index}",
                f"Body {index}.", [], "reason", {}, [],
            )
    # the viewer also reads as a link member: dropping the grant keeps reading
    world.repo._runtime.sharing.add_member(world.notebook, viewer)
    kept = store.grant_exit_snapshot(world.notebook, viewer, claim=False)
    assert (kept.is_member, kept.keeps_access, kept.memory_count) == (True, True, 0)
    statements: list[str] = []
    with recording(store.database, monkeypatch, statements):
        claimed = store.grant_exit_snapshot(world.notebook, editor, claim=True)
    assert (claimed.is_member, claimed.keeps_access, claimed.memory_count) == (True, False, 2)
    assert len(claimed.memory_ids) == 2
    if world.postgres:
        # the claim locks the grant row: no Memory write of this reader can
        # land while the ids are read
        assert any(
            "FROM notebook_grants" in sql and "FOR UPDATE" in sql for sql in statements
        ), statements
    # Memory still there: the grant stays and the count is reported
    assert store.finish_grant_exit(world.notebook, editor, claimed.membership_token) == MemberExitCompletion(
        2, False,
    )
    assert world.groups.user_grant_role(world.notebook, editor) == "editor"
    # a token of a different grant never ends the current one
    assert store.finish_grant_exit(world.notebook, editor, "gnt-other") == MemberExitCompletion(0, False)
    world.write("DELETE FROM memory_items WHERE notebook_id=? AND created_by=?",
                (world.notebook, editor))
    assert store.finish_grant_exit(world.notebook, editor, claimed.membership_token) == MemberExitCompletion(
        0, True,
    )
    assert world.groups.user_grant_role(world.notebook, editor) is None
    nobody = store.grant_exit_snapshot(world.notebook, world.uid("stranger"), claim=False)
    assert (nobody.is_member, nobody.memory_count) == (False, 0)


CASES: dict[str, Callable[..., None]] = {
    "user_grant_revocation_clears_only_lost_read_windows": user_grant_revocation_clears_only_lost_read_windows,
    "set_editor_upgrades_viewer_and_downgrades_admin": (
        set_editor_upgrades_viewer_and_downgrades_admin
    ),
    "set_editor_idempotent_reports_unchanged": set_editor_idempotent_reports_unchanged,
    "set_editor_refuses_public_library": set_editor_refuses_public_library,
    "set_editor_refuses_owner_self_and_unknown_users": (
        set_editor_refuses_owner_self_and_unknown_users
    ),
    "set_editor_pending_delete_or_foreign_is_not_found": (
        set_editor_pending_delete_or_foreign_is_not_found
    ),
    "tier_base_refused_while_editors_exist": tier_base_refused_while_editors_exist,
    "group_delete_grant_cannot_touch_user_rows": group_delete_grant_cannot_touch_user_rows,
    "list_grants_excludes_user_rows": list_grants_excludes_user_rows,
}

MONKEYPATCH_CASES: dict[str, Callable[..., None]] = {
    "concurrent_suggestions_only_exits_disclose_last_edge": (
        lambda world, monkeypatch: concurrent_member_and_grant_exits_acknowledge_last_edge(
            world, monkeypatch, memory_count=0, suggestion_count=2
        )
    ),
    "concurrent_mixed_exits_disclose_last_edge": (
        lambda world, monkeypatch: concurrent_member_and_grant_exits_acknowledge_last_edge(
            world, monkeypatch, suggestion_count=2
        )
    ),
    "concurrent_empty_member_and_grant_exits_end_last_edge": (
        lambda world, monkeypatch: concurrent_member_and_grant_exits_acknowledge_last_edge(
            world, monkeypatch, memory_count=0
        )
    ),
    "concurrent_member_and_grant_exits_acknowledge_last_edge": (
        concurrent_member_and_grant_exits_acknowledge_last_edge
    ),
    "owner_aggregate_collaborators_one_statement": (
        owner_aggregate_collaborators_one_statement
    ),
    "grant_exit_locks_grant_row_and_keeps_memory_when_still_readable": (
        grant_exit_locks_grant_row_and_keeps_memory_when_still_readable
    ),
}
