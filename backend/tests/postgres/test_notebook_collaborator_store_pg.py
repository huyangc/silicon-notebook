"""Named-share (editor) storage layer on PostgreSQL (share-edit P1-T5).

Runs every scenario of ``tests/test_notebook_collaborator_store.py`` (defined
once in ``tests/notebook_collaborator_store_cases.py``) against the production
PostgreSQL repository, plus the races only real row locks can show. Each race
is made deterministic with a pause injected right after the statement that
takes the lock under test, on one thread only:

* two first-time grants of the same editor: both pass the notebook's
  ``FOR SHARE`` before either inserts, and exactly one reports ``changed``;
* granting an editor vs publishing the notebook as a public library (PD-8),
  in both orders: the loser is refused and the notebook never ends up public
  WITH an editor. With the grant's lock weakened to ``FOR KEY SHARE`` the
  first order publishes underneath the grant and this fails;
* granting vs the notebook being deleted: the grant waits, then answers
  ``NotebookNotFoundError`` (a 404), never a foreign-key 500.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import pytest

from app.models.memory import MemberExitCompletion
from app.repositories.ports import (
    NotebookHasEditors,
    NotebookIsPublicLibrary,
    NotebookNotFoundError,
)
from tests.memory_purge_cases import EMBED_DIM
from tests.notebook_collaborator_store_cases import (
    CASES,
    MONKEYPATCH_CASES,
    build_world,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_collaborator_store"),
]

_BLOCKED_FOR = 0.6
# The grant's first statement (the notebook row lock): the gate matches the
# statement itself, not its lock clause, so a weakened lock still pauses here.
_GRANT_LOCK = "SELECT tier, created_by FROM notebooks"


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.embed_dim = EMBED_DIM
    # Two exit connections stay parked while a third observes their actual
    # blocking relation; the observation must not wait for a pool slot.
    postgres_settings.postgres_pool_max_size = 4
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_notebook_collaborator_store(repo, case):
    CASES[case](build_world(repo, postgres=True))


@pytest.mark.parametrize("case", sorted(MONKEYPATCH_CASES))
def test_notebook_collaborator_store_recorded(repo, monkeypatch, case):
    MONKEYPATCH_CASES[case](build_world(repo, postgres=True), monkeypatch)


# ------------------------------------------------------------------ races


class _Pausing:
    """Connection proxy: after a statement containing ``marker`` runs on a
    thread registered in ``gates``, signal that thread's ``reached`` event and
    wait for its ``release`` event before going on."""

    def __init__(self, connection, gates):
        self._connection = connection
        self._gates = gates

    def execute(self, query, params=None, *args, **kwargs):
        gate = self._gates.get(threading.get_ident())
        # A writer may block on an earlier shared root before reaching the
        # pause marker. Keep that connection observable to pg_blocking_pids.
        if gate is not None and not gate["done"]:
            gate["backend_pid"] = self._connection.info.backend_pid
        result = self._connection.execute(query, params, *args, **kwargs)
        if gate is not None and gate["marker"] in str(query) and not gate["done"]:
            gate["done"] = True
            gate["reached"].set()
            assert gate["release"].wait(10), "race gate never released"
        return result

    def __getattr__(self, name):
        return getattr(self._connection, name)


@contextmanager
def _pausable(repo, monkeypatch):
    gates: dict[int, dict] = {}
    databases = {
        id(db): db for db in (
            repo._runtime.database, repo._runtime.groups.database,
            repo._runtime.notebook_store.database,
        )
    }
    for database in databases.values():
        original = database.write

        @contextmanager
        def write(*args, _original=original, **kwargs):
            with _original(*args, **kwargs) as connection:
                yield _Pausing(connection, gates)

        monkeypatch.setattr(database, "write", write)
    try:
        yield gates
    finally:
        monkeypatch.undo()


def _start(gates, marker, fn):
    """Run ``fn`` on a thread that pauses after ``marker``; returns
    ``(thread, gate, outcome)`` -- ``outcome`` holds the result or exception."""
    gate = {
        "marker": marker, "done": False,
        "reached": threading.Event(), "release": threading.Event(),
    }
    outcome: dict = {}

    def run():
        gates[threading.get_ident()] = gate
        try:
            outcome["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - asserted by the test
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, gate, outcome


def _run(fn):
    outcome: dict = {}

    def run():
        try:
            outcome["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _grant_editor(world, user="editor"):
    return world.groups.set_user_editor_grant(
        world.notebook, owner_id=world.uid("owner"), target_user_id=world.uid(user)
    )


def _tier_and_editors(world):
    tier = world.rows("SELECT tier FROM notebooks WHERE id=?", (world.notebook,))[0]["tier"]
    editors = world.rows(
        "SELECT COUNT(*) AS c FROM notebook_grants WHERE notebook_id=? "
        "AND principal_type='user' AND role='editor'",
        (world.notebook,),
    )[0]["c"]
    return tier, int(editors)


def test_concurrent_first_put_notifies_once(repo, monkeypatch):
    world = build_world(repo, postgres=True)
    with _pausable(repo, monkeypatch) as gates:
        first = _start(gates, _GRANT_LOCK, lambda: _grant_editor(world))
        second = _start(gates, _GRANT_LOCK, lambda: _grant_editor(world))
        for _thread, gate, _outcome in (first, second):
            assert gate["reached"].wait(10)
        for _thread, gate, _outcome in (first, second):
            gate["release"].set()
        for thread, _gate, _outcome in (first, second):
            thread.join(10)
    results = [outcome.get("result") for _t, _g, outcome in (first, second)]
    errors = [outcome.get("error") for _t, _g, outcome in (first, second)]
    assert errors == [None, None], errors
    assert sorted(result["changed"] for result in results) == [False, True]
    assert len(world.groups.list_user_grants(world.notebook)) == 1


def test_editor_put_and_tier_publish_race_never_leaves_public_library_with_editor(
    repo, monkeypatch
):
    store = repo._runtime.notebook_store
    # order 1: the grant holds the notebook row, the publish comes second
    world = build_world(repo, postgres=True)
    with _pausable(repo, monkeypatch) as gates:
        grant = _start(gates, _GRANT_LOCK, lambda: _grant_editor(world))
        assert grant[1]["reached"].wait(10)
        publish, published = _run(lambda: store.set_tier(world.notebook, "base"))
        time.sleep(_BLOCKED_FOR)
        blocked = publish.is_alive()
        grant[1]["release"].set()
        grant[0].join(10)
        publish.join(10)
    assert blocked, "the publish did not wait for the grant's row lock"
    assert "error" not in grant[2], grant[2]
    assert isinstance(published.get("error"), NotebookHasEditors), published
    assert _tier_and_editors(world) == ("personal", 1)

    # order 2: the publish holds the notebook row, the grant comes second
    world.groups.delete_user_grant(world.notebook, world.uid("editor"))
    with _pausable(repo, monkeypatch) as gates:
        publish = _start(
            gates, "FOR NO KEY UPDATE", lambda: store.set_tier(world.notebook, "base")
        )
        assert publish[1]["reached"].wait(10)
        grant, granted = _run(lambda: _grant_editor(world))
        time.sleep(_BLOCKED_FOR)
        blocked = grant.is_alive()
        publish[1]["release"].set()
        publish[0].join(10)
        grant.join(10)
    assert blocked, "the grant did not wait for the publish's row lock"
    assert "error" not in publish[2], publish[2]
    assert isinstance(granted.get("error"), NotebookIsPublicLibrary), granted
    assert _tier_and_editors(world) == ("base", 0)


def test_set_editor_concurrent_with_notebook_delete_is_404_not_500(repo, monkeypatch):
    world = build_world(repo, postgres=True)

    def delete_notebook():
        # what the delete endpoint's request() does first: flip the status
        with repo._runtime.database.write() as db:
            db.execute(
                "UPDATE notebooks SET status='deleting' WHERE id=%s", (world.notebook,)
            )

    with _pausable(repo, monkeypatch) as gates:
        deleting = _start(gates, "status='deleting'", delete_notebook)
        assert deleting[1]["reached"].wait(10)
        grant, granted = _run(lambda: _grant_editor(world))
        time.sleep(_BLOCKED_FOR)
        blocked = grant.is_alive()
        deleting[1]["release"].set()
        deleting[0].join(10)
        grant.join(10)
    assert blocked, "the grant did not wait for the delete's row lock"
    assert isinstance(granted.get("error"), NotebookNotFoundError), granted
    assert world.groups.list_user_grants(world.notebook) == []


@pytest.mark.parametrize("first", ["member", "grant"])
def test_concurrent_exit_completions_lock_both_edges(repo, monkeypatch, first):
    """Both exit orders wait behind the common authority root/edge locks;
    the final disclosure retains the last edge and every Memory."""
    from tests.memory_purge_cases import wire_world

    world = build_world(repo, postgres=True)
    wire_world(repo)
    memory = repo._runtime.memory_service
    store = memory.store
    editor = world.uid("editor")
    world.groups.set_user_editor_grant(
        world.notebook, owner_id=world.uid("owner"), target_user_id=editor
    )
    repo._runtime.sharing.add_member(world.notebook, editor)
    memory.create_candidate(
        world.notebook, editor, None, "concurrent-exit", "Own memory", "Body.",
        [], "reason", {}, [],
    )
    member = store.member_exit_snapshot(world.notebook, editor, claim=True)
    grant = store.grant_exit_snapshot(world.notebook, editor, claim=True)
    finish = {
        "member": lambda: store.finish_member_exit(world.notebook, editor, member.membership_token),
        "grant": lambda: store.finish_grant_exit(world.notebook, editor, grant.membership_token),
    }
    second = "grant" if first == "member" else "member"
    marker = "SELECT added_at FROM notebook_members"
    with _pausable(repo, monkeypatch) as gates:
        initial = _start(gates, marker, finish[first])
        following = None
        blocked = False
        try:
            assert initial[1]["reached"].wait(10), "first exit never took its row lock"
            following = _start(gates, marker, finish[second])
            following[1]["release"].set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not following[2]:
                pid = following[1].get("backend_pid")
                if pid is not None:
                    with repo._runtime.database.connect() as db:
                        blocked = bool(db.execute(
                            "SELECT %s = ANY(pg_blocking_pids(%s)) AS blocked",
                            (initial[1]["backend_pid"], pid),
                        ).fetchone()["blocked"])
                    if blocked:
                        break
                following[1]["reached"].wait(0.01)
            assert blocked, "the second connection did not wait for the first exit"
        finally:
            initial[1]["release"].set()
            initial[0].join(15)
            if following is not None:
                following[1]["release"].set()
                following[0].join(15)
        assert not initial[0].is_alive() and not following[0].is_alive()
        assert initial[2] == {"result": MemberExitCompletion(0, True, True)}
        assert following[2] == {"result": MemberExitCompletion(1, False)}
    assert repo._runtime.sharing.user_can_read_notebook(world.notebook, editor)
    assert len(world.rows(
        "SELECT id FROM memory_items WHERE notebook_id=? AND created_by=?",
        (world.notebook, editor),
    )) == 1
