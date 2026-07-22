"""Protocol 契约套件——只测接口行为，不碰实现细节。

新增 backend 时把它加进 _BACKENDS，跑通即可切换。这是可替换性的另一半：
接口能插上不等于行为正确。
"""
import pytest

from app.core.cache import CacheBackend, NoCacheBackend


def _make_noop(tmp_path):
    return NoCacheBackend()


class MinimalBackend:
    """只实现 CacheBackend 两个必需方法的后端——可替换性的活体标尺。

    未来的 Redis/memcached 后端就是这个形状（TTL 与 LRU 交给服务端配置，
    不实现 CacheAdmin）。若有人把 stats/evict_tag 悄悄变成事实上的必需方法，
    本参数化项会立刻转红。不要因为"它看起来没用"而删掉它。
    """

    def __init__(self):
        self._d = {}

    def get(self, key):
        return self._d.get(key)

    def put(self, key, value, tag=""):
        self._d[key] = value


def _make_minimal(tmp_path):
    return MinimalBackend()


# 新 backend 在此登记：(名字, 构造函数, 是否真正持久化)
_BACKENDS = [
    ("noop", _make_noop, False),
    ("minimal", _make_minimal, True),
]


@pytest.fixture(params=_BACKENDS, ids=[b[0] for b in _BACKENDS])
def backend_case(request, tmp_path):
    name, factory, persists = request.param
    return factory(tmp_path), persists


def test_satisfies_protocol(backend_case):
    backend, _ = backend_case
    assert isinstance(backend, CacheBackend)


def test_missing_key_returns_none(backend_case):
    backend, _ = backend_case
    assert backend.get("absent") is None


def test_put_then_get(backend_case):
    backend, persists = backend_case
    backend.put("k", "v")
    assert backend.get("k") == ("v" if persists else None)


def test_overwrite_takes_effect(backend_case):
    backend, persists = backend_case
    backend.put("k", "v1")
    backend.put("k", "v2")
    assert backend.get("k") == ("v2" if persists else None)


def test_tag_argument_is_accepted(backend_case):
    """tag 是可选参数——不支持分组的实现必须能安全忽略它，而不是报错。"""
    backend, persists = backend_case
    backend.put("k", "v", tag="model-x")
    assert backend.get("k") == ("v" if persists else None)


def test_empty_string_value_roundtrips(backend_case):
    """空串是合法值，与"不存在"必须可区分（None vs ""）。"""
    backend, persists = backend_case
    backend.put("k", "")
    assert backend.get("k") == ("" if persists else None)
