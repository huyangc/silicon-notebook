"""缓存准入的第四道门：调用方提供的 schema 级 response_validator。

is_cacheable_llm_response 只做「非空 / 可解析 / 非 length」——一个语法合法但违反
调用方 schema 的响应（KG 抽取拿到 {"nodes":"invalid"}，nodes 该是 list 却是 string）
能过前三道门 → 缓存 90 天 → 下游静默产出 0 对象、重解析一直命中坏值。第四道门让
知道自己形状的调用方（KG 抽取）把这类响应挡在缓存外；ask/answer 不传 validator，
行为一字不变。
"""
from app.core.config import Settings
from app.core.llm import OpenAICompatibleClient
from app.services.kg.extract import _kg_fragment_cacheable, _refine_response_cacheable


class _ContentCompletions:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls += 1
        msg = type("M", (), {"content": self.outer.content})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice], "usage": None})()


class _ContentOpenAI:
    def __init__(self, content):
        self.calls = 0
        self.content = content
        self.chat = type("Ch", (), {"completions": _ContentCompletions(self)})()


def _client(tmp_path, monkeypatch, content):
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://llm.example.test")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "k")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "m")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_CACHE_ENABLED", "true")
    monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "cache.db"))
    client = OpenAICompatibleClient(Settings())
    fake = _ContentOpenAI(content)
    monkeypatch.setattr(client, "client", lambda: fake)
    return client, fake


_MSGS = [{"role": "user", "content": "extract a kg fragment"}]


# ------------------------------------------------------- the cache-write gate

def test_schema_invalid_reply_is_not_cached(tmp_path, monkeypatch):
    """{"nodes":"invalid"} passes the empty/parse/length gates but violates the
    KG schema (nodes must be a list). With a validator it must NOT be cached, so
    the second identical call re-hits the endpoint instead of serving poison."""
    client, fake = _client(tmp_path, monkeypatch, '{"nodes": "invalid"}')
    r1 = client.chat_json(_MSGS, "{}", response_validator=_kg_fragment_cacheable)
    r2 = client.chat_json(_MSGS, "{}", response_validator=_kg_fragment_cacheable)
    assert r1 == r2 == '{"nodes": "invalid"}'
    assert fake.calls == 2, "schema-invalid reply must not be cached (re-hits endpoint)"


def test_schema_valid_reply_is_still_cached(tmp_path, monkeypatch):
    """A well-shaped KG fragment must cache exactly like any usable reply — the
    validator must not be so strict it blocks good responses."""
    good = ('{"nodes": [{"local_id": "a", "type": "Concept", "name": "x", "ev": 0}],'
            ' "edges": []}')
    client, fake = _client(tmp_path, monkeypatch, good)
    client.chat_json(_MSGS, "{}", response_validator=_kg_fragment_cacheable)
    client.chat_json(_MSGS, "{}", response_validator=_kg_fragment_cacheable)
    assert fake.calls == 1, "schema-valid reply must be cached (second call is a hit)"


def test_no_validator_preserves_existing_caching(tmp_path, monkeypatch):
    """ask/answer callers pass no validator: even a KG-shape-invalid reply caches
    exactly as before (default None => the fourth door is open)."""
    client, fake = _client(tmp_path, monkeypatch, '{"nodes": "invalid"}')
    client.chat_json(_MSGS, "{}")
    client.chat_json(_MSGS, "{}")
    assert fake.calls == 1, "with no validator the old caching behavior is unchanged"


def test_validator_that_raises_conservatively_skips_cache(tmp_path, monkeypatch):
    """A validator fault (buggy validator) must never crash the call nor cache a
    possibly-bad value — it degrades to 'do not cache'."""
    client, fake = _client(tmp_path, monkeypatch, '{"nodes": []}')

    def boom(_content):
        raise RuntimeError("validator bug")

    assert client.chat_json(_MSGS, "{}", response_validator=boom) == '{"nodes": []}'
    client.chat_json(_MSGS, "{}", response_validator=boom)
    assert fake.calls == 2, "validator fault -> skip write, never cache, never crash"


# ------------------------------------------------------- the validators' shape

def test_kg_fragment_validator_rejects_wrong_typed_containers():
    # The reviewer's exact poisoning shape and its siblings: a dict whose
    # nodes/edges are the wrong type. These parse fine yet yield 0 grounded
    # objects — must never be cached.
    assert _kg_fragment_cacheable('{"nodes": "invalid"}') is False
    assert _kg_fragment_cacheable('{"nodes": {"a": 1}}') is False
    assert _kg_fragment_cacheable('{"edges": 5}') is False


def test_kg_fragment_validator_accepts_usable_shapes():
    assert _kg_fragment_cacheable('{"nodes": [], "edges": []}') is True
    assert _kg_fragment_cacheable('{"nodes": [{"type": "Concept"}]}') is True
    assert _kg_fragment_cacheable('{}') is True  # legitimately empty window


def test_refine_validator_shape():
    assert _refine_response_cacheable('{"items": [{"index": 0, "keep": true}]}') is True
    assert _refine_response_cacheable('{}') is True
    assert _refine_response_cacheable('{"items": "nope"}') is False
    assert _refine_response_cacheable('{"items": 7}') is False
