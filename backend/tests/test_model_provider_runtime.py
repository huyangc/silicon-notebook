from __future__ import annotations

import pytest

from app.core import ask_context
from app.core.config import Settings
from app.services.model_provider import RuntimeModelProvider
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch) -> SQLiteRepository:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'provider.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def test_runtime_owns_model_provider_with_shared_identity_cache(repo):
    provider = repo._runtime.models
    assert isinstance(provider, RuntimeModelProvider)
    assert provider.identity is repo._runtime.identity
    assert provider.model_config_cache is repo._runtime.identity.model_config_cache


def test_facade_model_properties_delegate_and_preserve_mutable_setters(repo):
    marker = object()
    repo.llm_client = marker
    assert repo.llm_client is marker
    assert repo._runtime.models.llm_client is marker

    reranker = object()
    repo.rerank_client = reranker
    assert repo.rerank_client is reranker
    assert repo._runtime.models.rerank_client is reranker


def test_legacy_cache_replacement_updates_every_runtime_owner(repo):
    model_settings = {"user-x": {"llm": {"model": "replacement"}}}
    repo._user_model_cfg_cache = model_settings
    assert repo._user_model_cfg_cache is model_settings
    assert repo._runtime.model_config_cache is model_settings
    assert repo._runtime.identity.model_config_cache is model_settings
    assert repo._runtime.models.model_config_cache is model_settings

    llm_clients = {"fingerprint": object()}
    repo._user_llm_clients = llm_clients
    assert repo._user_llm_clients is llm_clients
    assert repo._runtime.models._user_llm_clients is llm_clients

    rerank_clients = {"fingerprint": object()}
    repo._user_rerank_clients = rerank_clients
    assert repo._user_rerank_clients is rerank_clients
    assert repo._runtime.models._user_rerank_clients is rerank_clients


def test_identity_routes_do_not_require_settings_outside_identity_protocol():
    import inspect

    from app.api import routes

    assert "repo.settings" not in inspect.getsource(routes.test_model_service)


def test_ask_contextvars_keep_backwards_compatible_object_identity():
    from app.services import sqlite_repository

    assert sqlite_repository._ASK_MODEL_ERRORS is ask_context._ASK_MODEL_ERRORS
    assert sqlite_repository._ASK_EMBED_CACHE is ask_context._ASK_EMBED_CACHE


def test_provider_records_existing_model_error_shape(repo, monkeypatch):
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    token = ask_context._ASK_MODEL_ERRORS.set([])
    try:
        repo._note_model_error("rerank", "model-x", RuntimeError("boom"))
        assert ask_context._ASK_MODEL_ERRORS.get() == [
            {
                "stage": "rerank",
                "model": "model-x",
                "message": "RuntimeError: boom",
            }
        ]
        assert events == [
            {
                "kind": "model_error",
                "stage": "rerank",
                "model": "model-x",
                "error": "RuntimeError: boom",
                "status": "error",
            }
        ]
    finally:
        ask_context._ASK_MODEL_ERRORS.reset(token)
