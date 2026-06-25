"""测试进程默认开 auth_optional：无 token 的请求回退 seeded admin，
既有 HTTP 测试无需逐一登录即可继续以 admin 身份跑。"""
import os

os.environ.setdefault("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")


import pytest


@pytest.fixture(autouse=True)
def _reset_singleton_caches():
    """每个测试前后都清空 lru_cache 单例，防止测试间缓存污染
    （尤其是 test_auth.py 用不同 DATABASE_URL 注入 tmp_path 后缓存的 repository）。"""
    from app.core.config import get_settings
    from app.api import deps
    get_settings.cache_clear()
    deps.repository.cache_clear()
    yield
    get_settings.cache_clear()
    deps.repository.cache_clear()
