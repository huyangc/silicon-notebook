import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.config import Settings
from app.services.model_config import (
    STATUS_SERVICE_ROLES,
    model_config_fingerprint,
    resolve_effective_config,
    system_model_settings,
)
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path):
    return SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path}/status.db",
        storage_dir=str(tmp_path / "storage"),
    ))


_OLD_LLM = {
    "base_url": "https://old.example/v1",
    "api_key": "old-secret",
    "model": "old-model",
}
_NEW_LLM = {
    "base_url": "https://new.example/v1",
    "api_key": "new-secret",
    "model": "new-model",
}


def _llm_fingerprint(store, model_settings):
    return model_config_fingerprint(resolve_effective_config(
        model_settings,
        "llm",
        store.settings.user_model_config_policy,
        system_model_settings(store.settings),
    ))


def _hold_transaction_after_begin(monkeypatch, database):
    locked = threading.Event()
    release = threading.Event()
    original = database.begin_immediate

    def held_begin(connection):
        original(connection)
        locked.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(database, "begin_immediate", held_begin)
    return locked, release


def _announce_transaction_attempt(monkeypatch, database):
    attempted = threading.Event()
    original = database.begin_immediate

    def announced_begin(connection):
        attempted.set()
        original(connection)

    monkeypatch.setattr(database, "begin_immediate", announced_begin)
    return attempted


def _conditional_status(store, fingerprint, trigger, status="ok"):
    return store.record_model_service_status_if_current(
        "user-local",
        "llm",
        fingerprint,
        status,
        12 if status == "ok" else 0,
        "" if status == "ok" else "upstream_error",
        trigger,
        "2030-01-01T00:00:00+00:00",
    )


def test_migration_23_creates_cascading_latest_status_table(tmp_path):
    repo = _repo(tmp_path)
    with repo._connect() as db:
        columns = {row["name"] for row in db.execute(
            "PRAGMA table_info(model_service_status)"
        )}
        assert columns == {
            "user_id", "service", "config_fingerprint", "status",
            "latency_ms", "code", "trigger", "checked_at",
        }


def test_status_store_upserts_and_clears_by_service(tmp_path):
    store = _repo(tmp_path)._runtime.identity
    store.record_model_service_status(
        "user-local", "llm", "fp-1", "error", 121,
        "upstream_error", "manual_test", "2030-01-01T00:00:00+00:00",
    )
    store.record_model_service_status(
        "user-local", "llm", "fp-1", "ok", 44,
        "", "manual_test", "2030-01-01T00:01:00+00:00",
    )
    assert store.get_model_service_statuses("user-local")["llm"] == {
        "config_fingerprint": "fp-1",
        "status": "ok",
        "latency_ms": 44,
        "code": "",
        "trigger": "manual_test",
        "checked_at": "2030-01-01T00:01:00.000000+00:00",
    }
    store.clear_model_service_statuses("user-local", ["llm"])
    assert store.get_model_service_statuses("user-local") == {}


def test_atomic_settings_patch_locks_before_read_and_invalidates_in_same_transaction(
    tmp_path, monkeypatch
):
    store = _repo(tmp_path)._runtime.identity
    settings = {
        role: {
            "base_url": f"https://{role}.example/v1",
            "api_key": f"{role}-secret",
            "model": f"{role}-model",
        }
        for role in ("llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank")
    }
    store.set_user_model_settings("user-local", settings)
    for service in STATUS_SERVICE_ROLES:
        store.record_model_service_status(
            "user-local", service, f"{service}-fingerprint", "ok", 10, "",
            "manual_test", "2030-01-01T00:00:00+00:00",
        )

    statements = []
    original_new_connection = store.database._new_connection

    def traced_connection():
        connection = original_new_connection()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store.database, "_new_connection", traced_connection)

    store.patch_user_model_settings_atomic(
        "user-local", {"rerank": {"model": "new-rerank-model"}}
    )

    sql = [statement.upper() for statement in statements]
    begin = next(i for i, statement in enumerate(sql) if "BEGIN IMMEDIATE" in statement)
    read = next(i for i, statement in enumerate(sql) if "SELECT MODEL_SETTINGS" in statement)
    update = next(i for i, statement in enumerate(sql) if "UPDATE USER_PROFILES" in statement)
    delete = next(i for i, statement in enumerate(sql) if "DELETE FROM MODEL_SERVICE_STATUS" in statement)
    assert begin < read < update < delete
    assert set(store.get_model_service_statuses("user-local")) == (
        set(STATUS_SERVICE_ROLES) - {"rerank"}
    )


def test_new_manual_status_waiting_behind_settings_commit_survives(
    tmp_path, monkeypatch
):
    settings_store = _repo(tmp_path)._runtime.identity
    status_store = _repo(tmp_path)._runtime.identity
    settings_store.set_user_model_settings("user-local", {"llm": _OLD_LLM})
    assert status_store.get_user_model_settings("user-local") == {"llm": _OLD_LLM}
    new_fingerprint = _llm_fingerprint(settings_store, {"llm": _NEW_LLM})
    locked, release = _hold_transaction_after_begin(
        monkeypatch, settings_store.database
    )
    attempted = _announce_transaction_attempt(monkeypatch, status_store.database)

    with ThreadPoolExecutor(max_workers=2) as executor:
        save = executor.submit(
            settings_store.patch_user_model_settings_atomic,
            "user-local",
            {"llm": _NEW_LLM},
        )
        assert locked.wait(timeout=5)
        record = executor.submit(
            _conditional_status,
            status_store,
            new_fingerprint,
            "manual_test",
        )
        assert attempted.wait(timeout=5)
        release.set()
        save.result(timeout=10)
        assert record.result(timeout=10) is True

    row = settings_store.get_model_service_statuses("user-local")["llm"]
    assert row["config_fingerprint"] == new_fingerprint
    assert (row["status"], row["trigger"]) == ("ok", "manual_test")


