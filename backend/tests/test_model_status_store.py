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
        "checked_at": "2030-01-01T00:01:00+00:00",
    }
    store.clear_model_service_statuses("user-local", ["llm"])
    assert store.get_model_service_statuses("user-local") == {}
