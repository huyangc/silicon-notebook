"""PG 侧提问提交幂等键(v50 `ask_jobs.client_request_id`)的后端钉子。

行为本身后端中性,已由 tests/test_ask_jobs.py 在 sqlite 上覆盖;这里只钉必须真 PG
才能证的两件事:

- 同一用户同键重发在真 psycopg 事务里接回既有 job(不建第二行、不建第二个会话),
  另一笔记本同键报 ``AskRequestKeyConflict``;
- 部分唯一索引 ``idx_ask_jobs_client_request`` 真的在:直插同键第二行被
  ``UniqueViolation`` 拦下,而 NULL 键不参与该面;
- 查找与插入之间另一连接落了同键行(PG 的 SELECT 不挡并发 INSERT,与 SQLite 的
  ``BEGIN IMMEDIATE`` 围栏不同)时,存储层在事务外回读赢家并接回,而不是把
  ``UniqueViolation`` 抛成 500。
"""
from __future__ import annotations

import threading

import pytest
from psycopg import errors

from app.models.notebooks import NotebookCreate
from app.models.schemas import AskRequest
from app.repositories.ports import AskRequestKeyConflict
from app.repositories.postgres._store_utils import normalize_timestamp


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_ask_state_idempotency"),
]


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _count(repository, sql: str, params=()) -> int:
    with repository._runtime.database.connect() as db:
        return db.execute(sql, params).fetchone()["n"]


def test_repeated_key_attaches_and_other_notebook_conflicts(postgres_repository):
    repo = postgres_repository
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    nb = repo.create_notebook(NotebookCreate(name="idem")).id
    other = repo.create_notebook(NotebookCreate(name="other")).id

    first = AskRequest(question="Q?", mode="reasoning", client_request_id="pg-key-1")
    job_id, conv_id, attached = store.begin_or_attach_durable_job(nb, first, "reasoning", uid)
    assert not attached and first.conversation_id == conv_id

    again = AskRequest(question="Q?", mode="reasoning", client_request_id="pg-key-1")
    assert store.begin_or_attach_durable_job(nb, again, "reasoning", uid) == (
        job_id, conv_id, True)
    assert again.conversation_id == conv_id
    assert _count(repo, "SELECT COUNT(*) AS n FROM ask_jobs WHERE notebook_id=%s", (nb,)) == 1
    assert _count(repo, "SELECT COUNT(*) AS n FROM conversations WHERE notebook_id=%s", (nb,)) == 1

    with pytest.raises(AskRequestKeyConflict):
        store.begin_or_attach_durable_job(
            other, AskRequest(question="Q?", mode="chunk", client_request_id="pg-key-1"),
            "chunk", uid)
    assert _count(repo, "SELECT COUNT(*) AS n FROM ask_jobs WHERE notebook_id=%s", (other,)) == 0
    assert _count(
        repo, "SELECT COUNT(*) AS n FROM conversations WHERE notebook_id=%s", (other,)) == 0

    # A keyless submission never attaches to anything.
    plain = store.begin_or_attach_durable_job(
        nb, AskRequest(question="Q?", mode="chunk"), "chunk", uid)
    assert plain[2] is False and plain[0] != job_id


def test_partial_unique_index_guards_duplicates_but_not_null_keys(postgres_repository):
    repo = postgres_repository
    runtime = repo._runtime
    uid = repo.current_user().id
    nb = repo.create_notebook(NotebookCreate(name="idx")).id
    now = normalize_timestamp(runtime.seams.now())

    def insert(job_id: str, key):
        with runtime.database.write() as db:
            db.execute(
                "INSERT INTO ask_jobs (id,notebook_id,conversation_id,created_by,mode,"
                "question,asked_at,client_request_id,status,trace_json,answer_id,error,"
                "created_at,updated_at) VALUES (%s,%s,'',%s,'chunk','q','',%s,'done',"
                "'[]','','',%s,%s)",
                (job_id, nb, uid, key, now, now),
            )

    insert("askjob-idx-1", "dup-key")
    with pytest.raises(errors.UniqueViolation):
        insert("askjob-idx-2", "dup-key")
    insert("askjob-idx-3", None)
    insert("askjob-idx-4", None)
    assert _count(repo, "SELECT COUNT(*) AS n FROM ask_jobs WHERE notebook_id=%s", (nb,)) == 3


def test_lookup_insert_race_attaches_to_the_committed_winner(postgres_repository, monkeypatch):
    repo = postgres_repository
    runtime = repo._runtime
    store = runtime.ask_state
    uid = repo.current_user().id
    nb = repo.create_notebook(NotebookCreate(name="race")).id
    real_lookup = store._job_for_client_request
    state: dict = {}

    def lookup_then_lose(db, user_id, key):
        row = real_lookup(db, user_id, key)
        if row is None and "winner" not in state:
            # The "other process" commits the same key on its OWN connection
            # (a separate thread, so it cannot share this transaction) inside
            # our lookup→insert window.
            def other_process():
                winner = AskRequest(question="Q?", mode="chunk", client_request_id=key)
                state["winner"] = store.begin_durable_job(nb, winner, "chunk", user_id)

            thread = threading.Thread(target=other_process)
            thread.start()
            thread.join(timeout=10)
            assert "winner" in state
        return row

    monkeypatch.setattr(store, "_job_for_client_request", lookup_then_lose)
    loser = AskRequest(question="Q?", mode="chunk", client_request_id="pg-race")
    job_id, conv_id, attached = store.begin_or_attach_durable_job(nb, loser, "chunk", uid)
    assert attached and (job_id, conv_id) == state["winner"]
    assert loser.conversation_id == conv_id
    assert _count(repo, "SELECT COUNT(*) AS n FROM ask_jobs WHERE notebook_id=%s", (nb,)) == 1
    assert _count(repo, "SELECT COUNT(*) AS n FROM conversations WHERE notebook_id=%s", (nb,)) == 1
