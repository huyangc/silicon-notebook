"""CachedEmbedder：per-text 内容寻址，批量部分命中必须顺序对齐。"""
import pytest

from app.core.cache import NoCacheBackend
from app.core.cache.sqlite_backend import SqliteCacheBackend
from app.services.cached_embedder import CachedEmbedder


class RecordingEmbedder:
    """记录每次收到的文本，返回可辨识的确定性向量。"""

    dim = 4

    def __init__(self):
        self.calls = []

    def _vec(self, text):
        return [float(len(text)), float(sum(map(ord, text[:1])) if text else 0), 0.0, 1.0]

    def embed_texts(self, texts):
        self.calls.append(list(texts))
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class RecordingBackend:
    """可按门注入故障的缓存后端：get / put 各自能独立炸，用于逐门变异验证。"""

    def __init__(self, *, get_error=False, put_error=False):
        self.store = {}
        self.gets = []
        self.puts = []
        self.get_error = get_error
        self.put_error = put_error

    def get(self, key):
        self.gets.append(key)
        if self.get_error:
            raise RuntimeError("cache get boom")
        return self.store.get(key)

    def put(self, key, value, tag=""):
        self.puts.append(key)
        if self.put_error:
            raise RuntimeError("cache put boom")
        self.store[key] = value


def _mk(tmp_path, truncate_chars=2000):
    inner = RecordingEmbedder()
    backend = SqliteCacheBackend(str(tmp_path / "c.db"))
    return inner, CachedEmbedder(inner, backend, model="m1",
                                 truncate_chars=truncate_chars)


def test_second_call_is_served_from_cache(tmp_path):
    inner, cached = _mk(tmp_path)
    first = cached.embed_texts(["alpha"])
    second = cached.embed_texts(["alpha"])
    assert first == second
    assert len(inner.calls) == 1, "第二次不应再打后端"


def test_partial_hit_only_requests_missing_texts_in_order(tmp_path):
    """命中项与未命中项交错——顺序错配是静默灾难（向量张冠李戴）。"""
    inner, cached = _mk(tmp_path)
    cached.embed_texts(["b", "d"])              # 预热 b、d
    inner.calls.clear()
    out = cached.embed_texts(["a", "b", "c", "d", "e"])
    assert inner.calls == [["a", "c", "e"]], "只应请求未命中的三条"
    assert out == [inner._vec(t) for t in ["a", "b", "c", "d", "e"]], "顺序错配"
    assert len(out) == 5


def test_duplicate_texts_in_one_batch_cause_one_backend_call(tmp_path):
    inner, cached = _mk(tmp_path)
    out = cached.embed_texts(["x", "y", "x"])
    assert inner.calls == [["x", "y"]], "同批重复文本只应请求一次"
    assert out[0] == out[2] == inner._vec("x")


def test_key_is_based_on_truncated_text(tmp_path):
    """两个前 N 字符相同的长文本发给 API 的内容相同，必须共用同一条缓存。"""
    inner, cached = _mk(tmp_path, truncate_chars=5)
    cached.embed_texts(["abcde-TAIL-1"])
    inner.calls.clear()
    cached.embed_texts(["abcde-TAIL-2"])
    assert inner.calls == [], "截断后内容相同，应当命中缓存"


def test_length_mismatch_response_is_not_cached(tmp_path):
    """后端返回长度与输入不符时，不得写入缓存（否则毒化后续请求）。"""

    class BadEmbedder(RecordingEmbedder):
        def embed_texts(self, texts):
            self.calls.append(list(texts))
            return []                            # 长度不符

    inner = BadEmbedder()
    backend = SqliteCacheBackend(str(tmp_path / "c.db"))
    cached = CachedEmbedder(inner, backend, model="m1", truncate_chars=2000)
    with pytest.raises(RuntimeError):
        cached.embed_texts(["a"])
    assert backend.stats()["entries"] == 0


def test_length_mismatch_raises_instead_of_returning_misaligned_vectors():
    """部分命中 + 后端短应答：绝不能把这份响应当成对 texts 的应答返回。

    它是对 **missing 子集**（这里只有 "C"）的应答，而调用方按 texts 的下标消费
    （embed_in_chunks 的 out[start + offset] = vec）——直接返回会把 C 的向量装到
    A 头上，正是本层要防的张冠李戴。正确形状也重建不出来：无从得知返回的向量分别
    对应 missing 里的哪几条。故只能抛。

    变异验证：把 raise 换回 `return list(vectors)` 后本测试转红。
    """

    class MismatchEmbedder(RecordingEmbedder):
        def embed_texts(self, texts):
            self.calls.append(list(texts))
            # 评审实测的形状：请求 1 条，回来 2 条。
            return [self._vec(t) for t in texts] + [[999.0, 999.0, 999.0, 999.0]]

    good = RecordingEmbedder()
    backend = RecordingBackend()
    CachedEmbedder(good, backend, model="m1",
                   truncate_chars=2000).embed_texts(["A", "B"])   # 预热 A、B

    bad = MismatchEmbedder()
    cached = CachedEmbedder(bad, backend, model="m1", truncate_chars=2000)
    with pytest.raises(RuntimeError, match="2 vectors for 1 texts"):
        cached.embed_texts(["A", "B", "C"])
    assert bad.calls == [["C"]], "只应请求未命中的 C"

    # 已有条目不被这次异常毒化，后续请求仍拿到自己的向量。
    again = CachedEmbedder(good, backend, model="m1", truncate_chars=2000)
    assert again.embed_texts(["A", "B"]) == [good._vec("A"), good._vec("B")]


