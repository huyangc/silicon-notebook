from app.services.model_config import resolve_effective_config, ResolvedModelConfig

U = {"llm": {"base_url": "https://u/v1", "api_key": "sk-u", "model": "m"}}
VAR = {"reasoning_llm": {"base_url": "https://r/v1", "api_key": "sk-r", "model": "rm"}}


def test_user_full_config_wins():
    r = resolve_effective_config(U, "llm", "fallback")
    assert (r.base_url, r.api_key, r.model, r.source) == ("https://u/v1", "sk-u", "m", "user")


def test_variant_falls_back_to_user_primary():
    r = resolve_effective_config(U, "kg_llm", "fallback")
    assert r.source == "user" and r.model == "m"


def test_variant_own_config_wins_over_primary():
    r = resolve_effective_config({**U, **VAR}, "reasoning_llm", "fallback")
    assert r.source == "user" and r.model == "rm"


def test_unconfigured_fallback_to_system():
    r = resolve_effective_config({}, "llm", "fallback")
    assert r.source == "system" and r.base_url == ""


def test_unconfigured_required_is_none():
    r = resolve_effective_config({}, "llm", "required")
    assert r.source == "none"


def test_rerank_has_no_variant_fallback():
    assert resolve_effective_config(U, "rerank", "fallback").source == "system"
    assert resolve_effective_config(U, "rerank", "required").source == "none"
