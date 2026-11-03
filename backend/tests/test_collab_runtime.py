import threading
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.request_context import reset_request_user, set_request_user
from app.domain.collaboration import CollabMark, PulseActor
from app.models.identity import UserProfile
from app.services.collab_jobs import (
    current_job_actor,
    pulse_touch_for_job,
    pulse_touch_for_job_kinds,
    submission_context,
)
from app.services.collab_runtime import CollaborationRuntime, PoolWaitWindow
from app.services.notebook_activity_writer import NotebookActivityWriter, _drain_activity_writers_for_tests


@pytest.mark.parametrize("user", ["legacy-user", SimpleNamespace(id="id-only"), SimpleNamespace(id="named", username="login")])
def test_actor_capture_is_optional_and_preserves_the_original_request_context(user):
    from app.core.request_context import _REQUEST_USER, get_request_user

    token = _REQUEST_USER.set(user)
    try:
        context = submission_context()
        assert context.run(get_request_user) is user
        captured = context.run(current_job_actor)
        if isinstance(user, str):
            assert captured is None
        else:
            assert captured.user_id == user.id
            assert captured.display_name == getattr(user, "username", "")
    finally:
        _REQUEST_USER.reset(token)


def test_unreadable_optional_actor_metadata_never_blocks_context_copy():
    from app.core.request_context import _REQUEST_USER, get_request_user

    class LegacyContext:
        id = "actor"

        @property
        def display_name(self):
            raise RuntimeError("optional metadata unavailable")

    user = LegacyContext()
    token = _REQUEST_USER.set(user)
    try:
        context = submission_context()
        assert context.run(get_request_user) is user
        assert context.run(current_job_actor) is None
    finally:
        _REQUEST_USER.reset(token)


def test_pool_wait_window_is_bounded_recent_and_recovers_after_quiet_gap():
    clock = [0.0]
    stats = {"requests_num": 0, "requests_wait_ms": 0}
    window = PoolWaitWindow(lambda: dict(stats), clock=lambda: clock[0])
    assert window.wait_ms() == 0
    for second in range(1, 25):
        clock[0] = second
        stats["requests_num"] += 1
        stats["requests_wait_ms"] += 200 if second <= 3 else 0
        value = window.wait_ms()
        for _ in range(50):
            window.wait_ms()
        assert len(window._samples) <= 12
    assert value == 0
    clock[0] += 100
    stats["requests_wait_ms"] += 100000
    assert window.wait_ms() == 0
    stats.update(requests_num=0, requests_wait_ms=0)
    assert window.wait_ms() == 0


def test_activity_queue_is_bounded_and_submit_returns_while_insert_is_blocked(caplog):
    entered, release = threading.Event(), threading.Event()
    inserted = []

    def insert(row):
        entered.set()
        assert release.wait(5)
        inserted.append(row)

    writer = NotebookActivityWriter(SimpleNamespace(insert_activity=insert), 1)
    try:
        assert writer.submit({"id": "one"})
        assert entered.wait(5)
        assert not release.is_set()
        assert writer.submit({"id": "two"})
        assert not writer.submit({"id": "three"})
        assert "collaboration_activity_queue_full" in caplog.text
    finally:
        release.set()
        writer.close()
    assert [row["id"] for row in inserted] == ["one", "two"]
    assert not writer.submit({"id": "after-close"})


def test_shared_teardown_reaps_idle_threads_and_runtime_can_submit_again():
    inserted = []
    writer = NotebookActivityWriter(SimpleNamespace(insert_activity=inserted.append), 2)
    try:
        writer.submit({"id": "one"})
        _drain_activity_writers_for_tests()
        assert writer._thread is None
        writer.submit({"id": "two"})
    finally:
        writer.close()
    assert len(inserted) == 2


