"""缓存的准入门：现在是 **opt-in** —— 调用方传 response_validator 才参与缓存。

Codex 第 6 轮 / 用户拍板：不传 validator 的调用方（Ask、paper_meta、summary、schema
归纳、query rewrite…）既不写也不读缓存。一次性关掉整个投毒类——此前它们偶发拿到
`[]`/error 形状也会被缓存 90 天，重试/reparse 一直复用坏值。占 93% 成本的 KG 抽取三处
（extract_window/gleaning/refine）传 validator → 保持缓存。

两道门**各自独立**、可逐门变异：
- 写入门：`response_validator is not None`（+ is_cacheable + validator 形状）。
- 命中门：`response_validator is not None` 才 `cache.get` 并 serve。
cache/ckey 对每个非 bypass 调用都算出来，所以去掉任一门都能被对应测试单独抓红。
"""
from app.core.cache import llm_key
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
    # Transport identity is passed explicitly (system service registry model);
    # Settings no longer carries openai_compat_base_url/api_key/model.
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_CACHE_ENABLED", "true")
    monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "cache.db"))
    client = OpenAICompatibleClient(
        Settings(), base_url="https://llm.example.test", api_key="k", model="m",
    )
    fake = _ContentOpenAI(content)
    monkeypatch.setattr(client, "client", lambda: fake)
    return client, fake


_MSGS = [{"role": "user", "content": "extract a kg fragment"}]


def _expected_key(client, schema_hint="{}", user_content="extract a kg fragment"):
    """The exact key chat_json builds: system message prepended, base_url in the
    key, generation params resolved (temperature/top_p default 1.0, max_tokens =
    the global default)."""
    system = {
        "role": "system",
        "content": (
            "You are the extraction and reasoning engine for "
            "silicon-notebook. Return valid JSON only, no markdown fences. "
            f"Schema hint: {schema_hint}"
        ),
    }
    full = [system, {"role": "user", "content": user_content}]
    mt = client.settings.openai_compat_max_tokens
    eff = mt if isinstance(mt, int) and mt > 0 else None
    return llm_key(
        client.model, full, schema_hint, client.base_url,
        temperature=1.0, top_p=1.0, max_tokens=eff,
    )


# ------------------------------------------------------- the cache-WRITE gate

def test_schema_valid_reply_with_validator_is_cached(tmp_path, monkeypatch):
    """opt-in happy path: a well-shaped KG fragment WITH a validator caches, so
    the second identical call is a hit (one endpoint call total)."""
    good = ('{"nodes": [{"local_id": "a", "type": "Concept", "name": "x", "ev": 0}],'
            ' "edges": []}')
    client, fake = _client(tmp_path, monkeypatch, good)
    client.chat_json(_MSGS, "{}", response_validator=_kg_fragment_cacheable)
    client.chat_json(_MSGS, "{}", response_validator=_kg_fragment_cacheable)
    assert fake.calls == 1, "validator + 合格响应必须缓存（第二次是 hit）"
    assert client._get_cache().get(_expected_key(client)) == good


def test_schema_invalid_reply_is_not_cached(tmp_path, monkeypatch):
    """{"nodes":"invalid"} passes the empty/parse/length gates but violates the
    KG schema (nodes must be a list). With a validator it must NOT be cached, so
    the second identical call re-hits the endpoint instead of serving poison."""
    client, fake = _client(tmp_path, monkeypatch, '{"nodes": "invalid"}')
    r1 = client.chat_json(_MSGS, "{}", response_validator=_kg_fragment_cacheable)
    r2 = client.chat_json(_MSGS, "{}", response_validator=_kg_fragment_cacheable)
    assert r1 == r2 == '{"nodes": "invalid"}'
    assert fake.calls == 2, "schema-invalid reply must not be cached (re-hits endpoint)"


