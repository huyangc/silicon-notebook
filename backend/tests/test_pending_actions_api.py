"""API 结构测试：GET /api/me/pending-actions 快照端点 + /stream 流式端点。

薄包装测试——只验证 HTTP 层把 SQLiteRepository.pending_actions(user_id) 的返回值
（Task 1 已实现并有专门单测覆盖三源聚合逻辑）原样透传，不重复校验聚合细节。
匿名请求（无 Bearer token）在测试进程下 auth_optional=true（见 conftest.py），
回退 seeded user-local，应正常 200。

流式端点测试直接驱动 async generator（不经 starlette.testclient.TestClient 的
HTTP 层）——已用源码 + 实测确认 TestClient/httpx 的 in-process ASGI transport
（无论 sync TestClient 还是 httpx.ASGITransport）在拿到 Response 前会先把
StreamingResponse 的 body_iterator 完整跑到耗尽（portal.call(self.app, ...)
同步阻塞到 ASGI app 整个调用返回），对无限循环的端点(本端点靠 15s keepalive
持续 yield，永不 StopAsyncIteration)必然死锁整个测试进程（已用 faulthandler/
py-spy 级别的实机复现验证，非猜测）。已有先例 test_ask_stream_cancel.py 对
_stream_ask_events 采用同一手法：直接 await 端点函数拿 StreamingResponse，
再对 body_iterator 调 __anext__() 逐帧断言，绕开该 transport 限制。
"""
import asyncio
import json as _json

from fastapi.testclient import TestClient

from app.api.routes import me_pending_stream
from app.main import app
from app.models.schemas import UserProfile

client = TestClient(app)


def test_me_pending_actions_shape():
    resp = client.get("/api/me/pending-actions")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"count", "items"}
    assert isinstance(body["count"], int)
    assert isinstance(body["items"], list)


class _FakeRequest:
    """占位 Request：本测试只读第一帧(snapshot)就关闭 generator，不会真正
    走到 is_disconnected() 那一步，但端点签名需要一个 Request 实例。"""

    async def is_disconnected(self) -> bool:
        return False


def test_me_pending_stream_first_frame_is_snapshot():
    # 直接驱动端点的 async generator：读第一帧 NDJSON 应为 snapshot。
    # 见文件头注释——不用 TestClient.stream()，该 transport 对无限流式端点会
    # 死锁（拿到 Response 前先把 body_iterator 跑到耗尽）。
    user = UserProfile(
        id="user-local", email="local@example.com", display_name="Local", role="admin",
    )

    async def scenario():
        resp = await me_pending_stream(_FakeRequest(), user)
        assert "application/x-ndjson" in resp.media_type
        first_line = await resp.body_iterator.__anext__()
        msg = _json.loads(first_line.rstrip("\n"))
        assert msg["kind"] == "snapshot"
        assert "data" in msg and set(msg["data"].keys()) == {"count", "items"}
        # 显式关闭 generator，避免 pending_bus.register 之后的无限循环泄漏
        # 成后台任务(此处尚未 register，aclose 只是让 gen() 提前退出/幂等)。
        await resp.body_iterator.aclose()

    asyncio.run(scenario())
