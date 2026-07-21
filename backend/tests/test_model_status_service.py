import pytest

from app.core.config import Settings
from app.services.model_config import model_config_fingerprint
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/model-status.db",
        storage_dir=str(tmp_path / "storage"),
        openai_compat_base_url="https://primary.example/v1",
        openai_compat_api_key="primary-secret",
        openai_compat_model="primary-live-model",
        rerank_base_url="https://rerank.example/v1",
        rerank_api_key="rerank-secret",
        rerank_model="rerank-live-model",
        embed_provider="dashscope",
        embed_base_url="https://embed.example/v1",
        embed_api_key="embed-secret",
        embed_model="embed-live-model",
    )


@pytest.fixture
def identity(settings):
    return SQLiteRepository(settings)._runtime.identity


@pytest.fixture
def user(identity):
    return identity.current_user()


def test_snapshot_never_probes_and_invalidates_fingerprint(identity, user, settings):
    from app.services.model_status import ModelStatusService

    calls = []
    service = ModelStatusService(identity, settings, probe=lambda cfg: calls.append(cfg))
    config = identity.resolve_model_config(user, "llm")
    identity.record_model_service_status(
        user.id,
        "llm",
        "stale-fingerprint",
        "ok",
        17,
        "",
        "manual_test",
        "2030-01-01T00:00:00+00:00",
    )

    snapshot = service.snapshot(user)

    assert calls == []
    item = next(item for item in snapshot.services if item.service == "llm")
    assert item.status == "untested"
    assert item.code == ""
    assert item.latency_ms == 0
    assert item.model == config.model


def test_test_one_returns_dynamic_model_and_sanitized_failure(identity, user, settings):
    from app.services.model_status import ModelStatusService

    def fail(_config):
        raise RuntimeError("provider 10.0.0.8 rejected secret payload")

    service = ModelStatusService(identity, settings, probe=fail)
    item = service.test_one(user, "reasoning_llm")

    assert item.model == settings.openai_compat_model
    assert item.status == "error"
    assert item.code == "upstream_error"
    assert "10.0.0.8" not in item.model_dump_json()
    assert "secret" not in item.model_dump_json()
    assert "provider" not in item.model_dump_json()


def test_test_all_deduplicates_roles_sharing_one_effective_llm(identity, user, settings):
    from app.services.model_status import ModelStatusService

    calls = []
    service = ModelStatusService(
        identity,
        settings,
        probe=lambda config: calls.append((config.kind, config.model)),
    )

    result = service.test_all(user)
    expected_unique = {
        (descriptor.config.kind, descriptor.config.model)
        for descriptor in service.descriptors(user)
        if descriptor.config.configured
    }

    assert set(calls) == expected_unique
    assert len(calls) == len(expected_unique)
    assert {item.service for item in result.services} >= {
        "llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank", "embedding"
    }


def test_unconfigured_service_never_probes(identity, user, tmp_path):
    from app.services.model_status import ModelStatusService

    empty_settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/empty-model-status.db",
        storage_dir=str(tmp_path / "empty-storage"),
        user_model_config_policy="required",
    )
    empty_identity = SQLiteRepository(empty_settings)._runtime.identity
    calls = []
    service = ModelStatusService(empty_identity, empty_settings, probe=lambda cfg: calls.append(cfg))

    item = service.test_one(empty_identity.current_user(), "llm")

    assert item.status == "unconfigured"
    assert calls == []


def test_embedding_provider_off_never_runs_the_fake_embedder_probe(tmp_path):
    from app.services.model_status import ModelStatusService

    off_settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/provider-off.db",
        storage_dir=str(tmp_path / "provider-off-storage"),
        embed_provider="",
        embed_base_url="https://embed.example/v1",
        embed_api_key="embed-secret",
        embed_model="embed-live-model",
    )
    off_identity = SQLiteRepository(off_settings)._runtime.identity
    calls = []
    service = ModelStatusService(off_identity, off_settings, probe=lambda cfg: calls.append(cfg))

    item = service.test_one(off_identity.current_user(), "embedding")

    assert item.status == "unconfigured"
    assert item.configured is False
    assert calls == []


def test_whitespace_only_system_rerank_is_unconfigured_and_never_probed(tmp_path):
    from app.services.model_status import ModelStatusService

    rerank_settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/rerank-whitespace.db",
        storage_dir=str(tmp_path / "rerank-whitespace-storage"),
        rerank_base_url="   ",
        rerank_api_key=" rerank-secret ",
        rerank_model=" rerank-model ",
    )
    rerank_identity = SQLiteRepository(rerank_settings)._runtime.identity
    calls = []
    service = ModelStatusService(
        rerank_identity, rerank_settings, probe=lambda config: calls.append(config)
    )

    result = service.test_one(rerank_identity.current_user(), "rerank")

    assert result.configured is False
    assert result.status == "unconfigured"
    assert calls == []
    assert rerank_identity.get_model_service_statuses("user-local").get("rerank") is None


def test_default_probes_use_the_exact_resolved_embedding_and_rerank_protocol(
    identity, user, settings, monkeypatch
):
    from app.services import model_status

    embed_calls = []
    rerank_calls = []

    class _Embedder:
        def embed_query(self, text):
            embed_calls.append((text,))
            return [0.0]

    def fake_make_embedder(_settings, **overrides):
        embed_calls.append(overrides)
        return _Embedder()

    class _Reranker:
        def __init__(self, _settings, **overrides):
            rerank_calls.append(overrides)

        def _rerank_batch(self, query, documents):
            rerank_calls.append((query, documents))

    monkeypatch.setattr(model_status, "make_embedder", fake_make_embedder)
    monkeypatch.setattr(model_status, "RerankClient", _Reranker)
    settings.rerank_api_style = "vllm"
    service = model_status.ModelStatusService(identity, settings)

    embedding = identity.resolve_model_config(user, "embedding")
    rerank = identity.resolve_model_config(user, "rerank")
    service._probe(embedding)
    service._probe(rerank)

    assert embed_calls[0] == {
        "provider": "dashscope",
        "base_url": "https://embed.example/v1",
        "api_key": "embed-secret",
        "model": "embed-live-model",
    }
    assert rerank_calls[0]["api_style"] == "openai"