def test_inner_embedder_exception_propagates_and_caches_nothing():
    """真实 embedder 抛异常必须原样透传：各调用点的 per-batch except 依赖它把整批
    记成失败，吞掉会变成静默丢向量。同时一条缓存都不许写。

    变异验证：把 `self._inner.embed_texts(missing)` 包进 try/except 返回 [] 后
    本测试转红。
    """

    class BoomEmbedder(RecordingEmbedder):
        def embed_texts(self, texts):
            self.calls.append(list(texts))
            raise RuntimeError("upstream 500")

    inner = BoomEmbedder()
    backend = RecordingBackend()
    cached = CachedEmbedder(inner, backend, model="m1", truncate_chars=2000)
    with pytest.raises(RuntimeError, match="upstream 500"):
        cached.embed_texts(["a"])
    assert inner.calls == [["a"]]
    assert backend.puts == [], "调用失败不得留下任何缓存条目"


def test_cache_get_failure_degrades_to_miss():
    """读缓存炸掉只能退化成 miss——缓存故障绝不影响主流程（项目硬约束）。

    变异验证：删掉 embed_texts 里包住 `self._backend.get(key)` 的 try/except 后
    本测试转红。与 put 那道门分开测：一道门被遮蔽时另一道会假绿。
    """
    inner = RecordingEmbedder()
    backend = RecordingBackend(get_error=True)
    cached = CachedEmbedder(inner, backend, model="m1", truncate_chars=2000)
    out = cached.embed_texts(["a", "b"])
    assert out == [inner._vec("a"), inner._vec("b")]
    assert backend.gets, "应当真的尝试过读缓存，否则测的不是这道门"
    assert len(backend.puts) == 2, "读失败不该妨碍回写"


def test_cache_put_failure_degrades_silently():
    """写缓存炸掉同样只能吞掉，向量照常返回。

    变异验证：删掉包住 `self._backend.put(...)` 的 try/except 后本测试转红。
    """
    inner = RecordingEmbedder()
    backend = RecordingBackend(put_error=True)
    cached = CachedEmbedder(inner, backend, model="m1", truncate_chars=2000)
    out = cached.embed_texts(["a"])
    assert out == [inner._vec("a")]
    assert backend.puts, "应当真的尝试过写缓存，否则测的不是这道门"


def test_corrupt_cache_entry_degrades_to_miss():
    """缓存里躺着一条坏值时当 miss，不能把解码异常抛进主流程。"""
    inner = RecordingEmbedder()
    backend = RecordingBackend()
    cached = CachedEmbedder(inner, backend, model="m1", truncate_chars=2000)
    backend.store[cached._key("a")] = "not-json-at-all"
    backend.store[cached._key("b")] = '["nope", "not", "a", "vector"]'
    assert cached.embed_texts(["a", "b"]) == [inner._vec("a"), inner._vec("b")]


def test_empty_input_short_circuits():
    """空批既不打后端也不碰缓存。

    ⚠ 变异说明：删掉 `if not texts: return []` 是**等价变异**——空列表沿正常路径
    同样返回 []、同样不碰后端，没有任何测试鉴别得出来（该行是显式的防御性早退）。
    本测试锁的是契约本身，用行为变异（把 `return []` 改成 `return [[0.0]]`）验证
    过转红。
    """
    inner = RecordingEmbedder()
    backend = RecordingBackend()
    cached = CachedEmbedder(inner, backend, model="m1", truncate_chars=2000)
    assert cached.embed_texts([]) == []
    assert inner.calls == []
    assert backend.gets == [] and backend.puts == []


def test_missing_inner_does_not_recurse_forever():
    """_inner 缺席时 __getattr__ 必须如实报 AttributeError，不是 RecursionError。

    构造中途抛异常 / copy 出的空壳都会落到这个形态；无限递归会把一次属性探测
    （多是带默认值的 getattr）变成崩溃。
    """
    shell = CachedEmbedder.__new__(CachedEmbedder)
    with pytest.raises(AttributeError):
        shell.dim
    assert getattr(shell, "dim", "fallback") == "fallback"


