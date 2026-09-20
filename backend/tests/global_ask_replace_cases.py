"""Backend-agnostic contract cases for the two ways a stopped global job leaves.

"Edit and re-send" replaces it; "stopped before anything was shown" discards it.

``GlobalAskStore.create(replaces_job_id=…)`` deletes the stopped job the user is
re-asking inside the same transaction that inserts its replacement. One store
file serves both backends, so -- like ``global_ask_share_cases`` -- every case
lives here once and both ``tests/test_global_ask_replace_store.py`` (SQLite) and
``tests/postgres/test_global_ask_replace_store.py`` (PostgreSQL) run ``CASES``.
"""
from __future__ import annotations

import pytest

from app.models.global_ask import GlobalAskJob, GlobalNotebookScope
from app.repositories.global_ask_ports import ReplacedJobUnavailable

USER = "user-a"


def make_job(identifier, *, question="第一个问题", conversation_id="conv-r", status="running"):
    return GlobalAskJob(
        job_id=identifier, conversation_id=conversation_id, status=status,
        question=question, created_at="2026-09-20T00:00:00+00:00",
        notebook_scope=GlobalNotebookScope(mode="include", notebook_ids=["nb-a"]),
        resolved_notebook_ids=["nb-a"],
    )


def start(store, identifier, *, question="第一个问题", new_conversation=False, replaces=None,
          user_id=USER, conversation_id="conv-r"):
    job = make_job(identifier, question=question, conversation_id=conversation_id)
    store.create(job, user_id, f"request-{identifier}", "{}", "web",
                 new_conversation=new_conversation, replaces_job_id=replaces)
    return job


def finish(store, job, status, user_id=USER):
    job.status = status
    assert store.save(job, user_id)


def job_ids(store, conversation_id="conv-r", user_id=USER):
    return [item.job_id for item in reversed(store.jobs(conversation_id, user_id, 50, 0))]


def case_a_stopped_newest_job_is_replaced_in_the_same_insert(store):
    first = start(store, "job-1", new_conversation=True)
    finish(store, first, "done")
    stopped = start(store, "job-2", question="打错的问题")
    finish(store, stopped, "cancelled")
    start(store, "job-3", question="改好的问题", replaces="job-2")
    assert job_ids(store) == ["job-1", "job-3"]
    assert store.job("job-2", USER) is None
    assert store.job("job-3", USER).status == "running"


def case_the_replacement_still_sorts_after_everything_the_conversation_held(store):
    first = start(store, "job-1", new_conversation=True)
    finish(store, first, "cancelled")
    replacement = start(store, "job-2", replaces="job-1")
    # Same caller stamp as the job it replaced: the clamp runs BEFORE the delete.
    assert replacement.created_at > first.created_at


def case_an_answered_job_cannot_be_replaced(store):
    _refuse(store, "done")


def case_a_failed_job_cannot_be_replaced(store):
    _refuse(store, "failed")


def case_a_running_job_cannot_be_replaced(store):
    # The one-running-job index would refuse the insert anyway; the point is
    # that it is refused as "not replaceable", and that nothing was deleted.
    _refuse(store, "running")


def _refuse(store, status):
    first = start(store, "job-1", new_conversation=True)
    if status != "running":
        finish(store, first, status)
    with pytest.raises(ReplacedJobUnavailable):
        start(store, "job-2", replaces="job-1")
    assert job_ids(store) == ["job-1"]
    assert store.job("job-2", USER) is None


def case_only_the_newest_job_can_be_replaced(store):
    older = start(store, "job-1", new_conversation=True)
    finish(store, older, "cancelled")
    newer = start(store, "job-2")
    finish(store, newer, "done")
    with pytest.raises(ReplacedJobUnavailable):
        start(store, "job-3", replaces="job-1")
    assert job_ids(store) == ["job-1", "job-2"]


def case_a_job_of_another_conversation_or_user_cannot_be_named(store):
    other = start(store, "job-x", new_conversation=True, conversation_id="conv-other")
    finish(store, other, "cancelled")
    mine = start(store, "job-1", new_conversation=True)
    finish(store, mine, "cancelled")
    with pytest.raises(ReplacedJobUnavailable):
        start(store, "job-2", replaces="job-x")
    foreign = start(store, "job-f", new_conversation=True, user_id="user-b", conversation_id="conv-b")
    finish(store, foreign, "cancelled", user_id="user-b")
    with pytest.raises(ReplacedJobUnavailable):
        start(store, "job-3", replaces="job-f")
    assert job_ids(store) == ["job-1"]
    assert store.job("job-x", USER) is not None
    assert store.job("job-f", "user-b") is not None


