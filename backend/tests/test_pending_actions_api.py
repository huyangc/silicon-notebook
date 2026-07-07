"""API 结构测试：GET /api/me/pending-actions 快照端点。

薄包装测试——只验证 HTTP 层把 SQLiteRepository.pending_actions(user_id) 的返回值
（Task 1 已实现并有专门单测覆盖三源聚合逻辑）原样透传，不重复校验聚合细节。
匿名请求（无 Bearer token）在测试进程下 auth_optional=true（见 conftest.py），
回退 seeded user-local，应正常 200。
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_me_pending_actions_shape():
    resp = client.get("/api/me/pending-actions")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"count", "items"}
    assert isinstance(body["count"], int)
    assert isinstance(body["items"], list)
