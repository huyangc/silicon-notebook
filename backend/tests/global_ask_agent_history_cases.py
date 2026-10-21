"""Service cases: which global conversations an Agent token may list and read.

``GlobalAskService.agent_conversations`` / ``agent_conversation_turn`` judge a
global conversation on ALL of its live turns (plan D5): it is visible only when
every library any live turn used -- ran over, or names on an anchor or
citation -- is on the token's allowlist and readable by the owner now. One
library outside hides the whole conversation from the list and makes every
turn read as missing, even a turn that by itself is inside. A deleted library
counts as unreadable for an allowlist-bounded caller.

Backend-agnostic: ``tests/test_global_ask_agent_history.py`` (SQLite) and
``tests/postgres/test_global_ask_agent_history_pg.py`` (PostgreSQL) run every
case in ``CASES`` against ``create_app()``'s real repository inside one app
lifespan (``mcp_answer_link_cases.run_case``). Users, notebooks and group
grants go through the HTTP API; global turns are seeded as rows with
``global_job_payload``.
"""
from __future__ import annotations

from itertools import count

import pytest

from app.services.global_ask import GlobalAskError
from tests.answer_link_service_cases import _notebook, _user
from tests.global_ask_history_store_cases import global_job_payload
from tests.global_ask_share_cases import insert_conversation, insert_job

_IDS = count(1)


def _store(env):
    return env.repo._runtime.global_ask_store


def _conversation(env, owner_id) -> str:
    conversation_id = f"gconv-hist-{next(_IDS)}"
    insert_conversation(_store(env), conversation_id, owner_id)
    return conversation_id


def _turn(env, conversation_id, owner_id, resolved, *, status="done", created_at=None,
          job_id=None, **fields) -> str:
    n = next(_IDS)
    job_id = job_id or f"gask-hist-{n}"
    stamp = created_at or f"2026-01-01T00:{n // 60 % 60:02d}:{n % 60:02d}+00:00"
    return insert_job(
        _store(env), conversation_id, owner_id, job_id, stamp, status=status,
        payload=global_job_payload(job_id, conversation_id, status=status,
                                   created_at=stamp, resolved=resolved, **fields),
    )


def _listed(env, owner_id, allowed):
    return [
        (item.summary.id, item.notebook_ids, item.turn_count)
        for item in env.global_service.agent_conversations(
            user_id=owner_id, allowed_notebook_ids=allowed,
        )
    ]


def _read(env, conversation_id, owner_id, allowed, turn_offset=0):
    return env.global_service.agent_conversation_turn(
        conversation_id, user_id=owner_id, allowed_notebook_ids=allowed,
        turn_offset=turn_offset,
    )


def _refused(env, conversation_id, owner_id, allowed, turn_offset=0) -> str:
    with pytest.raises(GlobalAskError) as refused:
        _read(env, conversation_id, owner_id, allowed, turn_offset)
    assert refused.value.status_code == 404
    return refused.value.message


def _group_reader(env):
    """A notebook owned by someone else that ``reader`` reads through a group grant."""
    owner, _ = _user(env)
    nb = _notebook(env, owner, "组内库")
    reader, reader_id = _user(env)
    group_id = env.client.post("/api/groups", json={"name": "组"}, headers=owner).json()["id"]
    assert env.client.put(f"/api/groups/{group_id}/members/{reader_id}",
                          json={"role": "member"}, headers=owner).status_code == 200
    grant = env.client.post(
        f"/api/notebooks/{nb}/grants",
        json={"principal_type": "group", "principal_id": group_id, "role": "viewer"},
        headers=owner,
    )
    assert grant.status_code == 200, grant.text
    return owner, nb, reader_id, group_id


# ------------------------------------------------------------------ cases


