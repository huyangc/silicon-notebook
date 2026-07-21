import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path):
    return SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path}/status.db",
        storage_dir=str(tmp_path / "storage"),
    ))


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
