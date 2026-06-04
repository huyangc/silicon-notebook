from app.services.vector_cache import VectorCache


def test_cache_hit_and_version_invalidation():
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"e1": [1.0, 0.0]}

    c = VectorCache()
    v1 = c.get("nb1", version=("count=1", "ts=10"), loader=loader)
    v2 = c.get("nb1", version=("count=1", "ts=10"), loader=loader)
    assert v1 == v2 and calls["n"] == 1          # 同版本命中，不重复 loader

    c.get("nb1", version=("count=2", "ts=20"), loader=loader)
    assert calls["n"] == 2                        # 版本变 -> 重新 loader

    c.invalidate("nb1")
    c.get("nb1", version=("count=2", "ts=20"), loader=loader)
    assert calls["n"] == 3                        # 失效后重载