async def conversation_inside_the_allowlist_is_listed_with_its_libraries_and_finished_count(env):
    owner, owner_id = _user(env)
    nb_a, nb_b = _notebook(env, owner, "甲"), _notebook(env, owner, "乙")
    cid = _conversation(env, owner_id)
    first = _turn(env, cid, owner_id, [nb_a])
    # Its evidence names nb_b although it ran over nb_a only.
    second = _turn(env, cid, owner_id, [nb_a], anchors=[nb_b])
    _turn(env, cid, owner_id, [nb_a], status="running")

    listed = env.global_service.agent_conversations(
        user_id=owner_id, allowed_notebook_ids=[nb_a, nb_b],
    )

    assert [(item.summary.id, item.notebook_ids, item.turn_count) for item in listed] == [
        (cid, tuple(sorted([nb_a, nb_b])), 2),
    ]
    assert listed[0].summary.title == "全局会话"
    conversation, job = _read(env, cid, owner_id, [nb_a, nb_b], 0)
    assert conversation == listed[0] and job.job_id == first
    assert _read(env, cid, owner_id, [nb_a, nb_b], 1)[1].job_id == second
    # The running question is not a turn yet.
    conversation, job = _read(env, cid, owner_id, [nb_a, nb_b], 2)
    assert conversation.turn_count == 2 and job is None


async def one_turn_outside_the_allowlist_hides_the_whole_conversation(env):
    owner, owner_id = _user(env)
    nb_a, nb_b = _notebook(env, owner, "甲"), _notebook(env, owner, "乙")
    crossing = _conversation(env, owner_id)
    _turn(env, crossing, owner_id, [nb_a])
    _turn(env, crossing, owner_id, [nb_a, nb_b])
    inside = _conversation(env, owner_id)
    _turn(env, inside, owner_id, [nb_a])

    assert [row[0] for row in _listed(env, owner_id, [nb_a])] == [inside]
    # Turn 0 is inside the allowlist on its own; the conversation is not.
    _refused(env, crossing, owner_id, [nb_a], 0)
    _refused(env, crossing, owner_id, [nb_a], 1)
    # Both libraries allowed: listed and readable.
    assert {row[0] for row in _listed(env, owner_id, [nb_a, nb_b])} == {inside, crossing}
    assert _read(env, crossing, owner_id, [nb_a, nb_b], 1)[1] is not None


async def evidence_library_outside_the_allowlist_hides_it(env):
    owner, owner_id = _user(env)
    nb_a, nb_b = _notebook(env, owner, "甲"), _notebook(env, owner, "乙")
    cid = _conversation(env, owner_id)
    # Ran over nb_a only; one anchor names nb_b.
    _turn(env, cid, owner_id, [nb_a], anchors=[nb_a, nb_b], cited=[])

    assert _listed(env, owner_id, [nb_a]) == []
    _refused(env, cid, owner_id, [nb_a])
    assert _listed(env, owner_id, [nb_a, nb_b]) == [(cid, tuple(sorted([nb_a, nb_b])), 1)]


async def owner_losing_read_hides_it_and_restoring_shows_it(env):
    owner, nb, reader_id, group_id = _group_reader(env)
    cid = _conversation(env, reader_id)
    job_id = _turn(env, cid, reader_id, [nb])
    assert _listed(env, reader_id, [nb]) == [(cid, (nb,), 1)]

    assert env.client.delete(f"/api/groups/{group_id}/members/{reader_id}",
                             headers=owner).status_code in (200, 204)
    assert _listed(env, reader_id, [nb]) == []
    _refused(env, cid, reader_id, [nb])

    assert env.client.put(f"/api/groups/{group_id}/members/{reader_id}",
                          json={"role": "member"}, headers=owner).status_code == 200
    assert _listed(env, reader_id, [nb]) == [(cid, (nb,), 1)]
    assert _read(env, cid, reader_id, [nb])[1].job_id == job_id


async def deleted_library_hides_it_for_an_allowlist_reader(env):
    owner, owner_id = _user(env)
    nb_a, nb_b = _notebook(env, owner, "甲"), _notebook(env, owner, "乙")
    cid = _conversation(env, owner_id)
    job_id = _turn(env, cid, owner_id, [nb_a, nb_b])

    env.repo._runtime.notebook_store.delete_row_and_orphan_embeddings(nb_b)

    # The owner's own (browser) read keeps the retained turn ...
    detail = env.global_service.conversation(cid, user_id=owner_id)
    assert [turn.job_id for turn in detail.turns] == [job_id]
    assert detail.turns[0].retained_until and detail.turns[0].deleted_notebook_ids == [nb_b]
    # ... an allowlist-bounded caller cannot prove it was ever allowed nb_b.
    assert _listed(env, owner_id, [nb_a, nb_b]) == []
    _refused(env, cid, owner_id, [nb_a, nb_b])


