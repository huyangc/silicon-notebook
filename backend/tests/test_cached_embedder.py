"""CachedEmbedder：per-text 内容寻址，批量部分命中必须顺序对齐。"""
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
    cached.embed_texts(["a"])
    assert backend.stats()["entries"] == 0


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
