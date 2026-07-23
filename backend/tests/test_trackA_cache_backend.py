"""T2: CacheBackend Protocol + NoCacheBackend + injection into OpenAICompatibleClient."""
from app.core.llm_cache import CacheBackend, LLMCache, NoCacheBackend, cache_key


def test_no_cache_backend_is_always_miss():
    nc = NoCacheBackend()
    k = cache_key("m", [{"role": "user", "content": "x"}], "{}")
    assert nc.get(k) is None
    nc.put(k, "value")     # no-op; must not raise
    assert nc.get(k) is None


def test_llm_cache_satisfies_protocol(tmp_path):
    """LLMCache must be recognised as a CacheBackend at runtime."""
    lc = LLMCache(str(tmp_path / "c.db"))
    assert isinstance(lc, CacheBackend)


def test_no_cache_satisfies_protocol():
    assert isinstance(NoCacheBackend(), CacheBackend)


def test_injected_cache_used_for_hit(monkeypatch, tmp_path):
    """When a CacheBackend is injected, a pre-seeded hit is returned without
    calling the real LLM — for an opt-in (validator-bearing) caller."""
    import json
    from app.core.cache import llm_key
    from app.core.config import Settings
    from app.core.llm import OpenAICompatibleClient
    from app.core.llm_cache import LLMCache

    path = str(tmp_path / "c.db")
    lc = LLMCache(path)
    schema_hint = "{answer: str}"
    model = "test-model"
    base_url = "http://fake"
    user_msg = {"role": "user", "content": "what is X?"}
    system_msg = {
        "role": "system",
        "content": (
            "You are the extraction and reasoning engine for "
            "silicon-notebook. Return valid JSON only, no markdown fences. "
            f"Schema hint: {schema_hint}"
        ),
    }

    # Construct client with injected cache; model/url/key must look configured.
    # Transport identity is passed explicitly (system service registry model).
    settings = Settings(llm_cache_enabled=False)
    injected_client = OpenAICompatibleClient(
        settings,
        base_url=base_url,
        api_key="fake",
        model=model,
        cache=lc,
    )
    # Pre-seed the exact key chat_json builds: system message is prepended; base_url
    # isolates same-model-name-different-endpoint; and the effective generation
    # params are part of the key. chat_json below passes none of them, so it
    # resolves to temperature/top_p=1.0/1.0 and max_tokens=the global default.
    key = llm_key(
        model, [system_msg, user_msg], schema_hint, base_url,
        temperature=1.0, top_p=1.0, max_tokens=settings.openai_compat_max_tokens,
    )
    lc.put(key, '{"answer": "cached"}')
    assert injected_client._cache is lc

    # chat_json must return the cached value without touching the network.
    # Opt-in caching: a validator-bearing caller reads the hit (the value passes).
    result = injected_client.chat_json(
        [user_msg], schema_hint, response_validator=lambda _c: True
    )
    assert json.loads(result)["answer"] == "cached"


def test_no_cache_backend_injection_skips_cache(monkeypatch, tmp_path):
    """Injecting NoCacheBackend means every call is a cache miss (no SQLite file)."""
    from app.core.config import Settings
    from app.core.llm import OpenAICompatibleClient
    from app.core.llm_cache import NoCacheBackend

    settings = Settings(llm_cache_enabled=False)
    client = OpenAICompatibleClient(
        settings,
        base_url="http://fake",
        api_key="fake",
        model="m",
        cache=NoCacheBackend(),
    )
    # _get_cache must return the injected NoCacheBackend
    assert isinstance(client._get_cache(), NoCacheBackend)


def test_chat_json_cache_bypass_uses_transport_and_does_not_overwrite_hit(
    monkeypatch, tmp_path
):
    """Availability probes must observe the live transport, never a stale hit."""
    from types import SimpleNamespace

    from app.core.config import Settings
    from app.core.llm import OpenAICompatibleClient

    schema_hint = '{"ok":true}'
    user_msg = {"role": "user", "content": "probe"}
    system_msg = {
        "role": "system",
        "content": (
            "You are the extraction and reasoning engine for "
            "silicon-notebook. Return valid JSON only, no markdown fences. "
            f"Schema hint: {schema_hint}"
        ),
    }
    model = "test-model"
    cache = LLMCache(str(tmp_path / "bypass.db"))
    key = cache_key(model, [system_msg, user_msg], schema_hint)
    cache.put(key, '{"ok":"cached"}')

    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"ok":"live"}')
                )
            ],
            usage=None,
        )

    settings = Settings(llm_cache_enabled=False)
    client = OpenAICompatibleClient(
        settings,
        base_url="http://fake",
        api_key="fake",
        model=model,
        cache=cache,
    )
    monkeypatch.setattr(
        client,
        "client",
        lambda: SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )
        ),
    )

    assert client.chat_json(
        [user_msg],
        schema_hint,
        bypass_cache=True,
    ) == '{"ok":"live"}'
    assert len(calls) == 1
    assert cache.get(key) == '{"ok":"cached"}'
