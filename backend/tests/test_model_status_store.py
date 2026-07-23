from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _store(tmp_path):
    repo = SQLiteRepository(Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/status.db",
        storage_dir=str(tmp_path / "storage"),
    ))
    return repo._runtime.model_status_store


def _record(
    store,
    *,
    service_id="chat_primary",
    fingerprint="fp-1",
    status="ok",
    latency_ms=12,
    code="",
    trigger="manual_test",
    checked_at="2030-01-01T00:00:00+00:00",
    support_id="support-1",
):
    store.record(
        service_id=service_id,
        config_fingerprint=fingerprint,
        status=status,
        latency_ms=latency_ms,
        code=code,
        trigger=trigger,
        support_id=support_id,
        checked_at=checked_at,
    )


def test_system_status_store_is_keyed_only_by_service(tmp_path):
    store = _store(tmp_path)
    _record(store, fingerprint="fp-old", support_id="support-old")
    _record(
        store,
        fingerprint="fp-new",
        status="error",
        code="provider_unavailable",
        trigger="observed_failure",
        support_id="support-new",
        checked_at="2030-01-01T00:01:00+00:00",
    )

    assert store.get_all() == {
        "chat_primary": {
            "config_fingerprint": "fp-new",
            "status": "error",
            "latency_ms": 12,
            "code": "provider_unavailable",
            "trigger": "observed_failure",
            "support_id": "support-new",
            "checked_at": "2030-01-01T00:01:00.000000+00:00",
        }
    }


def test_system_status_store_rejects_naive_timestamp(tmp_path):
    with pytest.raises(ValueError, match="offset-aware"):
        _record(_store(tmp_path), checked_at="2030-01-01T00:00:00")


def test_system_status_store_normalizes_timestamp_to_utc(tmp_path):
    store = _store(tmp_path)
    _record(store, checked_at="2030-01-01T08:00:00+08:00")
    assert store.get_all()["chat_primary"]["checked_at"] == (
        "2030-01-01T00:00:00.000000+00:00"
    )


def test_system_status_store_rejects_older_occurrence_written_later(tmp_path):
    store = _store(tmp_path)
    _record(
        store,
        fingerprint="fp-new",
        checked_at="2030-01-01T00:02:00+00:00",
    )
    _record(
        store,
        fingerprint="fp-old",
        status="error",
        code="provider_unavailable",
        trigger="observed_failure",
        checked_at="2030-01-01T00:01:00+00:00",
    )
    assert store.get_all()["chat_primary"]["config_fingerprint"] == "fp-new"


@pytest.mark.parametrize("failure_first", [False, True])
def test_equal_time_observed_failure_wins_over_success(tmp_path, failure_first):
    store = _store(tmp_path)
    ok = dict(
        fingerprint="fp-ok", status="ok", trigger="recovery_probe", support_id="ok"
    )
    failure = dict(
        fingerprint="fp-error",
        status="error",
        code="provider_unavailable",
        trigger="observed_failure",
        support_id="error",
    )
    for values in ((failure, ok) if failure_first else (ok, failure)):
        _record(store, **values)
    row = store.get_all()["chat_primary"]
    assert (row["config_fingerprint"], row["status"], row["trigger"]) == (
        "fp-error",
        "error",
        "observed_failure",
    )


def test_monotonic_ordering_is_independent_per_service(tmp_path):
    store = _store(tmp_path)
    _record(store, service_id="chat", fingerprint="chat-new")
    _record(
        store,
        service_id="embed",
        fingerprint="embed-old",
        status="error",
        code="provider_unavailable",
        trigger="observed_failure",
    )
    assert {
        key: value["config_fingerprint"] for key, value in store.get_all().items()
    } == {"chat": "chat-new", "embed": "embed-old"}
