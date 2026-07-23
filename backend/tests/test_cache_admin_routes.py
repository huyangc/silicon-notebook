"""缓存的只读现状 + 失效逃生口（admin 端点）。

设计 §可观测 要求「一个『缓存现状』查询（总大小、命中率、按 model 分布）」，并把
`evict_tag(model)` 定为「换模型服务时的正确操作，而非等 TTL 到期」。在此之前
stats/evict_tag/clear 全仓零调用方——能力存在但**不可达**：没有端点、没有 CLI、
没有脚本，运维只能去删缓存文件，而没人知道要去删。TTL 默认 90 天存在的头号理由
正是「同名模型的权重被替换」，那恰恰是唯一需要主动清理的场景。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    # 端点读的是全局 settings，缓存开关必须在这里显式打开（conftest 为了隔离把
    # 整个测试进程置成 false）；路径指向 tmp_path，绝不碰仓库根那个共享缓存文件。
    monkeypatch.setenv("LLM_CACHE_ENABLED", "true")
    monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "cache.db"))
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    app = create_app()
    yield TestClient(app)
    get_settings.cache_clear()


def _auth(client, username):
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    t = client.post(
        "/api/auth/login", json={"username": username, "password": "pw"}
    ).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _auth_admin(client):
    t = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    ).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _backend():
    """端点用的那一个 backend 实例。

    工厂按路径记忆化，所以这里拿到的与端点内部拿到的**是同一个对象**——测试因此
    能预置内容再去查端点，不必绕过任何生产路径。
    """
    from app.core.cache import make_cache_backend
    from app.core.config import get_settings
    return make_cache_backend(get_settings())


def test_cache_endpoints_are_admin_only(client):
    """普通用户碰不到：缓存跨用户全局共享，清空影响所有人。"""
    who = _auth(client, "z00123456")
    assert client.get("/api/admin/cache", headers=who).status_code == 403
    assert client.post(
        "/api/admin/cache/evict", json={"tag": "m"}, headers=who
    ).status_code == 403


def test_cache_stats_reports_totals_and_per_model_distribution(client):
    """按 model 分布是「换了哪个模型服务、该清哪一份」的唯一依据。"""
    admin = _auth_admin(client)
    backend = _backend()
    backend.put("k1", "v1", tag="model-a")
    backend.put("k2", "v2", tag="model-a")
    backend.put("k3", "v3", tag="model-b")
    backend.get("k1")                      # hit
    backend.get("nope")                    # miss

    body = client.get("/api/admin/cache", headers=admin).json()
    assert body["enabled"] is True
    assert body["admin_supported"] is True
    assert body["entries"] == 3
    assert body["by_tag"] == {"model-a": 2, "model-b": 1}
    assert body["bytes"] > 0
    assert body["hits"] == 1 and body["misses"] == 1
    assert abs(body["hit_rate"] - 0.5) < 1e-9


def test_evict_by_model_removes_only_that_model(client):
    """换模型服务后的正确操作。同名模型换了权重时缓存键不变，不主动清就会一直
    回放旧答案直到 90 天 TTL 到期。"""
    admin = _auth_admin(client)
    backend = _backend()
    backend.put("k1", "v1", tag="model-a")
    backend.put("k2", "v2", tag="model-b")

    resp = client.post(
        "/api/admin/cache/evict", json={"tag": "model-a"}, headers=admin)
    assert resp.status_code == 200
    assert resp.json() == {"evicted": 1, "scope": "model-a"}
    assert backend.get("k1") is None, "指定的模型没被清掉"
    assert backend.get("k2") == "v2", "误伤了其他模型的缓存"


def test_clear_all_requires_an_explicit_flag(client):
    """不给「留空即全清」的默认：一次手滑就把整库清掉的代价太大。"""
    admin = _auth_admin(client)
    backend = _backend()
    backend.put("k1", "v1", tag="model-a")

    assert client.post(
        "/api/admin/cache/evict", json={}, headers=admin).status_code == 400
    assert backend.get("k1") == "v1", "空请求体不该清掉任何东西"

    resp = client.post(
        "/api/admin/cache/evict", json={"clear_all": True}, headers=admin)
    assert resp.json() == {"evicted": 1, "scope": "all"}
    assert backend.get("k1") is None


def test_tag_and_clear_all_together_is_rejected_and_clears_nothing(client):
    """契约是二选一。两个都传是自相矛盾的请求——此前静默走 clear_all 把整库清光并
    触发昂贵重填。现在先拒绝（400）、绝不执行任何清理。

    变异验证：把 admin_routes.evict_cache 里 `if payload.clear_all and tag: raise`
    那道门删掉后，本测试转红（请求被静默当成全清，k1 被清、状态码变 200）。
    """
    admin = _auth_admin(client)
    backend = _backend()
    backend.put("k1", "v1", tag="model-a")
    backend.put("k2", "v2", tag="model-b")

    resp = client.post(
        "/api/admin/cache/evict",
        json={"tag": "model-a", "clear_all": True},
        headers=admin,
    )
    assert resp.status_code == 400, "tag 与 clear_all 同时传必须被拒绝，而不是静默全清"
    assert backend.get("k1") == "v1", "被拒绝的歧义请求不该清掉任何东西"
    assert backend.get("k2") == "v2", "被拒绝的歧义请求不该清掉任何东西"


def test_endpoints_degrade_when_the_backend_has_no_admin_capability(client, monkeypatch):
    """只实现 get/put 的后端（Redis 把 TTL/LRU 交给服务端配置）是合法形态。

    端点必须 isinstance(backend, CacheAdmin) 探测后再调用，否则换后端时这两个
    端点会 500。守卫 test_cache_admin_is_optional_... 预留的正是这个写法。
    变异验证：把 admin_routes._cache_admin 里的 isinstance 判断删掉后本测试转红
    （AttributeError → 500，而不是如实的 501）。
    """
    admin = _auth_admin(client)

    class OnlyGetPut:
        def get(self, key):
            return None

        def put(self, key, value, tag=""):
            pass

    from app.api import admin_routes
    monkeypatch.setattr(admin_routes, "make_cache_backend", lambda _s: OnlyGetPut())

    assert client.get("/api/admin/cache", headers=admin).status_code == 501
    assert client.post(
        "/api/admin/cache/evict", json={"tag": "m"}, headers=admin
    ).status_code == 501