def test_activity_failure_logs_only_code_and_preserves_worker(caplog):
    attempts = []

    def insert(row):
        attempts.append(row)
        raise RuntimeError("secret-name-source")

    writer = NotebookActivityWriter(SimpleNamespace(insert_activity=insert), 2)
    writer.submit({"secret": "secret-title"})
    writer.close()
    assert len(attempts) == 1
    assert "collaboration_activity_insert_failed" in caplog.text
    assert "secret" not in caplog.text


def test_actor_is_frozen_at_submit_and_terminal_failure_is_isolated():
    user = UserProfile(id="actor", email="", display_name="提交时", username="login", role="user")
    token = set_request_user(user)
    try:
        context = submission_context()
        user.display_name = "后来"
        assert context.run(current_job_actor).display_name == "提交时"
    finally:
        reset_request_user(token)
    assert current_job_actor() is None
    pulse_touch_for_job(lambda *args: (_ for _ in ()).throw(RuntimeError("private")), "nb", "sources")


def test_runtime_owner_zero_inserts_nonowner_one_and_actor_name_read_only_on_recovery():
    inserted, name_reads = [], []
    actors = SimpleNamespace(find_user_by_id=lambda id_: name_reads.append(id_) or {"display_name": "显示名", "username": "login"})
    store = SimpleNamespace(insert_activity=inserted.append)
    runtime = CollaborationRuntime(Settings(), store, actors, SimpleNamespace(pool_wait_stats=lambda: {}))
    try:
        owner = PulseActor("owner", "主人", "owner")
        runtime.record(CollabMark("guard", "nb", "sources:write", "owner", owner), "sources", "POST /template")
        editor = PulseActor("editor", "同事", "editor")
        runtime.record(CollabMark("guard", "nb", "sources:write", "owner", editor), "sources", "POST /template")
        runtime.touch_for_job("nb", "kg", "editor", "提交时")
        assert name_reads == []
        runtime.touch_for_job("nb", "catalog", "restored", None)
        assert name_reads == ["restored"]
        runtime.touch_for_job("nb", ("kg", "knowledge", "notebook"), "restored-batch", None)
        assert name_reads == ["restored", "restored-batch"]
        result = runtime.pulse.poll("nb", owner, since=runtime.pulse.cursor().rsplit(":", 1)[0] + ":0", visible=True)
        assert next(item for item in result["changes"] if item["kind"] == "catalog")["actor_name"] == "显示名"
    finally:
        runtime.close()
    assert len(inserted) == 1
    assert inserted[0]["actor_id"] == "editor"


def test_job_batches_have_one_primary_history_and_hint_only_cleanup_has_none():
    inserted, reads = [], []
    runtime = CollaborationRuntime(Settings(), SimpleNamespace(insert_activity=inserted.append),
                                   SimpleNamespace(find_user_by_id=lambda id_: reads.append(id_) or {"display_name": "恢复姓名"}),
                                   SimpleNamespace(pool_wait_stats=lambda: {}))
    kinds = ("sources", "knowledge", "kg", "notebook")
    try:
        # A restored durable batch recovers the actor once for all hints.
        pulse_touch_for_job_kinds(lambda nb, kinds, *args: runtime.touch_for_job(nb, kinds, "owner"), "nb", kinds)
        assert reads == ["owner"]
        history = runtime.pulse.recent_owner_changes("nb", "owner")
        assert [row["kind"] for row in history] == ["sources"]
        cursor = runtime.pulse.cursor()
        runtime.touch_for_job("nb", ("knowhow", "knowledge", "kg", "notebook"), "owner", "恢复姓名", activity=False)
        assert runtime.pulse.recent_owner_changes("nb", "owner") == history
        changes = runtime.pulse.poll("nb", PulseActor("peer", "peer", "reader"), since=cursor, visible=True)["changes"]
        assert {change["kind"] for change in changes} == {"knowhow", "knowledge", "kg", "notebook"}
        assert reads == ["owner"]
    finally:
        runtime.close()
    assert inserted == []
