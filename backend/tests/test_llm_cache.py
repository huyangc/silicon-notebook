from app.core.llm_cache import LLMCache, cache_key


def test_cache_key_is_stable_and_order_independent():
    m = [{"role": "user", "content": "hi"}]
    k1 = cache_key("model-x", m, "{}")
    k2 = cache_key("model-x", [{"content": "hi", "role": "user"}], "{}")
    assert k1 == k2                      # dict key order must not matter
    assert k1 != cache_key("model-y", m, "{}")   # model is part of the key


def test_put_get_roundtrip_and_miss(tmp_path):
    c = LLMCache(str(tmp_path / "c.db"))
    k = cache_key("m", [{"role": "user", "content": "q"}], "{}")
    assert c.get(k) is None              # miss
    c.put(k, '{"a": 1}')
    assert c.get(k) == '{"a": 1}'        # hit


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "c.db")
    k = cache_key("m", [{"role": "user", "content": "q"}], "{}")
    LLMCache(path).put(k, "cached")
    assert LLMCache(path).get(k) == "cached"   # second instance, same file


from app.core.config import Settings
from app.core.llm import OpenAICompatibleClient


class _FakeCompletions:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls += 1
        msg = type("M", (), {"content": '{"ok": 1}'})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice], "usage": None})()


class _FakeOpenAI:
    def __init__(self):
        self.calls = 0
        self.chat = type("Ch", (), {"completions": _FakeCompletions(self)})()


def _configured_client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://llm.example.test")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "k")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "m")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_CACHE_ENABLED", "true")
    monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "cache.db"))
    client = OpenAICompatibleClient(Settings())
    fake = _FakeOpenAI()
    monkeypatch.setattr(client, "client", lambda: fake)
    return client, fake


def test_chat_json_caches_identical_calls(tmp_path, monkeypatch):
    client, fake = _configured_client(tmp_path, monkeypatch)
    msgs = [{"role": "user", "content": "same question"}]
    r1 = client.chat_json(msgs, "{}")
    r2 = client.chat_json(msgs, "{}")
    assert r1 == r2 == '{"ok": 1}'
    assert fake.calls == 1               # second call served from cache


def test_chat_json_cache_disabled(tmp_path, monkeypatch):
    client, fake = _configured_client(tmp_path, monkeypatch)
    monkeypatch.setattr(client.settings, "llm_cache_enabled", False)
    msgs = [{"role": "user", "content": "q"}]
    client.chat_json(msgs, "{}")
    client.chat_json(msgs, "{}")
    assert fake.calls == 2               # no caching -> two endpoint calls
