from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.request_context import reset_request_user, set_request_user
from app.repositories.sqlite.identity_store import IdentityStore
from app.services.sqlite_identity import SQLiteIdentityMixin
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch) -> SQLiteRepository:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'identity.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def test_identity_and_query_stores_share_runtime_database(repo):
    assert repo._runtime.identity.database is repo._runtime.database
    assert repo._runtime.queries.database is repo._runtime.database


def test_facade_identity_delegates_to_runtime(repo, monkeypatch):
    expected = repo.current_user()
    monkeypatch.setattr(repo._runtime.identity, "current_user", lambda: expected)
    assert repo.current_user() is expected


def test_identity_store_owns_identity_persistence_implementation():
    expected = {
        "current_user",
        "get_user_model_settings",
        "set_user_model_settings",
        "get_model_service_statuses",
        "record_model_service_status",
        "clear_model_service_statuses",
        "resolve_model_config",
        "create_user",
        "authenticate_user",
        "create_session",
        "resolve_session",
        "delete_session",
    }
    assert expected <= IdentityStore.__dict__.keys()
    assert "__getattr__" not in IdentityStore.__dict__


def test_repository_no_longer_inherits_identity_mixin():
    assert not issubclass(SQLiteRepository, SQLiteIdentityMixin)


def test_current_user_reads_request_context_at_call_time(repo):
    expected = repo.current_user().model_copy(update={"id": "user-context"})
    token = set_request_user(expected)
    try:
        assert repo._runtime.identity.current_user() is expected
    finally:
        reset_request_user(token)
