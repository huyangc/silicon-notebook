"""没有埋点就无法证明缓存在工作——命中率是决定何时清理/是否有效的唯一依据。"""
from pathlib import Path

from app.core.cache.sqlite_backend import SqliteCacheBackend


def _mk(tmp_path):
    return SqliteCacheBackend(str(tmp_path / "c.db"))


def test_stats_counts_hits_and_misses(tmp_path):
    c = _mk(tmp_path)
    c.get("absent")                 # miss
    c.put("k", "v")
    c.get("k")                      # hit
    c.get("k")                      # hit
    s = c.stats()
    assert s["hits"] == 2
    assert s["misses"] == 1
    assert abs(s["hit_rate"] - 2 / 3) < 1e-9


def test_expired_read_counts_as_miss(tmp_path):
    import time

    c = SqliteCacheBackend(str(tmp_path / "c.db"), ttl_seconds=0.3)
    c.put("k", "v")
    time.sleep(0.4)
    assert c.get("k") is None
    assert c.stats()["misses"] == 1 and c.stats()["hits"] == 0


def test_hit_rate_is_zero_when_no_reads(tmp_path):
    assert _mk(tmp_path).stats()["hit_rate"] == 0.0


# 消费侧不得无条件调用 CacheAdmin 的运维方法（stats/evict_tag/clear）。裸文本子串
# 匹配起初（只用 ".stats()"/".evict_tag("/".clear()" 判断，不检查文件是否引用
# app.core.cache）在真实代码库上报了 7 个 offender；逐个核实后确认全部是方法名
# 撞车，与本模块的 CacheBackend/CacheAdmin 毫无关系：dict/set/list/deque 的内置
# .clear()（pending_bus.py 的 _buffer/_conns、knowledge_counts_cache.py 的模块级
# 记忆化字典、knowledge_lifecycle.py/unified_kg_store.py 的缓冲区、
# scale_artifact_runtime.py 的队列、knowhow/api.py 的清扫集合）、以及
# app.services.kg.scheduler 模块级函数 stats()（batch_ingest.py）。这 7 个文件没
# 有一个 import app.core.cache 下的任何名字，物理上不可能持有一个
# CacheBackend/CacheAdmin 实例；而当前两个真实消费者 app/core/llm.py、
# app/services/cached_embedder.py 只调用 get()/put()，从未调用运维方法。
#
# 因此把扫描范围收紧到"引用了 app.core.cache 的文件"——按 __init__.py 顶部文档，
# 这是消费缓存后端的唯一合法入口，真实消费者必然会出现这个子串。这排除了方法名
# 撞车的噪音，不放松对真实消费侧的约束：llm.py/cached_embedder.py 仍在扫描范围
# 内。收紧后的扫描逻辑抽成下面这个函数，供守卫本体与其变异验证共用——两者各写
# 一份会有漂移风险，变异验证可能不知不觉停止代表守卫的真实行为。
#
# 与 test_cache_cohesion_guard.py 同款免责声明：这是 best-effort 文本扫描，不是
# 安全边界——一个不 import app.core.cache、纯靠外部注入拿到 backend 实例的消费者
# 理论上能绕过它。
_ADMIN_CALLS = (".stats()", ".evict_tag(", ".clear()")


def _admin_call_offenders(paths):
    offenders = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "app.core.cache" not in text:
            continue
        for call in _ADMIN_CALLS:
            if call in text and "CacheAdmin" not in text:
                offenders.append(f"{path.name}: {call}")
    return offenders


def test_cache_admin_is_optional_and_consumers_probe_before_calling():
    """只实现 CacheBackend 的后端必须能正常工作——这是"将来能换 Redis"的命脉。

    Redis 后端只需 get/put 两个方法（TTL 走 SET ... EX，容量与 LRU 走 redis.conf
    的 maxmemory + maxmemory-policy），不实现 CacheAdmin 是合法且预期的形态。
    任何无条件调用 backend.stats()/evict_tag() 的消费侧代码都会在换后端时崩溃。
    """
    from app.core.cache import CacheAdmin, CacheBackend

    class OnlyGetPut:
        def __init__(self):
            self._d = {}

        def get(self, key):
            return self._d.get(key)

        def put(self, key, value, tag=""):
            self._d[key] = value

    minimal = OnlyGetPut()
    assert isinstance(minimal, CacheBackend), "两个方法就该满足 CacheBackend"
    assert not isinstance(minimal, CacheAdmin), "CacheAdmin 必须保持可选"

    # 消费侧不得无条件调用运维方法。扫描 app/ 下对 backend 运维方法的裸调用。
    backend_dir = Path(__file__).resolve().parents[1] / "app"
    paths = [
        p for p in backend_dir.rglob("*.py")
        if "__pycache__" not in p.parts and "core/cache/" not in p.as_posix()
    ]
    offenders = _admin_call_offenders(paths)
    assert not offenders, (
        "疑似无条件调用缓存运维方法，换成只实现 CacheBackend 的后端会崩：\n  "
        + "\n  ".join(offenders)
    )


def test_narrowed_scan_still_catches_a_real_consumer_violation(tmp_path):
    """变异验证：收紧扫描范围（只看引用 app.core.cache 的文件）没有削弱对真实
    消费侧的保护。构造一个"引用 app.core.cache 但无条件调用 .stats() 且不提
    CacheAdmin"的文件，跑与守卫本体完全相同的 `_admin_call_offenders`，断言它
    必须被抓到——证明收紧的是噪音而不是保护范围。
    """
    fake_consumer = tmp_path / "fake_consumer.py"
    fake_consumer.write_text(
        "from app.core.cache import CacheBackend\n"
        "\n"
        "def dump(backend: CacheBackend):\n"
        "    return backend.stats()\n",
        encoding="utf-8",
    )
    assert _admin_call_offenders([fake_consumer]) == ["fake_consumer.py: .stats()"]
