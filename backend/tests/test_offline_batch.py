# backend/tests/test_offline_batch.py
"""offline_batch_scope 及其四个闸口(maybe_auto_index / maybe_enqueue_fold / should_extract_kg)。"""
from types import SimpleNamespace

from app.services.notebook_metadata import notebook_metadata_refresh_suppressed
from app.services.offline_batch import (
    OfflineBatchPolicy,
    current_offline_batch_policy,
    kg_extraction_deferred,
    offline_batch_active,
    offline_batch_scope,
)
from app.services.scale_artifact_runtime import ScaleArtifactRuntime
from app.services.source_ingestion import SourceIngestionService


def test_scope_sets_resets_nests_and_suppresses_metadata():
    assert current_offline_batch_policy() is None
    assert not offline_batch_active() and not kg_extraction_deferred()
    assert not notebook_metadata_refresh_suppressed()
    with offline_batch_scope(OfflineBatchPolicy()):
        assert offline_batch_active() and not kg_extraction_deferred()
        assert notebook_metadata_refresh_suppressed()
        with offline_batch_scope(OfflineBatchPolicy(defer_kg_extraction=True)):
            assert kg_extraction_deferred()
            assert notebook_metadata_refresh_suppressed()
        assert offline_batch_active() and not kg_extraction_deferred()
        assert notebook_metadata_refresh_suppressed()
    assert current_offline_batch_policy() is None
    assert not notebook_metadata_refresh_suppressed()


def test_scope_resets_when_the_body_raises():
    try:
        with offline_batch_scope(OfflineBatchPolicy(defer_kg_extraction=True)):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not offline_batch_active() and not notebook_metadata_refresh_suppressed()


def _auto_index_fake(calls):
    return SimpleNamespace(
        settings=SimpleNamespace(
            scale_index_auto_enabled=True, scale_index_auto_when="idle"
        ),
        auto_index_checked=set(),
        building=set(),
        idle_queue={},
        _scale_pending={},
        notebook_copy_stats=lambda nb: calls.append(("stats", nb)) or {"copyable": True},
        status=lambda nb: calls.append(("status", nb)) or {"state": "unindexed"},
        trigger=lambda nb, **kw: calls.append(("trigger", nb)),
        event_log=SimpleNamespace(logger=SimpleNamespace(exception=lambda *a: None)),
    )


def test_maybe_auto_index_is_a_noop_in_scope_and_leaves_checked_set_alone():
    calls = []
    fake = _auto_index_fake(calls)
    with offline_batch_scope(OfflineBatchPolicy()):
        ScaleArtifactRuntime.maybe_auto_index(fake, "nb")
    assert calls == [] and fake.auto_index_checked == set()
    # 同一个对象出了 scope 后恢复原行为:被评估并记入已检查集合。
    ScaleArtifactRuntime.maybe_auto_index(fake, "nb")
    assert calls == [("stats", "nb")] and fake.auto_index_checked == {"nb"}


def test_maybe_enqueue_fold_is_a_noop_in_scope_and_unchanged_outside():
    calls = []
    fake = SimpleNamespace(
        settings=SimpleNamespace(scale_auto_fold_on_add=True),
        load=lambda nb, allow_stale=False: calls.append(("load", nb)) or object(),
        trigger=lambda nb, **kw: calls.append(("trigger", nb, kw["mode"])),
        event_log=SimpleNamespace(logger=SimpleNamespace(exception=lambda *a: None)),
    )
    with offline_batch_scope(OfflineBatchPolicy(defer_kg_extraction=True)):
        ScaleArtifactRuntime.maybe_enqueue_fold(fake, "nb")
    assert calls == []
    ScaleArtifactRuntime.maybe_enqueue_fold(fake, "nb")
    assert calls == [("load", "nb"), ("trigger", "nb", "fold")]


def test_should_extract_kg_false_under_defer_policy_even_with_existing_kg():
    fake = SimpleNamespace(
        settings=SimpleNamespace(kg_auto_extract=True),
        notebook_has_kg=lambda nb: True,
    )
    assert SourceIngestionService.should_extract_kg(fake, "nb") is True
    with offline_batch_scope(OfflineBatchPolicy()):
        assert SourceIngestionService.should_extract_kg(fake, "nb") is True
    with offline_batch_scope(OfflineBatchPolicy(defer_kg_extraction=True)):
        assert SourceIngestionService.should_extract_kg(fake, "nb") is False
    fake.settings.kg_auto_extract = False
    assert SourceIngestionService.should_extract_kg(fake, "nb") is True
