"""Global conversation public sharing, PostgreSQL adapter (0057).

Runs the SAME case list as the SQLite twin
(``tests/test_global_ask_share_store.py``); see
``tests/global_ask_share_cases.py`` for why the scenarios live in one place.
"""
from __future__ import annotations

import pytest

from app.repositories.global_ask_store import GlobalAskStore
from app.repositories.postgres.migrator import PostgresMigrator
from tests.global_ask_share_cases import CASE_IDS, CASES


pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def store(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 61
    return GlobalAskStore(postgres_database, marker="%s")


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_global_ask_share_contract(store, case):
    case(store)


def test_a_new_job_and_a_share_serialize_on_the_conversation_row(store, postgres_scope):
    """插入新作业与分享必须在会话行上排队(codex #758 第 2 轮 P1)。

    PostgreSQL 的写事务是 READ COMMITTED、互不排队:一次提交读完 ``MAX(created_at)``
    之后若停住,别的事务可以在这段空档里插入、答完、分享,而它醒来后仍带着那个较早
    的时间戳插入,落进已发布的前缀。``create`` 因此先对会话行 ``FOR UPDATE``、再读
    最新时间戳;``share_conversation`` 取的是同一把行锁。

    并发靠握手:A 在**持锁**状态下停在「读完最新时间戳、尚未插入」这一点;断言落在
    「分享确实在等这把锁」(从 ``pg_stat_activity`` 读到 Lock 等待),不落在耗时上。
    上限只是安全网。SQLite 的 ``write()`` 本来就是进程级写锁,不需要这条用例。
    """
    import threading
    import time

    from app.models.global_ask import GlobalAskJob, GlobalNotebookScope
    from tests.global_ask_share_cases import insert_conversation, insert_job

    conversation_id, user_id = insert_conversation(store, "conv-lock", "user-lock")
    insert_job(store, conversation_id, user_id, "job-done", "2026-01-01T00:00:05+00:00")

    paused, release = threading.Event(), threading.Event()
    original = store._after_latest_job

    def stalled_after_reading(db, conv, stamp):
        result = original(db, conv, stamp)
        paused.set()
        assert release.wait(30), "the test never released the stalled insert"
        return result

    store._after_latest_job = stalled_after_reading
    slow = GlobalAskJob(
        job_id="job-slow", conversation_id=conversation_id, status="running",
        question="卡住的那一条", created_at="2026-01-01T00:00:01+00:00",
        notebook_scope=GlobalNotebookScope(mode="all"), resolved_notebook_ids=["nb-1"],
    )
    errors: list = []
    shared: dict = {}

    def insert_slow():
        try:
            store.create(slow, user_id, None, "{}", "web", new_conversation=False)
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    def share():
        try:
            shared.update(store.share_conversation(conversation_id, user_id))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    inserter = threading.Thread(target=insert_slow)
    inserter.start()
    assert paused.wait(30), "the insert never reached the stamp read"
    sharer = threading.Thread(target=share)
    sharer.start()

    # 观察用一条**池外**的直连:夹具的连接池上限是 2,两条都被上面两个事务占着。
    import psycopg

    waited = False
    with psycopg.connect(postgres_scope.url, autocommit=True) as probe:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and sharer.is_alive():
            waiting = probe.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()[0]
            if waiting >= 1:
                waited = True
                break
            time.sleep(0.01)
    assert waited and not shared, (
        "share_conversation did not wait for the in-flight insert: the two do not "
        "serialize on the conversation row"
    )

    release.set()
    inserter.join(30)
    sharer.join(30)
    assert errors == []
    assert slow.created_at > "2026-01-01T00:00:05+00:00"
    assert shared["shared_through_id"] == "job-done"

    slow.status = "done"
    assert store.save(slow, user_id) is True
    public = store.public_conversation_by_token(shared["share_token"])
    assert [job["job_id"] for job in public["jobs"]] == ["job-done"]
