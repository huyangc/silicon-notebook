"""Task 7: 深度报告 API 端点(创建/列表/详情/取消/删除)+ 取消注册表。"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    # 隔离 LLM 端点:清空真实 key/model,保证「LLM 未配置 → 409」分支确定性
    # (不受运行环境 OS env 泄漏影响;与 test_report_engine.py 的 repo fixture 同法)。
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL",
               "REWRITE_LLM_API_KEY", "REWRITE_LLM_BASE_URL", "REWRITE_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    from app.main import app
    return TestClient(app)


def test_report_endpoints_lifecycle(client, monkeypatch):
    # 建 notebook
    nb = client.post("/api/notebooks", json={"name": "t", "purpose": "p",
                                             "primary_domain": "d"}).json()
    # 起报告:LLM 未配置 → 409
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question": "q"})
    assert r.status_code == 409
    # stub 引擎线程:不真跑(单测不起真深挖)
    import app.api.routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_report_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question": "为什么?"})
    assert r.status_code == 200
    rid = r.json()["report_id"]
    lst = client.get(f"/api/notebooks/{nb['id']}/reports").json()
    assert lst[0]["id"] == rid and lst[0]["status"] == "pending"
    detail = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert detail["question"] == "为什么?" and "content_md" in detail
    assert "references" in detail and detail["references"] == []
    assert client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/cancel").status_code == 200
    assert client.delete(f"/api/notebooks/{nb['id']}/reports/{rid}").status_code == 200
    assert client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").status_code == 404


def test_report_create_rejects_blank_question_and_missing_nb(client, monkeypatch):
    import app.api.routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_report_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question": "   "})
    assert r.status_code == 422
    r = client.post("/api/notebooks/nb-none/reports", json={"question": "q"})
    assert r.status_code == 404


def test_cancel_registry_live_thread_path(client, monkeypatch):
    """取消注册表:register → 端点 cancel 置事件返回 cancelling → unregister 后落库标记。"""
    from app.services.report_engine import (
        register_cancel, cancel_report, unregister_cancel)
    import app.api.routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_report_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(f"/api/notebooks/{nb['id']}/reports",
                      json={"question": "q"}).json()["report_id"]
    ev = register_cancel(rid)
    r = client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/cancel")
    assert r.json()["status"] == "cancelling" and ev.is_set()
    unregister_cancel(rid)
    assert cancel_report(rid) is False           # 注销后不再命中活动线程
    # 线程已结束路径:再 cancel → 直接落库 cancelled
    r = client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/cancel")
    assert r.json()["status"] == "cancelled"
    detail = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert detail["status"] == "cancelled"