def test_no_validator_reply_is_not_written(tmp_path, monkeypatch):
    """opt-in WRITE gate (isolated): a validator-LESS call must write NOTHING,
    even for a reply that would pass is_cacheable_llm_response. Asserted by
    inspecting the backend directly — a plain re-call would also blame the hit
    gate. Mutation: drop `response_validator is not None` from the write gate ->
    this good reply gets written -> the key is present -> RED."""
    client, fake = _client(tmp_path, monkeypatch, '{"nodes": []}')
    client.chat_json(_MSGS, "{}")
    assert fake.calls == 1
    assert client._get_cache().get(_expected_key(client)) is None, (
        "opt-out: 不传 validator 时任何响应都不该被写进缓存"
    )


def test_no_validator_reply_is_not_served_or_written_end_to_end(tmp_path, monkeypatch):
    """The user-visible contract: a validator-less call is transparent to the
    cache — two identical calls both reach the endpoint (no hit ever served)."""
    client, fake = _client(tmp_path, monkeypatch, '{"nodes": []}')
    client.chat_json(_MSGS, "{}")
    client.chat_json(_MSGS, "{}")
    assert fake.calls == 2, "不传 validator：连续两次同调用都打 upstream"


def test_validator_that_raises_conservatively_skips_cache(tmp_path, monkeypatch):
    """A validator fault (buggy validator) must never crash the call nor cache a
    possibly-bad value — it degrades to 'do not cache'."""
    client, fake = _client(tmp_path, monkeypatch, '{"nodes": []}')

    def boom(_content):
        raise RuntimeError("validator bug")

    assert client.chat_json(_MSGS, "{}", response_validator=boom) == '{"nodes": []}'
    client.chat_json(_MSGS, "{}", response_validator=boom)
    assert fake.calls == 2, "validator fault -> skip write, never cache, never crash"


# --------------------------------------------------------- the cache-HIT gate

def test_no_validator_does_not_read_a_prepopulated_hit(tmp_path, monkeypatch):
    """opt-in HIT gate (isolated): pre-seed the cache with a GOOD value at the
    exact key, then call WITHOUT a validator. It must NOT be served — the call
    goes to the endpoint. Mutation: drop the `response_validator is not None`
    guard around the serve -> the seeded value is handed back -> 0 endpoint
    calls -> RED."""
    client, fake = _client(tmp_path, monkeypatch, '{"nodes": [{"type": "Concept"}]}')
    client._get_cache().put(_expected_key(client), '{"nodes": []}', tag="m")
    out = client.chat_json(_MSGS, "{}")
    assert fake.calls == 1, "不传 validator 时预置命中不得被 serve（必须打 upstream）"
    assert out == '{"nodes": [{"type": "Concept"}]}', "返回的是 upstream 的新响应，不是缓存"


def test_validator_bearing_call_is_served_a_good_hit(tmp_path, monkeypatch):
    """The hit gate must not block GOOD cached values: a well-shaped reply written
    with a validator is served on the next validator-bearing call (no re-hit).

    The value is a groundable node (name + integer ev): the window-less
    _kg_fragment_cacheable now judges grounding by the minimal field set, so a bare
    ``{"type":"Concept"}`` (no name/ev) is no longer a cacheable 'good' reply."""
    good = '{"nodes": [{"type": "Concept", "name": "x", "ev": 0}], "edges": []}'
    client, fake = _client(tmp_path, monkeypatch, good)
    client.chat_json(_MSGS, "{}", response_validator=_kg_fragment_cacheable)
    client.chat_json(_MSGS, "{}", response_validator=_kg_fragment_cacheable)
    assert fake.calls == 1, "满足 validator 的好值仍必须命中（第二次是 hit）"


def test_a_cache_hit_that_fails_the_validator_is_not_served(tmp_path, monkeypatch):
    """A value present at the key (e.g. written earlier by a laxer validator, or
    seeded) must not be handed to a validator-bearing caller if it fails THAT
    validator. The reject is treated as a MISS: the call falls through to the
    endpoint (whose fresh reply the write gate then re-judges)."""
    client, fake = _client(tmp_path, monkeypatch, '{"nodes": [{"type": "Concept"}]}')
    # Seed poison directly (opt-in means a validator-less write can't create it).
    client._get_cache().put(_expected_key(client), '{"nodes": "invalid"}', tag="m")
    r = client.chat_json(_MSGS, "{}", response_validator=_kg_fragment_cacheable)
    assert fake.calls == 1, (
        "命中一个不过 validator 的坏值必须当 miss 处理、走真实调用，而不是原样返回"
    )
    assert r == '{"nodes": [{"type": "Concept"}]}'