def test_record_observed_failure_stores_only_safe_status(identity, user, settings):
    from app.services.model_status import ModelStatusService

    service = ModelStatusService(identity, settings)
    service.record_observed_failure(
        user,
        "llm",
        model_config_fingerprint(identity.resolve_model_config(user, "llm")),
    )

    stored = identity.get_model_service_statuses(user.id)["llm"]
    assert stored["config_fingerprint"] == model_config_fingerprint(
        identity.resolve_model_config(user, "llm")
    )
    assert stored["status"] == "error"
    assert stored["code"] == "upstream_error"
    assert stored["trigger"] == "observed_failure"


def test_observed_failure_skips_an_identity_replaced_before_the_failure_arrives(
    identity, user, settings
):
    from app.services.model_status import ModelStatusService

    service = ModelStatusService(identity, settings, probe=lambda _config: None)
    old_config = identity.resolve_model_config(user, "llm")
    old_fingerprint = model_config_fingerprint(old_config)
    identity.set_user_model_settings(user.id, {
        "llm": {
            "base_url": "https://new.example/v1",
            "api_key": "new-secret",
            "model": "new-runtime-model",
        }
    })
    current = service.test_one(user, "llm")

    recorded = service.record_observed_failure(user, "llm", old_fingerprint)

    assert recorded is False
    stored = identity.get_model_service_statuses(user.id)["llm"]
    assert stored["status"] == "ok"
    assert stored["config_fingerprint"] == model_config_fingerprint(
        identity.resolve_model_config(user, "llm")
    )
    assert current.model == "new-runtime-model"


def test_snapshot_drops_unsafe_persisted_metadata(identity, user, settings):
    from app.services.model_status import ModelStatusService

    service = ModelStatusService(identity, settings)
    config = identity.resolve_model_config(user, "llm")
    with identity.database.write() as database:
        database.execute(
            "INSERT INTO model_service_status "
            "(user_id, service, config_fingerprint, status, latency_ms, code, trigger, checked_at) "
            "VALUES (?, 'llm', ?, 'error', 15, ?, 'manual_test', ?)",
            (
                user.id,
                model_config_fingerprint(config),
                "provider 10.0.0.8 secret body",
                "provider 10.0.0.8 secret body",
            ),
        )

    item = next(item for item in service.snapshot(user).services if item.service == "llm")

    assert item.status == "error"
    assert item.code == ""
    assert item.trigger == "manual_test"
    assert item.checked_at == ""
    assert "10.0.0.8" not in item.model_dump_json()
    assert "secret" not in item.model_dump_json()


@pytest.mark.parametrize(
    "unsafe_model",
    [
        "https://10.0.0.8/v1?api_key=provider-secret",
        "sk-0123456789abcdefghijklmnop",
        "model/10.0.0.8",
        "x/sk-0123456789abcdefghijklmnop",
        "model/http://provider.example/v1",
        "model/[2001:db8::1]",
        "model/fe80::1",
        "localhost:8000",
        "model/127.0.0.1:8000",
        "model-authorization-diagnostic",
        "api.example.com:8000",
        "api.example.com:443",
        "x/api.example.com:443",
        "api:8000",
        "host:80",
        "server:22",
        "gateway:443",
        "intranet:80",
        "custom-host:443",
        "model:65535",
        "model10.0.0.8",
        "modelsk-0123456789abcdefghijklmnop",
    ],
)
def test_status_hides_unsafe_model_but_probe_receives_raw_effective_config(
    identity, user, settings, unsafe_model
):
    from app.services.model_status import ModelStatusService

    identity.set_user_model_settings(user.id, {
        "llm": {
            "base_url": "https://configured.example/v1",
            "api_key": "configured-key",
            "model": unsafe_model,
        }
    })
    calls = []
    service = ModelStatusService(identity, settings, probe=lambda config: calls.append(config.model))

    snapshot = next(item for item in service.snapshot(user).services if item.service == "llm")
    explicit = service.test_one(user, "llm")

    assert snapshot.model == ""
    assert explicit.model == ""
    assert calls == [unsafe_model]
    assert unsafe_model not in snapshot.model_dump_json()
    assert unsafe_model not in explicit.model_dump_json()


@pytest.mark.parametrize(
    "safe_model",
    [
        "llama3.2:latest",
        "llama3:70b",
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
        "meta-llama/Llama-3.1-8B-Instruct",
    ],
)
def test_status_preserves_safe_namespace_and_tag_model_identifiers(
    identity, user, settings, safe_model
):
    from app.services.model_status import ModelStatusService

    identity.set_user_model_settings(user.id, {
        "llm": {
            "base_url": "https://configured.example/v1",
            "api_key": "configured-key",
            "model": safe_model,
        }
    })
    calls = []
    service = ModelStatusService(identity, settings, probe=lambda config: calls.append(config.model))

    snapshot = next(item for item in service.snapshot(user).services if item.service == "llm")
    explicit = service.test_one(user, "llm")

    assert snapshot.model == safe_model
    assert explicit.model == safe_model
    assert calls == [safe_model]
