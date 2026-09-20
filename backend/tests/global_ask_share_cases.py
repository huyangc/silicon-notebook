"""Backend-agnostic contract cases for GLOBAL conversation public sharing.

``GlobalAskStore`` is one file serving both backends (SQLite and PostgreSQL,
told apart by ``marker``), so the share contract must hold identically on both.
Rather than transcribe the same scenarios into two test files and let them
drift, every case lives here once and both
``tests/test_global_ask_share_store.py`` (SQLite, standard lane) and
``tests/postgres/test_global_ask_share_store.py`` (PostgreSQL, integration
lane) parametrize over ``CASES``.

The cases only use the store's public methods plus direct row inserts, so they
say nothing about dialect; a case that passes on one backend and fails on the
other is exactly the divergence this file exists to catch.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.repositories.ports import (
    ConversationHasNoShareableAnswer,
    ConversationShareWatermarkStale,
)


def _integrity_errors() -> tuple[type[Exception], ...]:
    """The DB-API integrity error of whichever driver is under test.

    sqlite3 and psycopg share no common integrity-error base, so the assertion
    names both and lets the backend that is not installed drop out.
    """
    errors: list[type[Exception]] = [sqlite3.IntegrityError]
    try:  # pragma: no cover - depends on which lane is running
        import psycopg
    except ImportError:  # pragma: no cover
        pass
    else:
        errors.append(psycopg.IntegrityError)
    return tuple(errors)


def insert_conversation(store, conversation_id="conv-g", user_id="user-a"):
    with store.database.write() as db:
        db.execute(store._sql(
            "INSERT INTO global_ask_conversations"
            "(id,user_id,title,scope_json,submitted_via,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)"
        ), (conversation_id, user_id, "全局会话",
            json.dumps({"mode": "all", "notebook_ids": []}), "web",
            "2026-01-01T00:00:00", "2026-01-01T00:00:09"))
    return conversation_id, user_id


def insert_job(store, conversation_id, user_id, job_id, created_at,
               status="done", payload=None):
    body = payload if payload is not None else {
        "job_id": job_id, "status": status, "question": "q",
        "response": {"answer": f"answer-{job_id}"},
    }
    with store.database.write() as db:
        db.execute(store._sql(
            "INSERT INTO global_ask_jobs"
            "(id,conversation_id,user_id,client_request_id,request_json,"
            "status,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)"
        ), (job_id, conversation_id, user_id, None, "{}", status,
            json.dumps(body, ensure_ascii=False), created_at))
    return job_id


def _seed_three_done(store):
    """Three completed jobs across two instants; ``job-b``/``job-c`` are TIED.

    They are inserted ``job-c`` first so physical/insertion order disagrees
    with the canonical ``(created_at, id)`` order at the tied instant -- a
    keyset that fell back to insertion order would put ``job-c`` first and this
    seed would catch it.
    """
    conversation_id, user_id = insert_conversation(store)
    insert_job(store, conversation_id, user_id, "job-a", "2026-01-01T00:00:01")
    insert_job(store, conversation_id, user_id, "job-c", "2026-01-01T00:00:02")
    insert_job(store, conversation_id, user_id, "job-b", "2026-01-01T00:00:02")
    return conversation_id, user_id


def _public_ids(store, token):
    public = store.public_conversation_by_token(token)
    return [entry["job_id"] for entry in public["jobs"]]


def case_share_is_token_idempotent_but_advances_the_watermark(store):
    conversation_id, user_id = _seed_three_done(store)

    first = store.share_conversation(conversation_id, user_id)
    assert first["share_token"].startswith("gshr-")
    # "Latest" at the tied instant is the one that sorts LAST canonically --
    # job-c, not the physically-first-inserted row.
    assert first["shared_through_id"] == "job-c"
    assert first["shared_through_at"] == "2026-01-01T00:00:02"

    insert_job(store, conversation_id, user_id, "job-d", "2026-01-01T00:00:05")
    second = store.share_conversation(conversation_id, user_id)

    assert second["share_token"] == first["share_token"]
    assert second["shared_through_id"] == "job-d"
    assert store.conversation_share_state(conversation_id, user_id) == second


def case_a_turn_written_after_the_share_stays_private_until_reshared(store):
    conversation_id, user_id = _seed_three_done(store)
    token = store.share_conversation(conversation_id, user_id)["share_token"]

    insert_job(store, conversation_id, user_id, "job-d", "2026-01-01T00:00:05")

    assert _public_ids(store, token) == ["job-a", "job-b", "job-c"]
    store.share_conversation(conversation_id, user_id)
    assert _public_ids(store, token) == ["job-a", "job-b", "job-c", "job-d"]


def case_public_snapshot_tie_breaks_the_watermark_instant_by_id(store):
    conversation_id, user_id = _seed_three_done(store)
    # ``job-b`` and ``job-c`` share an instant. Pinning the boundary to the one
    # that sorts FIRST must exclude the other: a ``created_at <= watermark``
    # interval would wrongly publish both.
    store.share_conversation(conversation_id, user_id, expected_through_id="job-b")
    token = store.conversation_share_state(conversation_id, user_id)["share_token"]

    assert _public_ids(store, token) == ["job-a", "job-b"]


def case_expected_through_id_pins_an_older_boundary_then_refuses_to_regress(store):
    conversation_id, user_id = _seed_three_done(store)

    pinned = store.share_conversation(
        conversation_id, user_id, expected_through_id="job-a"
    )
    assert pinned["shared_through_id"] == "job-a"
    assert _public_ids(store, pinned["share_token"]) == ["job-a"]

    # Advancing is fine ...
    store.share_conversation(conversation_id, user_id, expected_through_id="job-c")
    # ... regressing to an already-superseded boundary is not.
    with pytest.raises(ConversationShareWatermarkStale):
        store.share_conversation(conversation_id, user_id, expected_through_id="job-a")

    assert store.conversation_share_state(
        conversation_id, user_id
    )["shared_through_id"] == "job-c"


def case_unresolvable_expected_through_id_is_stale_not_latest(store):
    conversation_id, user_id = _seed_three_done(store)
    insert_job(store, conversation_id, user_id, "job-run",
               "2026-01-01T00:00:03", status="running")
    other_conversation, _ = insert_conversation(store, "conv-other", user_id)
    insert_job(store, other_conversation, user_id, "job-elsewhere",
               "2026-01-01T00:00:04")

    for boundary in ("job-deleted", "job-run", "job-elsewhere"):
        with pytest.raises(ConversationShareWatermarkStale):
            store.share_conversation(
                conversation_id, user_id, expected_through_id=boundary
            )

    # Never published "latest" behind the user's back: still unshared.
    assert store.conversation_share_state(conversation_id, user_id) == {
        "share_token": "", "shared_through_at": "", "shared_through_id": "",
    }


def case_a_conversation_with_no_completed_job_mints_no_token(store):
    conversation_id, user_id = insert_conversation(store)
    insert_job(store, conversation_id, user_id, "job-run",
               "2026-01-01T00:00:01", status="running")
    insert_job(store, conversation_id, user_id, "job-fail",
               "2026-01-01T00:00:02", status="failed")

    with pytest.raises(ConversationHasNoShareableAnswer):
        store.share_conversation(conversation_id, user_id)

    assert store.conversation_share_state(conversation_id, user_id) == {
        "share_token": "", "shared_through_at": "", "shared_through_id": "",
    }


def case_only_the_owner_can_share_read_back_or_revoke(store):
    conversation_id, user_id = _seed_three_done(store)
    token = store.share_conversation(conversation_id, user_id)["share_token"]

    with pytest.raises(KeyError):
        store.share_conversation(conversation_id, "user-intruder")
    with pytest.raises(KeyError):
        store.conversation_share_state(conversation_id, "user-intruder")
    with pytest.raises(KeyError):
        store.share_conversation("conv-does-not-exist", user_id)

    store.unshare_conversation(conversation_id, "user-intruder")
    assert store.public_conversation_by_token(token) is not None


def case_revoking_kills_the_link_and_a_later_share_mints_a_new_token(store):
    conversation_id, user_id = _seed_three_done(store)
    first = store.share_conversation(conversation_id, user_id)["share_token"]

    store.unshare_conversation(conversation_id, user_id)
    store.unshare_conversation(conversation_id, user_id)  # idempotent

    assert store.public_conversation_by_token(first) is None
    assert store.conversation_share_state(conversation_id, user_id) == {
        "share_token": "", "shared_through_at": "", "shared_through_id": "",
    }

    second = store.share_conversation(conversation_id, user_id)["share_token"]
    assert second != first
    assert store.public_conversation_by_token(first) is None
    assert store.public_conversation_by_token(second) is not None


def case_unfinished_jobs_never_reach_the_public_list(store):
    conversation_id, user_id = insert_conversation(store)
    insert_job(store, conversation_id, user_id, "job-done", "2026-01-01T00:00:01")
    for job_id, status in (
        ("job-running", "running"),
        ("job-failed", "failed"),
        ("job-cancelled", "cancelled"),
        ("job-interrupted", "interrupted"),
    ):
        insert_job(store, conversation_id, user_id, job_id,
                   "2026-01-01T00:00:00", status=status)

    token = store.share_conversation(conversation_id, user_id)["share_token"]
    assert _public_ids(store, token) == ["job-done"]


def case_job_payloads_cross_untouched_in_both_shapes(store):
    conversation_id, user_id = insert_conversation(store)
    legacy = {"job_id": "job-old", "status": "done", "question": "旧问题",
              "response": {"answer": "旧形状答案", "citations": []}}
    current = {"job_id": "job-new", "status": "done", "question": "新问题",
               "answer": {"answer": "新形状答案", "citations": []}}
    insert_job(store, conversation_id, user_id, "job-old",
               "2026-01-01T00:00:01", payload=legacy)
    insert_job(store, conversation_id, user_id, "job-new",
               "2026-01-01T00:00:02", payload=current)

    token = store.share_conversation(conversation_id, user_id)["share_token"]
    public = store.public_conversation_by_token(token)

    assert [entry["payload"] for entry in public["jobs"]] == [legacy, current]
    assert public["id"] == conversation_id
    assert public["user_id"] == user_id
    assert public["title"] == "全局会话"
    assert public["shared_through_id"] == "job-new"


def case_an_unknown_or_blank_token_resolves_to_nothing(store):
    conversation_id, user_id = _seed_three_done(store)
    store.share_conversation(conversation_id, user_id)

    assert store.public_conversation_by_token("gshr-never-issued") is None
    assert store.public_conversation_by_token("") is None
    assert store.public_conversation_by_token(None) is None


def case_a_token_cannot_be_issued_twice(store):
    conversation_id, user_id = _seed_three_done(store)
    other_conversation, _ = insert_conversation(store, "conv-other", user_id)
    token = store.share_conversation(conversation_id, user_id)["share_token"]

    with pytest.raises(_integrity_errors()):
        with store.database.write() as db:
            db.execute(store._sql(
                "UPDATE global_ask_conversations SET share_token=? WHERE id=?"
            ), (token, other_conversation))

    # Two unshared rows both carrying NULL never collide on the partial index.
    third, _ = insert_conversation(store, "conv-third", user_id)
    assert store.conversation_share_state(third, user_id)["share_token"] == ""


def case_a_deleted_watermark_job_falls_back_to_the_interval_not_to_nothing(store):
    """水位作业事后被删:keyset 失锚,退回 ``created_at <= 水位时刻`` 的区间。

    一条已经发出去的链接不许因此变成空会话(fail closed);区间读比 keyset 在同刻
    并列上略宽,但仍然不越过水位时刻——之后写的轮次照样不公开。
    """
    conversation_id, user_id = _seed_three_done(store)
    token = store.share_conversation(conversation_id, user_id)["share_token"]
    insert_job(store, conversation_id, user_id, "job-d", "2026-01-01T00:00:05")
    with store.database.write() as db:
        db.execute(store._sql("DELETE FROM global_ask_jobs WHERE id=?"), ("job-c",))

    assert _public_ids(store, token) == ["job-a", "job-b"]


def case_the_public_read_is_capped_at_one_past_the_render_limit(store):
    """匿名读取有上界:渲染上限 + 1(多出的那一条只用来披露「被截断了」)。"""
    from app.domain.conversation_public_view import MAX_TURNS

    conversation_id, user_id = insert_conversation(store, "conv-long", "user-long")
    for index in range(MAX_TURNS + 5):
        # 真实写入形状:UTC ``isoformat()``,整秒时刻不带小数位。
        stamp = f"2026-01-01T{index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d}+00:00"
        insert_job(store, conversation_id, user_id, f"job-{index:04d}", stamp)
    token = store.share_conversation(conversation_id, user_id)["share_token"]

    ids = _public_ids(store, token)
    assert len(ids) == MAX_TURNS + 1
    assert ids[0] == "job-0000"


def case_production_shaped_timestamps_keep_lexical_and_temporal_order_aligned(store):
    """水位比较是**文本**比较,所以字典序必须等于时间序。

    生产写的是 ``datetime.now(timezone.utc).isoformat()``:带 ``+00:00``,微秒为 0 时
    省略小数位。整秒串是同秒小数串的前缀、``+`` 排在 ``.`` 之前,所以整秒时刻恒排在
    同一秒的小数时刻之前——与时间序一致。哪天有写入点改用 ``Z`` 后缀或本地偏移,
    这条会红。
    """
    conversation_id, user_id = insert_conversation(store, "conv-shape", "user-shape")
    insert_job(store, conversation_id, user_id, "job-whole", "2026-01-01T00:00:01+00:00")
    insert_job(store, conversation_id, user_id, "job-frac", "2026-01-01T00:00:01.250000+00:00")
    insert_job(store, conversation_id, user_id, "job-next", "2026-01-01T00:00:02+00:00")
    token = store.share_conversation(
        conversation_id, user_id, expected_through_id="job-frac")["share_token"]

    assert _public_ids(store, token) == ["job-whole", "job-frac"]


CASES = [
    case_share_is_token_idempotent_but_advances_the_watermark,
    case_a_turn_written_after_the_share_stays_private_until_reshared,
    case_public_snapshot_tie_breaks_the_watermark_instant_by_id,
    case_expected_through_id_pins_an_older_boundary_then_refuses_to_regress,
    case_unresolvable_expected_through_id_is_stale_not_latest,
    case_a_conversation_with_no_completed_job_mints_no_token,
    case_only_the_owner_can_share_read_back_or_revoke,
    case_revoking_kills_the_link_and_a_later_share_mints_a_new_token,
    case_unfinished_jobs_never_reach_the_public_list,
    case_job_payloads_cross_untouched_in_both_shapes,
    case_an_unknown_or_blank_token_resolves_to_nothing,
    case_a_token_cannot_be_issued_twice,
    case_a_deleted_watermark_job_falls_back_to_the_interval_not_to_nothing,
    case_the_public_read_is_capped_at_one_past_the_render_limit,
    case_production_shaped_timestamps_keep_lexical_and_temporal_order_aligned,
]
CASE_IDS = [case.__name__.removeprefix("case_") for case in CASES]
