from app.core.config import Settings
from app.services.model_config import (
    model_config_fingerprint,
    resolve_effective_config,
    system_model_settings,
)


def _settings(**overrides):
    values = {
        "openai_compat_base_url": "https://system.example/v1",
        "openai_compat_api_key": "system-secret",
        "openai_compat_model": "system-primary",
        "reasoning_llm_base_url": "https://reason.example/v1",
        "reasoning_llm_api_key": "reason-secret",
        "reasoning_llm_model": "reason-live",
        "rewrite_llm_model": "rewrite-live",
        "rerank_base_url": "https://rerank.example/v1",
        "rerank_api_key": "rerank-secret",
        "rerank_model": "rerank-live",
        "embed_provider": "dashscope",
        "embed_base_url": "https://embed.example/v1",
        "embed_api_key": "embed-secret",
        "embed_model": "embed-live",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_dedicated_reasoning_model_name_is_resolved_from_settings():
    settings = _settings(reasoning_llm_model="configured-at-runtime")
    config = resolve_effective_config(
        {}, "reasoning_llm", "fallback", system_model_settings(settings)
    )
    assert config.model == "configured-at-runtime"
    assert config.source == "system"
    assert config.kind == "llm"


def test_user_reasoning_role_falls_back_to_dynamic_user_primary():
    user = {
        "llm": {
            "base_url": "https://user.example/v1",
            "api_key": "user-secret",
            "model": "user-primary-live",
        }
    }
    config = resolve_effective_config(
        user, "reasoning_llm", "fallback", system_model_settings(_settings())
    )
    assert (config.model, config.source) == ("user-primary-live", "user")


def test_embedding_descriptor_is_system_managed():
    config = resolve_effective_config(
        {}, "embedding", "fallback", system_model_settings(_settings())
    )
    assert config.model == "embed-live"
    assert config.kind == "embedding"
    assert config.configured is True


def test_fingerprint_changes_when_credential_or_model_changes():
    settings = system_model_settings(_settings())
    first = resolve_effective_config({}, "llm", "fallback", settings)
    second = first.__class__(
        base_url=first.base_url,
        api_key="rotated-secret",
        model=first.model,
        source=first.source,
        kind=first.kind,
    )
    assert model_config_fingerprint(first) != model_config_fingerprint(second)