def case_a_new_conversation_has_nothing_to_replace(store):
    with pytest.raises(ReplacedJobUnavailable):
        start(store, "job-1", new_conversation=True, replaces="job-0")
    assert store.conversation("conv-r", USER) is None


def case_an_automatic_title_follows_the_replacement_when_nothing_else_is_left(store):
    first = start(store, "job-1", question="打错的问题", new_conversation=True)
    finish(store, first, "cancelled")
    start(store, "job-2", question="改好的问题", replaces="job-1")
    assert store.conversation("conv-r", USER).title == "改好的问题"


def case_a_title_the_user_typed_is_left_alone(store):
    first = start(store, "job-1", question="打错的问题", new_conversation=True)
    finish(store, first, "cancelled")
    assert store.rename("conv-r", USER, "我的课题")
    start(store, "job-2", question="改好的问题", replaces="job-1")
    assert store.conversation("conv-r", USER).title == "我的课题"


def case_the_title_stays_when_other_turns_remain(store):
    first = start(store, "job-1", question="第一个问题", new_conversation=True)
    finish(store, first, "done")
    stopped = start(store, "job-2", question="打错的问题")
    finish(store, stopped, "cancelled")
    start(store, "job-3", question="改好的问题", replaces="job-2")
    assert store.conversation("conv-r", USER).title == "第一个问题"


def case_the_delete_and_the_insert_are_one_transaction(store):
    """A replacement whose INSERT fails must leave the stopped job where it was.

    The insert is made to fail on the per-user ``client_request_id`` uniqueness;
    were the delete committed on its own, the old record would be gone with
    nothing in its place.
    """
    answered = start(store, "job-1", new_conversation=True)
    finish(store, answered, "done")
    stopped = start(store, "job-2", question="打错的问题")
    finish(store, stopped, "cancelled")
    clash = make_job("job-3", question="改好的问题")
    with pytest.raises(Exception) as failure:
        store.create(clash, USER, "request-job-1", "{}", "web",
                     new_conversation=False, replaces_job_id="job-2")
    assert not isinstance(failure.value, ReplacedJobUnavailable)
    assert job_ids(store) == ["job-1", "job-2"]
    assert store.job("job-2", USER).status == "cancelled"


def case_a_discarded_first_question_takes_its_conversation_with_it(store):
    first = start(store, "job-1", new_conversation=True)
    finish(store, first, "cancelled")
    assert store.discard_cancelled("job-1", USER) is True
    assert store.job("job-1", USER) is None
    assert store.conversation("conv-r", USER) is None


def case_a_discarded_follow_up_leaves_the_earlier_turns(store):
    first = start(store, "job-1", new_conversation=True)
    finish(store, first, "done")
    stopped = start(store, "job-2")
    finish(store, stopped, "cancelled")
    assert store.discard_cancelled("job-2", USER) is True
    assert job_ids(store) == ["job-1"]
    assert store.conversation("conv-r", USER) is not None


def case_only_a_newest_stopped_job_of_its_owner_is_discarded(store):
    answered = start(store, "job-1", new_conversation=True)
    finish(store, answered, "done")
    assert store.discard_cancelled("job-1", USER) is False
    stopped = start(store, "job-2")
    finish(store, stopped, "cancelled")
    assert store.discard_cancelled("job-2", "user-b") is False
    later = start(store, "job-3")
    assert store.discard_cancelled("job-3", USER) is False  # still running
    finish(store, later, "done")
    assert store.discard_cancelled("job-2", USER) is False  # no longer the newest
    assert store.discard_cancelled("job-missing", USER) is False
    assert job_ids(store) == ["job-1", "job-2", "job-3"]


CASES = [
    case_a_stopped_newest_job_is_replaced_in_the_same_insert,
    case_the_replacement_still_sorts_after_everything_the_conversation_held,
    case_an_answered_job_cannot_be_replaced,
    case_a_failed_job_cannot_be_replaced,
    case_a_running_job_cannot_be_replaced,
    case_only_the_newest_job_can_be_replaced,
    case_a_job_of_another_conversation_or_user_cannot_be_named,
    case_a_new_conversation_has_nothing_to_replace,
    case_an_automatic_title_follows_the_replacement_when_nothing_else_is_left,
    case_a_title_the_user_typed_is_left_alone,
    case_the_title_stays_when_other_turns_remain,
    case_the_delete_and_the_insert_are_one_transaction,
    case_a_discarded_first_question_takes_its_conversation_with_it,
    case_a_discarded_follow_up_leaves_the_earlier_turns,
    case_only_a_newest_stopped_job_of_its_owner_is_discarded,
]
CASE_IDS = [case.__name__.removeprefix("case_") for case in CASES]
