from __future__ import annotations

import pytest

from app.core import ask_context
from app.core.config import Settings
from app.services.model_config import ModelNotConfiguredError
from app.services.model_config import model_config_fingerprint
from app.services.model_status import ModelStatusService
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

    from app.api import system_routes

    assert "repo.settings" not in inspect.getsource(system_routes.test_model_service)


def test_provider_records_existing_model_error_shape(repo, monkeypatch):
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    token = ask_context._ASK_MODEL_ERRORS.set([])
    try:
        unsafe_model = "https://10.0.0.8/v1?api_key=sk-private-secret"
        repo._note_model_error(
            "rerank",
            unsafe_model,
            RuntimeError("provider https://10.0.0.8 leaked sk-private-secret"),
            provider_failure=True,
        )
        assert ask_context._ASK_MODEL_ERRORS.get() == [
            {
                "service": "rerank",
                "stage": "rerank",
                "model": "",
                "message": "upstream_error",
            }
        ]
        assert events == [
            {
                "kind": "model_error",
                "stage": "rerank",
                "model": unsafe_model,
                "error": (
                    "RuntimeError: provider https://10.0.0.8 leaked "
                    "sk-private-secret"
                ),
                "status": "error",
            }
        ]
    finally:
        ask_context._ASK_MODEL_ERRORS.reset(token)


def test_provider_classifies_missing_configuration_with_stable_code(repo, monkeypatch):
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    token = ask_context._ASK_MODEL_ERRORS.set([])
    try:
        repo._note_model_error(
            "answer", "", ModelNotConfiguredError("private configuration detail"),
            provider_failure=True,
        )

        assert ask_context._ASK_MODEL_ERRORS.get() == [{
            "service": "llm",
            "stage": "answer",
            "model": "",
            "message": "missing_config",
        }]
        assert events[0]["error"] == (
            "ModelNotConfiguredError: private configuration detail"
        )
    finally:
        ask_context._ASK_MODEL_ERRORS.reset(token)


@pytest.mark.parametrize(
    "stage",
    [
        "chunk_fts",
        "chunk_ann_query",
        "scale_ann_open_chunk",
        "tier2_bridge_ann_query",
    ],
)
def test_local_retrieval_and_index_diagnostics_never_become_model_outages(
    repo, monkeypatch, stage
):
    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    token = ask_context._ASK_MODEL_ERRORS.set([])
    try:
        repo._runtime.models.note_model_error(
            stage,
            "embedding-runtime",
            RuntimeError("local index is stale"),
            service="embedding",
            provider_failure=False,
        )

        assert ask_context._ASK_MODEL_ERRORS.get() == []
        assert repo._runtime.identity.get_model_service_statuses("user-local") == {}
        assert events[0]["stage"] == stage
    finally:
        ask_context._ASK_MODEL_ERRORS.reset(token)


@pytest.mark.parametrize(
    ("stage", "service"),
    [
        ("answer", "llm"),
        ("rewrite", "rewrite_llm"),
        ("rerank", "rerank"),
        ("embed", "embedding"),
    ],
)
def test_explicit_provider_failures_update_only_the_exact_service(
    repo, stage, service
):
    repo.settings.openai_compat_base_url = "https://llm.example/v1"
    repo.settings.openai_compat_api_key = "llm-secret"
    repo.settings.openai_compat_model = "llm-runtime"
    repo.settings.rerank_base_url = "https://rerank.example/v1"
    repo.settings.rerank_api_key = "rerank-secret"
    repo.settings.rerank_model = "rerank-runtime"
    repo.settings.embed_provider = "dashscope"
    repo.settings.embed_base_url = "https://embed.example/v1"
    repo.settings.embed_api_key = "embed-secret"
    repo.settings.embed_model = "embed-runtime"
    token = ask_context._ASK_MODEL_ERRORS.set([])
    try:
        config = repo.resolve_model_config(repo.current_user(), service)
        repo._runtime.models.note_model_error(
            stage,
            f"{service}-runtime",
            RuntimeError("provider failed"),
            service=service,
            provider_failure=True,
            failed_fingerprint=model_config_fingerprint(config),
        )

        rows = repo._runtime.identity.get_model_service_statuses("user-local")
        assert set(rows) == {service}
        assert rows[service]["status"] == "error"
        assert ask_context._ASK_MODEL_ERRORS.get()[0]["service"] == service
    finally:
        ask_context._ASK_MODEL_ERRORS.reset(token)


def test_old_real_client_failure_keeps_old_ask_name_but_not_new_status(repo):
    old_settings = {
        "llm": {
            "base_url": "https://old.example/v1",
            "api_key": "old-secret",
            "model": "old-runtime-model",
        }
    }
    repo.set_user_model_settings("user-local", old_settings)
    old_config = repo.resolve_model_config(repo.current_user(), "llm")
    old_client = repo.llm_client
    failed_fingerprint = getattr(old_client, "_model_status_fingerprint", "")
    assert failed_fingerprint == model_config_fingerprint(old_config)

    repo.set_user_model_settings("user-local", {
        "llm": {
            "base_url": "https://new.example/v1",
            "api_key": "new-secret",
            "model": "new-runtime-model",
        }
    })
    ModelStatusService(
        repo._runtime.identity, repo.settings, probe=lambda _config: None
    ).test_one(repo.current_user(), "llm")
    token = ask_context._ASK_MODEL_ERRORS.set([])
    try:
        repo._runtime.models.note_model_error(
            "answer",
            old_client.model,
            RuntimeError("old request failed after save"),
            service="llm",
            provider_failure=True,
            failed_fingerprint=failed_fingerprint,
        )

        assert ask_context._ASK_MODEL_ERRORS.get()[0]["model"] == "old-runtime-model"
        stored = repo._runtime.identity.get_model_service_statuses("user-local")["llm"]
        assert stored["status"] == "ok"
        assert stored["config_fingerprint"] == model_config_fingerprint(
            repo.resolve_model_config(repo.current_user(), "llm")
        )
    finally:
        ask_context._ASK_MODEL_ERRORS.reset(token)


def test_runtime_client_resolution_observes_another_repository_commit(repo):
    writer = SQLiteRepository(repo.settings)
    writer._migrate()
    writer._seed()
    repo.set_user_model_settings("user-local", {
        "llm": {
            "base_url": "https://old.example/v1",
            "api_key": "old-secret",
            "model": "old-runtime-model",
        }
    })
    old_client = repo.llm_client
    assert old_client.model == "old-runtime-model"

    writer._runtime.identity.patch_user_model_settings_atomic(
        "user-local", {"llm": {"model": "new-runtime-model"}}
    )

    new_client = repo.llm_client
    assert new_client.model == "new-runtime-model"
    assert new_client is not old_client


def test_provider_failure_from_unstamped_test_double_is_not_persisted(repo):
    token = ask_context._ASK_MODEL_ERRORS.set([])
    try:
        repo._runtime.models.note_model_error(
            "answer",
            "fake-runtime-model",
            RuntimeError("fake provider failed"),
            service="llm",
            provider_failure=True,
            failed_fingerprint=getattr(object(), "_model_status_fingerprint", ""),
        )

        assert ask_context._ASK_MODEL_ERRORS.get()[0]["model"] == "fake-runtime-model"
        assert repo._runtime.identity.get_model_service_statuses("user-local") == {}
    finally:
        ask_context._ASK_MODEL_ERRORS.reset(token)