# ------------------------------------------------------- the validators' shape

def test_kg_fragment_validator_rejects_wrong_typed_containers():
    # The reviewer's exact poisoning shape and its siblings: a dict whose
    # nodes/edges are the wrong type. These parse fine yet yield 0 grounded
    # objects — must never be cached.
    assert _kg_fragment_cacheable('{"nodes": "invalid"}') is False
    assert _kg_fragment_cacheable('{"nodes": {"a": 1}}') is False
    assert _kg_fragment_cacheable('{"edges": 5}') is False


def test_kg_fragment_validator_rejects_error_envelopes_and_junk_entries():
    # Round-8 structural tightening: cache only a reply that GROUNDS >=1 object (the
    # real downstream gate), or is a legit empty window. Replies that ground to 0
    # objects but would DIFFER on retry (error envelopes), are malformed (junk node
    # entries), or are present-but-ungroundable must not be frozen for the whole TTL.
    assert _kg_fragment_cacheable('{"error": "quota"}') is False  # no nodes + error
    assert _kg_fragment_cacheable('{"nodes": [], "error": "partial"}') is False  # error env
    assert _kg_fragment_cacheable('{"nodes": [{"garbage": 1}]}') is False  # typeless junk
    assert _kg_fragment_cacheable('{"nodes": [{"type": "Widget"}]}') is False  # off-vocab type
    assert _kg_fragment_cacheable('{"nodes": [{"type": "Concept"}, 5]}') is False  # non-dict entry
    # The round-4..8 whack-a-mole shape: a typed node with NO name/ev. It parses,
    # passes every earlier shape check, yet extract_window drops it (ungroundable)
    # so the window yields 0 objects. The window-less gate rejects it on the minimal
    # field set (name empty), and the elements-aware gate rejects it on real resolve.
    assert _kg_fragment_cacheable('{"nodes": [{"type": "Concept"}]}') is False


def test_kg_fragment_validator_accepts_usable_shapes():
    assert _kg_fragment_cacheable('{"nodes": [], "edges": []}') is True  # legit empty window
    assert _kg_fragment_cacheable(
        '{"nodes": [{"local_id": "a", "type": "Concept", "name": "x", "ev": 0}],'
        ' "edges": []}'
    ) is True  # a normal, fully-formed extraction (name + integer ev -> grounds)
    # No `nodes` key is error/malformed, NOT a legit empty window (that is
    # {"nodes":[],...}); is_cacheable_llm_response already rejects a bare "{}".
    assert _kg_fragment_cacheable('{}') is False


def test_refine_validator_shape():
    # The window-less DEGRADED entry (no nodes to bound-check against): keeps the
    # structural + type(index) is int + keep bool checks; only the range check is
    # dropped (it is unknowable without the node list — see the nodes-closed tests
    # in tests/kg/test_extract.py).
    assert _refine_response_cacheable('{"items": [{"index": 0, "keep": true}]}') is True
    assert _refine_response_cacheable('{"items": []}') is True  # keep-all = empty decisions
    assert _refine_response_cacheable('{"items": "nope"}') is False
    assert _refine_response_cacheable('{"items": 7}') is False
    assert _refine_response_cacheable('{}') is False  # absent items != keep-all
    assert _refine_response_cacheable('{"error": "quota"}') is False
    assert _refine_response_cacheable('{"items": [{"garbage": 1}]}') is False  # no index/keep
    # bool is an int subclass — `index: true` must NOT be treated as an integer
    # index (downstream would consume it as index 1). type(index) is int excludes it,
    # so even the degraded entry (no range check) rejects it.
    assert _refine_response_cacheable('{"items": [{"index": true, "keep": false}]}') is False
    # keep must be a real bool, not just present.
    assert _refine_response_cacheable('{"items": [{"index": 0, "keep": 1}]}') is False
