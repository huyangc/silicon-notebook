"""Unsharing the link and joining through it, interleaved on PostgreSQL.

``clear_share`` locks the notebook row ``FOR UPDATE``, voids the token and
removes every link member in one transaction (``kick_all_members``'s
``DELETE ... RETURNING``); ``join_shared`` takes ``FOR KEY SHARE`` on the
notebook row, with the token in the predicate, before it inserts. Under READ
COMMITTED both orders must end with the joiner not a member and their private
overlay state cleared — the backend-neutral sequential cases are in
``tests/memory_purge_cases.py`` (``join_with_voided_link_refused`` and the
``unshare_*`` cases); the first two here pin the two locks:

* the unshare holds the row when the join arrives: the join waits, re-checks
  the token after the commit and inserts nothing (a 404). Without the lock the
  join would read the old token, commit after the unshare's ``DELETE`` had
  taken its snapshot, and stay a member of a notebook whose link is gone.
* the join holds the row when the unshare arrives: the unshare waits, and its
  ``DELETE`` (a later statement) sees and removes the new row, and returns it,
  so that member's state is cleared too.

The third pins what the join's lock does NOT block: an ordinary
``UPDATE notebooks SET name=…`` (``FOR NO KEY UPDATE``) goes through while a
join holds its ``FOR KEY SHARE``; a ``FOR SHARE`` there would queue it for the
whole summary hydration.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.models.schemas import NotebookCreate

pytestmark = pytest.mark.postgres_integration

_WAIT_SECONDS = 10.0


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.postgres_pool_max_size = 6
    postgres_settings.postgres_lock_timeout_seconds = 15
    postgres_settings.postgres_statement_timeout_seconds = 20
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _shared_notebook(repo):
    owner = repo._runtime.identity.create_user("o00200001", "pw123456")
    carol = repo._runtime.identity.create_user("c00200002", "pw123456")
    marker = set_request_user(owner)
    try:
        notebook_id = repo.create_notebook(NotebookCreate(name="Race")).id
    finally:
        reset_request_user(marker)
    token = repo._runtime.sharing.share_notebook(notebook_id)["share_token"]
    return notebook_id, carol, token


def _lock_waiters(repo) -> int:
    with repo._runtime.database.connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS c FROM pg_stat_activity "
            "WHERE datname=current_database() AND wait_event_type='Lock'"
        ).fetchone()
    return int(row["c"])


def _until_blocked_or_done(repo, thread: threading.Thread) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        if not thread.is_alive() or _lock_waiters(repo) > 0:
            return
        time.sleep(0.02)


def test_a_join_arriving_while_the_unshare_holds_the_row_is_refused(repo, monkeypatch):
    sharing = repo._runtime.sharing
    store = repo._runtime.sharing_store
    notebook_id, carol, token = _shared_notebook(repo)
    outcome: list[str] = []

    def join() -> None:
        try:
            sharing.join_shared(notebook_id, carol.id, share_token=token)
        except KeyError:
            outcome.append("404")
        else:
            outcome.append("joined")

    joiner = threading.Thread(target=join)
    real_kick = store.kick_all_members

    def kick_then_let_the_join_in(connection, nb_id):
        removed = real_kick(connection, nb_id)
        # The token UPDATE and the DELETE have both run; this transaction
        # still holds the notebook row. Start the join now and wait until it
        # is blocked on that row (or, without the lock, has finished).
        joiner.start()
        _until_blocked_or_done(repo, joiner)
        return removed

    monkeypatch.setattr(store, "kick_all_members", kick_then_let_the_join_in)
    sharing.unshare_notebook(notebook_id)
    joiner.join(_WAIT_SECONDS)
    assert not joiner.is_alive()
    assert outcome == ["404"]
    assert not sharing.is_member(notebook_id, carol.id)
    assert sharing.list_members(notebook_id) == []


def test_an_unshare_arriving_while_the_join_holds_the_row_removes_the_joiner(
    repo, monkeypatch
):
    sharing = repo._runtime.sharing
    store = repo._runtime.sharing_store
    notebook_id, carol, token = _shared_notebook(repo)
    errors: list[BaseException] = []

    def unshare() -> None:
        try:
            sharing.unshare_notebook(notebook_id)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    unsharer = threading.Thread(target=unshare)
    real_insert = store.insert_member_if_live

    def insert_then_let_the_unshare_in(connection, nb_id, user_id, now, share_token):
        inserted = real_insert(connection, nb_id, user_id, now, share_token)
        # The join holds FOR SHARE on the notebook row and its new member row
        # is not committed yet. Start the unshare and wait until its UPDATE is
        # blocked on the row.
        unsharer.start()
        _until_blocked_or_done(repo, unsharer)
        return inserted

    # Carol's overlay job row: cleared only if the unshare's DELETE returned her.
    repo._runtime.agent_profile.bump_signal(notebook_id, carol.id, 1)
    monkeypatch.setattr(store, "insert_member_if_live", insert_then_let_the_unshare_in)
    joined = sharing.join_shared(notebook_id, carol.id, share_token=token)
    assert joined.access == "reader"
    unsharer.join(_WAIT_SECONDS)
    assert not unsharer.is_alive()
    assert errors == []
    assert not sharing.is_member(notebook_id, carol.id)
    assert repo._runtime.agent_profile.job_row(notebook_id, carol.id) is None
    assert sharing.find_notebook_by_share_token(token) is None


def test_a_join_holding_its_row_lock_does_not_block_a_rename(repo, monkeypatch):
    sharing = repo._runtime.sharing
    store = repo._runtime.sharing_store
    notebook_id, carol, token = _shared_notebook(repo)
    renamed: list[str] = []

    def rename() -> None:
        try:
            with repo._runtime.database.write() as connection:
                connection.execute("SET LOCAL lock_timeout = '2s'")
                connection.execute(
                    "UPDATE notebooks SET name=%s WHERE id=%s", ("Renamed", notebook_id)
                )
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            renamed.append(type(exc).__name__)
        else:
            renamed.append("ok")

    real_insert = store.insert_member_if_live

    def insert_then_rename(connection, nb_id, user_id, now, share_token):
        inserted = real_insert(connection, nb_id, user_id, now, share_token)
        # The join holds its notebook row lock and has not committed.
        renamer = threading.Thread(target=rename)
        renamer.start()
        renamer.join(_WAIT_SECONDS)
        assert not renamer.is_alive()
        return inserted

    monkeypatch.setattr(store, "insert_member_if_live", insert_then_rename)
    joined = sharing.join_shared(notebook_id, carol.id, share_token=token)
    assert joined.access == "reader"
    assert renamed == ["ok"]
    assert sharing.is_member(notebook_id, carol.id)
