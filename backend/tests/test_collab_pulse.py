"""Process-local revision and privacy contracts; no services, ports or timers."""
from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.services.collab_pulse import CollabPulse, PulseActor

OWNER = PulseActor("owner", "主人", "owner")
EDITOR = PulseActor("editor", "编辑者", "editor")
READER = PulseActor("reader", "读者", "reader")


def pulse(**settings):
    clock = [0.0]
    service = CollabPulse(Settings(**settings), clock=lambda: clock[0],
                          wall_clock=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc), epoch="epoch")
    return service, clock


def poll(service, since, actor=OWNER, **kwargs):
    return service.poll("nb", actor, since=since, visible=True, **kwargs)


@pytest.mark.parametrize("since", ["", "invalid", "epoch:-1", "wrong:0", "epoch:1", "epoch:０"])
def test_invalid_epoch_or_future_cursor_resets(since):
    service, _ = pulse()
    assert poll(service, since)["reset"]


def test_detail_cursor_before_first_touch_delivers_change_without_reset():
    service, _ = pulse()
    cursor = service.cursor()
    service.touch("nb", "sources", EDITOR)
    result = poll(service, cursor)
    assert not result["reset"]
    assert result["changes"][0]["kind"] == "sources"


def test_eviction_without_later_touch_resets_hidden_client():
    service, clock = pulse(collab_pulse_max_notebooks=1)
    cursor = service.cursor()
    service.touch("nb", "sources", EDITOR)
    clock[0] += 2
    service.touch("other", "kg", EDITOR)
    assert poll(service, cursor)["reset"]


def test_eviction_and_recreation_resets_but_surviving_old_entry_does_not():
    service, clock = pulse(collab_pulse_max_notebooks=2)
    cursor = service.cursor()
    service.touch("nb", "sources", EDITOR)
    service.touch("discard", "kg", EDITOR)
    # Polling moves nb to the LRU tail without changing the global revision.
    poll(service, cursor)
    clock[0] += 2
    service.touch("new", "kg", EDITOR)
    assert not poll(service, cursor)["reset"]
    service.touch("discard", "kg", EDITOR)
    service.touch("new", "sources", EDITOR)
    service.touch("nb", "kg", EDITOR)
    assert poll(service, cursor)["reset"]


def test_ring_overflow_and_boundary_watermark():
    service, clock = pulse(collab_pulse_ring_size=2)
    cursor = service.cursor()
    service.touch("nb", "sources", EDITOR)
    first = service.cursor()
    clock[0] += 2
    service.touch("nb", "kg", EDITOR)
    clock[0] += 2
    service.touch("nb", "catalog", EDITOR)
    assert poll(service, cursor)["reset"]
    assert not poll(service, first)["reset"]


def test_trailing_coalesce_new_revision_preserves_peer_then_self():
    service, clock = pulse(collab_pulse_ring_size=1)
    before = service.cursor()
    service.touch("nb", "sources", EDITOR)
    middle = service.cursor()
    clock[0] += .5
    service.touch("nb", "sources", OWNER)
    assert service.cursor() != middle
    assert not poll(service, before)["reset"]
    result = poll(service, middle)
    assert [change["kind"] for change in result["changes"]] == ["sources"]


def test_all_self_updates_are_omitted_while_cursor_advances():
    service, clock = pulse()
    before = service.cursor()
    service.touch("nb", "kg", OWNER)
    clock[0] += 2
    service.touch("nb", "kg", OWNER)
    result = poll(service, before)
    assert result["changes"] == []
    assert result["cursor"] != before


def test_latest_actor_and_multiple_records_of_one_kind_are_combined():
    service, clock = pulse()
    cursor = service.cursor()
    service.touch("nb", "kg", EDITOR)
    clock[0] += 2
    service.touch("nb", "kg", OWNER)
    result = poll(service, cursor)
    assert len(result["changes"]) == 1
    assert result["changes"][0]["actor_name"] == OWNER.display_name


def test_presence_hides_reader_identity_and_invisible_reader_leaves_immediately():
    service, _ = pulse()
    cursor = service.cursor()
    poll(service, cursor, READER)
    poll(service, cursor, EDITOR)
    result = poll(service, cursor)
    assert result["other_reader_count"] == 1
    assert {viewer["user_id"] for viewer in result["viewers"]} == {OWNER.user_id, EDITOR.user_id}
    assert poll(service, cursor, READER)["other_reader_count"] == 0
    service.poll("nb", READER, since=cursor, visible=False)
    assert poll(service, cursor)["other_reader_count"] == 0


