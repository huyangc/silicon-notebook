"""测试进程默认开 auth_optional：无 token 的请求回退 seeded admin，
既有 HTTP 测试无需逐一登录即可继续以 admin 身份跑。"""
import os

os.environ.setdefault("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")


import pytest


@pytest.fixture(autouse=True)
def _reset_singleton_caches(tmp_path, monkeypatch):
    """Give every test an isolated default DB/storage/log root and clear singletons.

    Individual tests may override or delete these variables after this fixture
    starts. The default prevents a partial ``Settings(database_url=...)`` from
    touching the developer's real ``.local/storage`` and makes the suite safe
    in linked worktrees and CI sandboxes.
    """
    from app.core.config import get_settings
    from app.api import deps
    repository_factory = deps.repository
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'default.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("LLM_LOG_PATH", str(tmp_path / "logs" / "llm.jsonl"))
    get_settings.cache_clear()
    repository_factory.cache_clear()
    yield
    get_settings.cache_clear()
    repository_factory.cache_clear()


@pytest.fixture(autouse=True)
def _mark_service_ready():
    """The readiness gate 503s every app route until startup warm-up flips ready.
    Tests drive the app via TestClient WITHOUT the lifespan (which is what runs
    warm-up), so mark ready up-front; test_readiness_gate toggles it explicitly."""
    from app.core import readiness
    readiness.mark_ready()
    yield
