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
def _reset_pending_bus():
    """pending_bus 是进程级单例,状态会跨测试串味——统一在这里重置。

    症状:任何测试只要触发 index_done / paper_meta_done 这类 emit,而当时没有
    活的 SSE 连接,事件就滞留进 _buffer;同一个 xdist worker 里后跑的
    test_me_pending_stream_first_frame_is_snapshot 读到的首帧于是变成这条事件
    而非 snapshot(端点契约就是「先补发离线缓冲、再发 snapshot」)。哪些测试同
    worker、谁先跑由 xdist 分配决定 → 表现为间歇失败。
    _loop 同理:端点 bind_loop 记下的 loop,在测试的 asyncio.run 结束后即关闭,
    留给后续测试就是个已关闭的 loop。

    ce8fa1e4 曾逐个给污染源打桩 emit,但 emit 点只会越来越多(彼时修掉
    test_paper_meta_service,如今 test_scale_index_repo / test_batch_ingest /
    test_relation_ann 仍在泄漏),逐点堵按下葫芦浮起瓢;故改为统一重置。
    """
    from app.services.pending_bus import pending_bus
    pending_bus.reset()
    yield
    pending_bus.reset()


@pytest.fixture(autouse=True)
def _mark_service_ready():
    """The readiness gate 503s every app route until startup warm-up flips ready.
    Tests drive the app via TestClient WITHOUT the lifespan (which is what runs
    warm-up), so mark ready up-front; test_readiness_gate toggles it explicitly."""
    from app.core import readiness
    readiness.mark_ready()
    yield