def test_private_attributes_still_pass_through(tmp_path):
    """私有名不能一刀切拒绝：source_embedding._warm_up 靠
    `getattr(embedder, "_ensure", None)` 预建 HTTP 客户端规避多线程首触竞态，
    拒绝私有名会让预热静默变成 no-op（那里的 getattr 带默认值，不会报错）。
    """

    class WarmableEmbedder(RecordingEmbedder):
        def __init__(self):
            super().__init__()
            self.ensured = 0

        def _ensure(self):
            self.ensured += 1

    inner = WarmableEmbedder()
    cached = CachedEmbedder(inner, NoCacheBackend(), model="m1", truncate_chars=2000)
    ensure = getattr(cached, "_ensure", None)
    assert callable(ensure), "_warm_up 拿不到 _ensure 就会静默跳过预热"
    ensure()
    assert inner.ensured == 1


def test_attributes_pass_through(tmp_path):
    inner, cached = _mk(tmp_path)
    assert cached.dim == 4
    assert cached.embed_query("q") == inner._vec("q")


def test_noop_backend_disables_caching(tmp_path):
    inner = RecordingEmbedder()
    cached = CachedEmbedder(inner, NoCacheBackend(), model="m1", truncate_chars=2000)
    cached.embed_texts(["a"])
    cached.embed_texts(["a"])
    assert len(inner.calls) == 2


def test_health_probe_bypasses_cache():
    """模型故障时探针若命中缓存会显示假绿——必须绕过。

    变异验证：把 model_status.py 里的 cache=False 改成 True 后，本测试必须转红。
    """
    import inspect

    from app.services import model_status

    src = inspect.getsource(model_status)
    idx = src.find("make_embedder(")
    assert idx != -1, "model_status 里找不到 make_embedder 调用"
    window = src[idx:idx + 400]
    assert "cache=False" in window, "健康探针未绕过缓存，模型故障会被缓存掩盖成假绿"


def test_make_embedder_returns_uncached_when_cache_false():
    from app.core.config import Settings
    from app.services.embedding import make_embedder

    e = make_embedder(Settings(), cache=False)
    assert e.__class__.__name__ != "CachedEmbedder"


def test_make_embedder_survives_unusable_cache_file(tmp_path, monkeypatch):
    """缓存后端构造失败绝不能打死 make_embedder。

    make_embedder 在 SQLiteRepository.__init__ 里被调用，而 deps.repository() 是
    所有请求级依赖的唯一入口——在这里抛异常 = 每个请求 500，且 @lru_cache 永远
    缓存不住；离线 CLI 同样起不来。触发条件不玄学：WAL 崩溃残留、.local 不可写、
    路径撞到别的文件（本测试用的就是最后一种）。

    变异验证：去掉 embedding.py 里包住 make_cache_backend 的 try 后本测试转红。
    """
    from app.core.cache import make_cache_backend
    from app.core.config import Settings
    from app.services.embedding import make_embedder

    bad = tmp_path / "not-a-database.db"
    bad.write_bytes(b"this is definitely not a sqlite database\n" * 64)
    monkeypatch.setenv("LLM_CACHE_ENABLED", "true")
    monkeypatch.setenv("LLM_CACHE_PATH", str(bad))
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-v4")
    settings = Settings()

    with pytest.raises(Exception):     # 前提：这个文件确实构造不出后端
        make_cache_backend(settings)

    e = make_embedder(settings)        # 不得抛
    assert e.__class__.__name__ == "DashscopeEmbedder", "缓存不可用时应降级为不包装"
    assert e.dim == settings.embed_dim, "降级后仍须是个能用的 embedder"
    assert getattr(e, "_model_status_fingerprint", ""), "身份绑定不能因降级丢失"


def test_model_settings_test_endpoint_bypasses_cache():
    """POST /me/model-settings/test 是前端「测试连接」按钮打的探活端点。全仓 36 个
    chat_json 调用点中探活性质共 3 处，这是其中之一——命中缓存会在服务已挂掉时
    仍回放 ok=True 对用户撒谎，chat_json 调用必须显式 bypass_cache=True。

    变异验证：把 system_routes.py 里的 bypass_cache=True 删掉后，本测试必须转红。
    """
    import inspect

    from app.api import system_routes

    src = inspect.getsource(system_routes)
    idx = src.find("chat_json(")
    assert idx != -1, "system_routes 里找不到 chat_json 调用"
    window = src[idx:idx + 300]
    assert "bypass_cache=True" in window, "健康探针未绕过缓存，模型故障会被缓存掩盖成假绿"