def test_interval_and_presence_ttl_scale_together_then_recover():
    service, clock = pulse()
    cursor = service.cursor()
    assert poll(service, cursor, READER, pool_wait_ms=200)["next_interval_s"] == 60
    clock[0] = 100
    assert poll(service, cursor)["other_reader_count"] == 1
    assert poll(service, cursor, pool_wait_ms=50)["next_interval_s"] == 30
    clock[0] = 181
    assert poll(service, cursor)["other_reader_count"] == 0
    assert poll(service, cursor)["next_interval_s"] == 15


def test_none_does_not_allocate_or_advance_and_unknown_kind_fails():
    service, _ = pulse()
    before = service.cursor()
    service.touch("nb", "none", OWNER)
    assert service.cursor() == before
    with pytest.raises(ValueError):
        service.touch("nb", "secret", OWNER)


def test_recent_owner_activity_uses_same_public_shape_and_excludes_peers():
    service, _ = pulse()
    assert service.recent_owner_changes("missing", OWNER.user_id) == []
    service.touch("nb", "knowhow", OWNER, capability="knowhow:write", target="PATCH /template")
    service.touch("nb", "kg", EDITOR)
    records = service.recent_owner_changes("nb", OWNER.user_id)
    assert len(records) == 1
    record = records[0]
    assert set(record) == {"id", "kind", "capability", "actor_id", "actor_name", "target", "created_at"}
    assert record["id"] == "epoch:1"
    assert (record["kind"], record["actor_id"], record["actor_name"]) == ("knowhow", OWNER.user_id, OWNER.display_name)
    assert record["capability"] == "knowhow:write"
    assert record["target"] == "PATCH /template"


def test_primary_history_survives_derived_hint_coalescing_and_is_bounded_with_its_notebook():
    service, _ = pulse(collab_pulse_ring_size=1, notebook_activity_panel_limit=2, collab_pulse_max_notebooks=1)
    kinds = ("sources", "knowledge", "kg", "notebook")
    service.touch("nb", "sources", OWNER, affected_kinds=kinds, target="DELETE /source")
    original = service.recent_owner_changes("nb", OWNER.user_id)
    assert len(original) == 1 and original[0]["kind"] == "sources"
    # A peer can coalesce the same hint and a conservative invalidation can
    # overflow the hint ring; neither is a new owner action.
    service.touch("nb", "notebook", EDITOR, activity=False)
    service.touch("nb", "knowhow", OWNER, affected_kinds=("knowhow", "kg"), activity=False)
    assert service.recent_owner_changes("nb", OWNER.user_id) == original
    for target in ("second", "third"):
        service.touch("nb", "sources", OWNER, affected_kinds=kinds, target=target)
    recent = service.recent_owner_changes("nb", OWNER.user_id)
    assert [record["target"] for record in recent] == ["third", "second"]
    # Returned projections cannot mutate the state held under the lock.
    recent[0]["target"] = "external mutation"
    assert service.recent_owner_changes("nb", OWNER.user_id)[0]["target"] == "third"
    service.touch("other", "kg", OWNER)
    assert service.recent_owner_changes("nb", OWNER.user_id) == []


def test_concurrent_batches_keep_one_unique_primary_each_and_respect_the_panel_capacity():
    from concurrent.futures import ThreadPoolExecutor

    service, _ = pulse(notebook_activity_panel_limit=7, collab_pulse_ring_size=1)

    def action(index):
        service.touch("nb", "sources", OWNER, affected_kinds=("sources", "knowledge", "kg", "notebook"), target=str(index))

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(action, range(100)))
    recent = service.recent_owner_changes("nb", OWNER.user_id)
    assert len(recent) == len({row["id"] for row in recent}) == 7
    assert {row["kind"] for row in recent} == {"sources"}
    assert service.cursor() == "epoch:400"


@pytest.mark.parametrize("settings", [
    {"collab_pulse_slow_intervals": "30"}, {"collab_pulse_slow_intervals": "10,60"},
    {"collab_pulse_slow_intervals": "60,30"}, {"collab_pulse_slow_intervals": "bad,60"},
    {"collab_pulse_pool_wait_slow_ms": 300},
])
def test_invalid_deployment_intervals_refused(settings):
    with pytest.raises(ValueError):
        Settings(**settings)