async def another_users_conversation_is_invisible(env):
    owner_b, owner_b_id = _user(env)
    nb = _notebook(env, owner_b, "乙的库")
    _, owner_a_id = _user(env)
    # A reads B's library, so only ownership can keep B's conversation away.
    env.repo._runtime.sharing_store.add_member(nb, owner_a_id)
    theirs = _conversation(env, owner_b_id)
    _turn(env, theirs, owner_b_id, [nb])
    assert _listed(env, owner_b_id, [nb]) == [(theirs, (nb,), 1)]

    assert _listed(env, owner_a_id, [nb]) == []
    foreign = _refused(env, theirs, owner_a_id, [nb])
    missing = _refused(env, "gconv-hist-missing", owner_a_id, [nb])
    assert foreign == missing


async def only_running_turn_is_visible_with_zero_turns_and_no_job(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    cid = _conversation(env, owner_id)
    _turn(env, cid, owner_id, [nb], status="running")

    assert _listed(env, owner_id, [nb]) == [(cid, (nb,), 0)]
    conversation, job = _read(env, cid, owner_id, [nb], 0)
    assert conversation.turn_count == 0 and job is None


async def turn_is_read_oldest_first(env):
    owner, owner_id = _user(env)
    nb = _notebook(env, owner)
    cid = _conversation(env, owner_id)
    # Inserted out of order; the tie at :02 is broken by id.
    for job_id, stamp in (("gask-ord-c", "2026-01-01T00:00:03+00:00"),
                          ("gask-ord-a", "2026-01-01T00:00:01+00:00"),
                          ("gask-ord-b2", "2026-01-01T00:00:02+00:00"),
                          ("gask-ord-b1", "2026-01-01T00:00:02+00:00")):
        _turn(env, cid, owner_id, [nb], job_id=job_id, created_at=stamp)

    read = [_read(env, cid, owner_id, [nb], offset)[1] for offset in range(5)]

    assert [job.job_id if job else None for job in read] == [
        "gask-ord-a", "gask-ord-b1", "gask-ord-b2", "gask-ord-c", None,
    ]


async def a_turn_shifted_in_after_the_judgement_is_rechecked(env):
    """D5's race: between the visibility judgement and the read, the first
    turn goes and a turn over another library finishes, shifting it into the
    index being read. The turn actually read is checked once more."""
    owner, owner_id = _user(env)
    nb_a, nb_b = _notebook(env, owner, "甲"), _notebook(env, owner, "乙")
    cid = _conversation(env, owner_id)
    first = _turn(env, cid, owner_id, [nb_a], created_at="2026-01-01T00:00:01+00:00")
    second = _turn(env, cid, owner_id, [nb_a], created_at="2026-01-01T00:00:02+00:00")
    # Before the change the conversation is visible and index 1 is ``second``.
    assert _read(env, cid, owner_id, [nb_a], 1)[1].job_id == second
    store = _store(env)
    judged = store.job_scope_rows
    changed = []

    def judged_then_changed(user_id, conversation_id=None):
        rows = judged(user_id, conversation_id=conversation_id)
        with store.database.write() as db:
            db.execute(store._sql("DELETE FROM global_ask_jobs WHERE id=?"), (first,))
        changed.append(_turn(env, cid, owner_id, [nb_b],
                             created_at="2026-01-01T00:00:03+00:00"))
        return rows

    env.monkeypatch.setattr(store, "job_scope_rows", judged_then_changed)
    _refused(env, cid, owner_id, [nb_a], 1)
    # The judgement ran on the old rows (all inside the allowlist); what was
    # read at index 1 is the shifted-in turn over nb_b, refused by the re-check.
    assert len(changed) == 1
    assert store.finished_job_at(cid, owner_id, 1).job_id == changed[0]


CASES = {case.__name__: case for case in (
    conversation_inside_the_allowlist_is_listed_with_its_libraries_and_finished_count,
    one_turn_outside_the_allowlist_hides_the_whole_conversation,
    evidence_library_outside_the_allowlist_hides_it,
    owner_losing_read_hides_it_and_restoring_shows_it,
    deleted_library_hides_it_for_an_allowlist_reader,
    another_users_conversation_is_invisible,
    only_running_turn_is_visible_with_zero_turns_and_no_job,
    turn_is_read_oldest_first,
    a_turn_shifted_in_after_the_judgement_is_rechecked,
)}