def test_old_observed_status_waiting_behind_settings_commit_is_rejected(
    tmp_path, monkeypatch
):
    settings_store = _repo(tmp_path)._runtime.identity
    status_store = _repo(tmp_path)._runtime.identity
    settings_store.set_user_model_settings("user-local", {"llm": _OLD_LLM})
    assert status_store.get_user_model_settings("user-local") == {"llm": _OLD_LLM}
    old_fingerprint = _llm_fingerprint(settings_store, {"llm": _OLD_LLM})
    locked, release = _hold_transaction_after_begin(
        monkeypatch, settings_store.database
    )
    attempted = _announce_transaction_attempt(monkeypatch, status_store.database)

    with ThreadPoolExecutor(max_workers=2) as executor:
        save = executor.submit(
            settings_store.patch_user_model_settings_atomic,
            "user-local",
            {"llm": _NEW_LLM},
        )
        assert locked.wait(timeout=5)
        record = executor.submit(
            _conditional_status,
            status_store,
            old_fingerprint,
            "observed_failure",
            "error",
        )
        assert attempted.wait(timeout=5)
        release.set()
        save.result(timeout=10)
        assert record.result(timeout=10) is False

    assert "llm" not in settings_store.get_model_service_statuses("user-local")


def test_old_manual_status_committed_first_is_deleted_by_settings_transaction(
    tmp_path, monkeypatch
):
    status_store = _repo(tmp_path)._runtime.identity
    settings_store = _repo(tmp_path)._runtime.identity
    settings_store.set_user_model_settings("user-local", {"llm": _OLD_LLM})
    old_fingerprint = _llm_fingerprint(settings_store, {"llm": _OLD_LLM})
    locked, release = _hold_transaction_after_begin(monkeypatch, status_store.database)
    attempted = _announce_transaction_attempt(monkeypatch, settings_store.database)

    with ThreadPoolExecutor(max_workers=2) as executor:
        record = executor.submit(
            _conditional_status,
            status_store,
            old_fingerprint,
            "manual_test",
        )
        assert locked.wait(timeout=5)
        save = executor.submit(
            settings_store.patch_user_model_settings_atomic,
            "user-local",
            {"llm": _NEW_LLM},
        )
        assert attempted.wait(timeout=5)
        release.set()
        assert record.result(timeout=10) is True
        save.result(timeout=10)

    assert "llm" not in settings_store.get_model_service_statuses("user-local")


def test_status_store_rejects_an_older_occurrence_written_later(tmp_path):
    store = _repo(tmp_path)._runtime.identity
    store.record_model_service_status(
        "user-local", "llm", "new-fp", "ok", 44, "", "manual_test",
        "2030-01-01T00:02:00+00:00",
    )
    store.record_model_service_status(
        "user-local", "llm", "old-fp", "error", 0, "upstream_error",
        "observed_failure", "2030-01-01T00:01:00+00:00",
    )

    assert store.get_model_service_statuses("user-local")["llm"] == {
        "config_fingerprint": "new-fp",
        "status": "ok",
        "latency_ms": 44,
        "code": "",
        "trigger": "manual_test",
        "checked_at": "2030-01-01T00:02:00.000000+00:00",
    }


@pytest.mark.parametrize("write_error_first", [False, True])
def test_equal_time_failure_wins_deterministically_over_manual_ok(
    tmp_path, write_error_first
):
    store = _repo(tmp_path)._runtime.identity
    ok = ("ok-fp", "ok", 21, "", "manual_test")
    failed = ("error-fp", "error", 0, "upstream_error", "observed_failure")
    values = (failed, ok) if write_error_first else (ok, failed)
    for fingerprint, status, latency, code, trigger in values:
        store.record_model_service_status(
            "user-local", "llm", fingerprint, status, latency, code, trigger,
            "2030-01-01T00:00:00+00:00",
        )

    row = store.get_model_service_statuses("user-local")["llm"]
    assert (row["config_fingerprint"], row["status"], row["trigger"]) == (
        "error-fp", "error", "observed_failure",
    )


def test_monotonic_ordering_is_independent_per_service(tmp_path):
    store = _repo(tmp_path)._runtime.identity
    store.record_model_service_status(
        "user-local", "llm", "llm-new", "ok", 11, "", "manual_test",
        "2030-01-01T00:02:00+00:00",
    )
    store.record_model_service_status(
        "user-local", "embedding", "embed-old", "error", 0,
        "upstream_error", "observed_failure", "2030-01-01T00:01:00+00:00",
    )

    rows = store.get_model_service_statuses("user-local")
    assert rows["llm"]["config_fingerprint"] == "llm-new"
    assert rows["embedding"]["config_fingerprint"] == "embed-old"
